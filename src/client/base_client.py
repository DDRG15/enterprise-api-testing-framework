"""
src/client/base_client.py
==========================
The core HTTP client for the entire framework.

This is the most critical module. Every design decision here affects
test reliability, observability, and security. Key guarantees:

  1. ZERO INFINITE HANGS    — explicit (connect, read) timeout on every call.
  2. SMART RETRY            — retries ONLY on transient infrastructure errors
                              (502, 503, 504, connection error, timeout).
                              NEVER retries logic errors (4xx). Retrying a 401
                              14 times is noise, not resilience.
  3. 429 RATE-LIMIT AWARE   — if the server sends Retry-After, the client
                              sleeps EXACTLY that duration before retrying.
                              Ignoring Retry-After is a protocol violation and
                              gets your IP banned on Tier-1 financial APIs.
  4. CIRCUIT BREAKER        — after N consecutive failures, fail-fast.
                              No thundering herd against a degraded upstream.
  5. FULL OBSERVABILITY     — on ANY failure: URL, method, headers (masked),
                              payload, status, response body, elapsed time,
                              and correlation ID are all logged.
  6. SLO ENFORCEMENT        — responses that arrive "successfully" but too slowly
                              are treated as failures.
  7. CORRELATION IDs        — every request carries X-Correlation-ID so that
                              a single test's traffic can be traced end-to-end.
  8. STRICT SSL             — verify= is NEVER False. CA bundle is configurable.

429 / Retry-After design note:
  RFC 7231 §7.1.3 defines two valid formats for the Retry-After header:
    - Delta-seconds: "Retry-After: 120"
    - HTTP-date:     "Retry-After: Wed, 21 Oct 2025 07:28:00 GMT"

  Both are parsed. The resulting sleep is capped at settings.retry_max_delay_seconds
  to prevent a malicious or misconfigured server from issuing a sleep-forever
  instruction — a subtle denial-of-service vector in rate-limited environments.
  This cap override is always logged as a warning.
"""
from __future__ import annotations

import datetime
import time
import uuid
from email.utils import parsedate_to_datetime
from typing import Any, Optional

import requests
import structlog
from requests import Response, Session
from requests.exceptions import (
    ConnectionError as RequestsConnectionError,
    ReadTimeout,
    ConnectTimeout,
    Timeout,
)
from tenacity import (
    RetryError,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)
from tenacity.wait import wait_base

from config.settings import settings
from src.utils.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError
from src.utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Retry predicate: ONLY retry on transient infrastructure failures.
# ---------------------------------------------------------------------------

#: HTTP status codes that represent transient server-side failures.
TRANSIENT_STATUS_CODES: frozenset[int] = frozenset({429, 502, 503, 504})

#: Exception types that indicate a network-level transient failure.
TRANSIENT_EXCEPTIONS: tuple[type[Exception], ...] = (
    RequestsConnectionError,
    ReadTimeout,
    ConnectTimeout,
    Timeout,
)


def _is_transient_failure(exc: Exception) -> bool:
    """
    Return True ONLY for infrastructure-level transients.

    Logic errors (400, 401, 403, 404, 409) must NOT be retried —
    they are deterministic failures that will not resolve on their own.
    """
    if isinstance(exc, TRANSIENT_EXCEPTIONS):
        return True
    if isinstance(exc, _TransientHttpError):
        return True
    return False


class _TransientHttpError(Exception):
    """
    Internal sentinel raised when a transient HTTP status is received.

    Crucially, it carries the parsed Retry-After delay so the custom
    wait strategy (_RetryAfterWait) can honour the server's instruction.
    This is the bridge between the HTTP layer and the retry scheduler.
    """

    def __init__(
        self,
        status_code: int,
        url: str,
        retry_after_seconds: Optional[float] = None,
    ) -> None:
        super().__init__(f"Transient HTTP {status_code} from {url}")
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


# ---------------------------------------------------------------------------
# Retry-After header parser (RFC 7231 §7.1.3 compliant)
# ---------------------------------------------------------------------------


def _parse_retry_after(header_value: str) -> Optional[float]:
    """
    Parse the Retry-After header into float seconds to wait.

    Supports:
      - Delta-seconds: "120"                        → 120.0
      - HTTP-date:     "Fri, 01 May 2026 08:00:00 GMT" → computed delta

    Returns None on parse failure — never crashes on malformed headers.
    The caller falls back to exponential backoff in that case.
    """
    if not header_value:
        return None

    value = header_value.strip()

    # --- Attempt 1: delta-seconds ---
    try:
        delta = float(value)
        if delta >= 0:
            return delta
    except ValueError:
        pass

    # --- Attempt 2: HTTP-date ---
    try:
        retry_at = parsedate_to_datetime(value)
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        delta = (retry_at - now).total_seconds()
        return max(0.0, delta)  # Guard against clock skew returning negative
    except Exception:
        pass

    logger.warning(
        "retry_after_parse_failed",
        header_value=header_value,
        note="Unrecognised Retry-After format. Using exponential backoff.",
    )
    return None


# ---------------------------------------------------------------------------
# Dynamic wait strategy: Retry-After-aware with exponential fallback
# ---------------------------------------------------------------------------


class _RetryAfterWait(wait_base):
    """
    Custom tenacity wait strategy for intelligent rate-limit handling.

    Decision tree per retry attempt:
      1. Was the failure a 429 with a parseable Retry-After header?
         YES → sleep exactly that duration (capped at max_delay for safety).
         NO  → delegate to exponential backoff.

    This is the correct way to be a "good citizen" API consumer.
    Respecting Retry-After:
      - Prevents IP bans on production APIs with strict rate limiters.
      - Reduces load on stressed upstream services.
      - Is required by RFC 6585 and expected by all major API gateways.
    """

    def __init__(self, fallback: wait_base, max_delay: float) -> None:
        self._fallback = fallback
        self._max_delay = max_delay

    def __call__(self, retry_state: Any) -> float:
        exc = retry_state.outcome.exception()

        if isinstance(exc, _TransientHttpError) and exc.retry_after_seconds is not None:
            raw = exc.retry_after_seconds
            capped = min(raw, self._max_delay)

            if capped < raw:
                # The server asked for more time than our configured maximum.
                # We cap it but log loudly — this warrants operator attention.
                logger.warning(
                    "retry_after_cap_override",
                    server_requested_s=round(raw, 2),
                    capped_to_s=round(capped, 2),
                    max_configured_s=self._max_delay,
                    action=(
                        "Sleeping for capped duration instead. "
                        "Consider raising RETRY_MAX_DELAY_SECONDS if this "
                        "service has known long rate-limit windows."
                    ),
                )
            else:
                logger.info(
                    "retry_after_honoured",
                    sleeping_seconds=round(capped, 2),
                    status_code=exc.status_code,
                    protocol="RFC 7231 §7.1.3",
                )

            return capped

        # No Retry-After — use exponential backoff as the safe default
        return self._fallback(retry_state)


# ---------------------------------------------------------------------------
# Core Client
# ---------------------------------------------------------------------------


class ApiClient:
    """
    Base HTTP client. Instantiate once per test session (or per test, if
    you need strict isolation). All test-facing client classes inherit this.

    Args:
        base_url: API base URL. Defaults to settings.api_base_url.
        circuit_breaker: Optional shared CB instance. If None, a new one
                         is created scoped to this client instance.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
    ) -> None:
        self._base_url = (base_url or settings.api_base_url).rstrip("/")
        self._session = Session()
        self._cb = circuit_breaker or CircuitBreaker(
            name="api-client",
            failure_threshold=settings.circuit_breaker_failure_threshold,
            recovery_timeout=settings.circuit_breaker_recovery_timeout_seconds,
        )
        self._configure_session()

    def _configure_session(self) -> None:
        """Configure session-level defaults applied to every request."""
        self._session.verify = settings.ssl_verify
        self._session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    def set_auth_token(self, token: str) -> None:
        """Inject a session token for the lifetime of this client."""
        self._session.headers["Cookie"] = f"token={token}"

    def close(self) -> None:
        """Release the underlying connection pool. Call in fixture teardown."""
        self._session.close()

    # ------------------------------------------------------------------
    # Public request interface
    # ------------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
        correlation_id: Optional[str] = None,
    ) -> Response:
        """
        Execute an HTTP request with the full resilience stack applied.

        Returns:
            requests.Response on success.

        Raises:
            CircuitBreakerOpenError: Circuit is OPEN; upstream considered down.
            requests.exceptions.Timeout: Server exceeded configured timeout.
            AssertionError: Response time exceeded the SLO threshold.
            RetryError: All retry attempts exhausted.
        """
        cid = correlation_id or str(uuid.uuid4())
        url = f"{self._base_url}{path}"

        merged_headers: dict[str, str] = {
            **dict(self._session.headers),
            **(headers or {}),
            "X-Correlation-ID": cid,
        }

        log = logger.bind(
            correlation_id=cid,
            method=method.upper(),
            url=url,
        )

        # ------------------------------------------------------------------
        # Compose the wait strategy: Retry-After-aware → exponential fallback
        # ------------------------------------------------------------------
        exponential_backoff = wait_exponential(
            multiplier=settings.retry_base_delay_seconds,
            max=settings.retry_max_delay_seconds,
        )
        smart_wait = _RetryAfterWait(
            fallback=exponential_backoff,
            max_delay=settings.retry_max_delay_seconds,
        )

        # ------------------------------------------------------------------
        # Tenacity retry loop
        # ------------------------------------------------------------------
        try:
            for attempt in Retrying(
                retry=retry_if_exception(_is_transient_failure),
                stop=stop_after_attempt(settings.retry_max_attempts),
                wait=smart_wait,
                reraise=True,
            ):
                with attempt:
                    attempt_number = attempt.retry_state.attempt_number
                    if attempt_number > 1:
                        log.warning(
                            "request_retry_attempt",
                            attempt=attempt_number,
                            max_attempts=settings.retry_max_attempts,
                        )

                    response = self._execute_single_request(
                        method=method,
                        url=url,
                        params=params,
                        json=json,
                        headers=merged_headers,
                        log=log,
                    )

                    # For 429: parse Retry-After and carry it in the sentinel.
                    # For other transients: raise with no delay override.
                    if response.status_code in TRANSIENT_STATUS_CODES:
                        retry_after: Optional[float] = None

                        if response.status_code == 429:
                            raw_header = response.headers.get("Retry-After", "")
                            retry_after = _parse_retry_after(raw_header)
                            log.warning(
                                "rate_limited_429",
                                retry_after_header=raw_header or "<absent>",
                                parsed_wait_s=retry_after,
                                note=(
                                    "Server is rate-limiting this client. "
                                    "Honouring Retry-After before next attempt."
                                    if retry_after
                                    else "No Retry-After header. Using exponential backoff."
                                ),
                            )

                        raise _TransientHttpError(
                            response.status_code, url, retry_after
                        )

        except RetryError as exc:
            log.error(
                "request_exhausted_retries",
                max_attempts=settings.retry_max_attempts,
                last_exception=str(exc),
            )
            raise
        except CircuitBreakerOpenError:
            log.error("request_circuit_open", circuit_name=self._cb.name)
            raise

        # ------------------------------------------------------------------
        # SLO enforcement — a slow 200 is still an incident.
        # ------------------------------------------------------------------
        elapsed_ms = int(response.elapsed.total_seconds() * 1000)
        if elapsed_ms > settings.slo_response_time_ms:
            log.error(
                "slo_breach",
                elapsed_ms=elapsed_ms,
                slo_threshold_ms=settings.slo_response_time_ms,
                status_code=response.status_code,
            )
            raise AssertionError(
                f"SLO BREACH: {method.upper()} {url} took {elapsed_ms}ms "
                f"(threshold: {settings.slo_response_time_ms}ms). "
                "Response time is a first-class failure."
            )

        log.info(
            "request_success",
            status_code=response.status_code,
            elapsed_ms=elapsed_ms,
        )
        return response

    def _execute_single_request(
        self,
        method: str,
        url: str,
        params: Optional[dict[str, Any]],
        json: Optional[dict[str, Any]],
        headers: dict[str, str],
        log: structlog.stdlib.BoundLogger,
    ) -> Response:
        """
        Executes exactly one HTTP request. Called by the retry loop.
        All failure information is logged here before propagating.
        """
        log.debug(
            "request_attempt",
            params=params,
            request_headers=headers,  # Sensitive values masked by logger processor
            request_body=json,
        )

        start_ns = time.perf_counter_ns()
        try:
            with self._cb:
                response = self._session.request(
                    method=method.upper(),
                    url=url,
                    params=params,
                    json=json,
                    headers=headers,
                    timeout=settings.timeout_tuple,
                    allow_redirects=True,
                )
        except (ConnectTimeout, ReadTimeout, Timeout) as exc:
            elapsed_s = (time.perf_counter_ns() - start_ns) / 1e9
            log.error(
                "request_timeout",
                elapsed_s=round(elapsed_s, 3),
                timeout_config=settings.timeout_tuple,
                exception=str(exc),
            )
            raise
        except RequestsConnectionError as exc:
            log.error("request_connection_error", exception=str(exc))
            raise

        elapsed_ms = int(response.elapsed.total_seconds() * 1000)
        log_ctx = dict(
            status_code=response.status_code,
            elapsed_ms=elapsed_ms,
            response_headers=dict(response.headers),
        )

        if not response.ok:
            log.warning(
                "request_non_ok_response",
                response_body=self._safe_response_body(response),
                **log_ctx,
            )
        else:
            log.debug("request_raw_response", **log_ctx)

        return response

    @staticmethod
    def _safe_response_body(response: Response) -> str:
        try:
            return response.text[:4096]
        except Exception:
            return "<unreadable response body>"

    # ------------------------------------------------------------------
    # Convenience methods
    # ------------------------------------------------------------------

    def get(self, path: str, **kwargs: Any) -> Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Response:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Response:
        return self.request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Response:
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Response:
        return self.request("DELETE", path, **kwargs)
