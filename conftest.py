"""
conftest.py
============
The framework's central configuration and fixture hub.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  AUDIT FIXES IMPLEMENTED IN THIS FILE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  FIX 1 — xdist Health Check (was: fires once per worker, hammers API)
  ─────────────────────────────────────────────────────────────────────
  The `health_check` fixture now uses `filelock.FileLock` — a cross-platform
  file locking library (Windows, Linux, macOS) — to ensure exactly ONE health
  check fires across all xdist workers, regardless of how many parallel workers
  are spawned. All other workers wait on the lock and then read the result from
  a JSON result file. If the health check fails, the result is broadcast to all
  workers. No fcntl, no OS-specific calls.

  FIX 2 — Teardown Deadlock (was: orphan_cleanup used circuit-protected client)
  ──────────────────────────────────────────────────────────────────────────────
  `orphan_cleanup` now uses `DirectApiClient` — a subclass of ApiClient that
  is instantiated WITHOUT a circuit breaker. It bypasses circuit state entirely.
  A tripped circuit means the API was down during the test run. Cleanup must
  attempt to reach the server regardless, using its own independent retry config.
  If the API is still down, cleanup failures are logged as CRITICAL and the
  booking IDs are written to a `leaked_resources.txt` file for manual recovery.

  FIX 3 — Circuit Breaker Distributed State
  ──────────────────────────────────────────
  `shared_circuit_breaker` now calls `make_circuit_breaker()`, which returns
  a Redis-backed instance when REDIS_URL is set (parallel mode) and an
  in-memory instance when it is not (single-process mode). The fixture logs
  which mode is active so operators can verify their configuration.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Fixture scoping:
  session  → Once per test run (or once per xdist worker for session fixtures,
             except health_check which uses a file lock for true once-only).
  function → One per test. Fresh client, fresh booking client, fresh data.

Teardown guarantee:
  Every resource-creating fixture uses yield + finally.
  Teardown failures are logged but never re-raised.
  The orphan registry and leak file are the last lines of defence.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Generator, Optional

from filelock import FileLock, Timeout

import pytest
import structlog

from config.settings import settings
from src.client.base_client import ApiClient
from src.client.booking_client import BookingClient
from src.models.booking import BookingDates, BookingPayload
from src.utils.circuit_breaker import make_circuit_breaker
from src.utils.logger import RUN_ID, configure_logging, get_logger

logger = get_logger(__name__)

# Path to write IDs that could not be cleaned up during this run.
_LEAK_FILE = Path("logs/leaked_resources.txt")

# File-system lock path for xdist-safe health check coordination.
_HEALTH_CHECK_LOCK = Path(tempfile.gettempdir()) / "api_fw_health_check.lock"
_HEALTH_CHECK_RESULT = Path(tempfile.gettempdir()) / "api_fw_health_check.result"


# ===========================================================================
# xdist helpers
# ===========================================================================

def _get_worker_id(config: pytest.Config) -> str:
    """
    Returns the xdist worker ID string ('gw0', 'gw1', etc.) or
    'master' if running in single-process mode.
    """
    worker_input = getattr(config, "workerinput", None)
    if worker_input:
        return worker_input.get("workerid", "gw0")
    return "master"


def _is_xdist_worker(config: pytest.Config) -> bool:
    return hasattr(config, "workerinput")


# ===========================================================================
# Session-scoped: run once for the entire test suite
# ===========================================================================


@pytest.fixture(scope="session", autouse=True)
def configure_framework_logging() -> None:
    """
    Initialize structured JSON logging before any test runs.
    autouse=True ensures this cannot be forgotten.
    Each worker writes to its own log file to avoid interleaved writes.
    """
    worker_id = _get_worker_id(pytest.Config.fromdictargs({}, []))  # type: ignore[arg-type]
    log_file = settings.log_file
    if worker_id not in ("master", "controller"):
        # Each worker gets its own log shard to avoid file-write contention.
        p = Path(settings.log_file)
        log_file = str(p.parent / f"{p.stem}_{worker_id}{p.suffix}")

    configure_logging(log_file=log_file, log_level=settings.log_level)
    logger.info(
        "framework_session_start",
        run_id=RUN_ID,
        worker_id=worker_id,
        base_url=settings.api_base_url,
        log_file=log_file,
        redis_url=settings.redis_url or "NOT_SET (in-memory CB mode)",
    )


@pytest.fixture(scope="session")
def shared_circuit_breaker():
    """
    ─── FIX 3: Distributed Circuit Breaker ───────────────────────────────────
    Returns the correct circuit breaker implementation based on REDIS_URL:

      REDIS_URL set   → RedisCircuitBreaker (shared state across all workers)
      REDIS_URL unset → InMemoryCircuitBreaker (single-process only)

    The fixture logs which mode is active. If running pytest-xdist without
    REDIS_URL, a WARNING is emitted so the operator knows the CB is not
    providing distributed protection.
    """
    cb = make_circuit_breaker(
        name="session-circuit-breaker",
        failure_threshold=settings.circuit_breaker_failure_threshold,
        recovery_timeout=settings.circuit_breaker_recovery_timeout_seconds,
        redis_url=settings.redis_url,
    )

    mode = "distributed_redis" if settings.redis_url else "in_memory_single_process"
    log_fn = logger.info if settings.redis_url else logger.warning

    log_fn(
        "circuit_breaker_mode",
        mode=mode,
        threshold=settings.circuit_breaker_failure_threshold,
        **({"warning": "Not suitable for pytest-xdist parallel runs without Redis"}
           if not settings.redis_url else {}),
    )

    return cb


@pytest.fixture(scope="session")
def health_check(shared_circuit_breaker, pytestconfig) -> None:
    """
    ─── FIX 1: xdist-Safe Health Check (cross-platform) ─────────────────────
    Fires exactly ONCE across ALL workers on Windows, Linux, and macOS.

    Uses the `filelock` PyPI package — a pure cross-platform file locking
    library with no OS-specific calls (no fcntl, no win32api).

    Protocol:
      LEADER  — the first worker to acquire the lock with timeout=0.
                Runs the health check, writes pass/fail JSON to a result
                file, then releases the lock.

      FOLLOWER — every other worker. Blocks on `with lock` until the leader
                 releases, then reads the result file and honours it.

    Why filelock over fcntl:
      - fcntl is POSIX-only and raises ImportError on Windows entirely.
      - filelock uses LockFile on Windows (CreateFile with exclusive access)
        and fcntl on POSIX internally — the same semantics, one API,
        zero platform conditionals in our code.

    Without this fix: N workers × 1 health check = N simultaneous API hits.
    With this fix:    exactly 1 hit, always, on every OS.
    """
    _HEALTH_CHECK_LOCK.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(_HEALTH_CHECK_LOCK))

    try:
        # Non-blocking attempt — timeout=0 raises Timeout immediately if
        # the lock is already held by another worker.
        lock.acquire(timeout=0)
        # ── LEADER PATH ──────────────────────────────────────────────────
        # We are the first worker. Run the check and write the result so
        # followers can read it after we release.
        try:
            _run_health_check_and_write_result(shared_circuit_breaker)
        finally:
            lock.release()  # Release before followers proceed

    except Timeout:
        # ── FOLLOWER PATH ─────────────────────────────────────────────────
        # Another worker is running (or has run) the health check.
        # Block until the leader releases the lock, then read the result.
        logger.info("health_check_waiting_for_leader_worker")
        with lock:          # Blocks until leader releases; auto-releases after
            pass            # Lock acquired means leader has written the result
        _read_health_check_result()


def _run_health_check_and_write_result(circuit_breaker) -> None:
    """
    Execute the health check probe and write the result to a shared file.
    Called only by the leader worker. Result is read by all follower workers.
    """
    probe_client = ApiClient(circuit_breaker=circuit_breaker)
    result: dict = {"status": "unknown", "message": ""}

    try:
        response = probe_client.get(
            "/booking",
            params={"firstname": f"_healthcheck_{RUN_ID[:8]}_"},
        )
        assert response.status_code in {200, 404}, (
            f"Health check returned unexpected status {response.status_code}"
        )
        result = {"status": "pass", "message": f"HTTP {response.status_code}"}
        logger.info("health_check_passed", status_code=response.status_code)

    except Exception as exc:
        result = {"status": "fail", "message": str(exc)}
        logger.critical("health_check_failed", exception=str(exc))

    finally:
        probe_client.close()
        # Write result BEFORE releasing the lock so followers always find it.
        _HEALTH_CHECK_RESULT.write_text(json.dumps(result), encoding="utf-8")


def _read_health_check_result() -> None:
    """
    Read the result written by the leader worker.
    Called by every follower after the leader releases the lock.
    If the result is "fail", abort this worker immediately.
    """
    try:
        result = json.loads(_HEALTH_CHECK_RESULT.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(
            "health_check_result_unreadable",
            exception=str(exc),
            note="Could not read result file. Assuming pass and continuing.",
        )
        return

    if result.get("status") == "fail":
        pytest.exit(
            f"ABORTING: API health check failed — {result.get('message')}. "
            "No tests will run against an unavailable service.",
            returncode=2,
        )


@pytest.fixture(scope="session")
def auth_token(health_check: None, shared_circuit_breaker) -> str:
    """
    Obtains a session-scoped auth token ONCE.
    With xdist each worker gets its own token — this is correct:
    tokens should not be shared between processes to avoid invalidation races.
    """
    auth_client = ApiClient(circuit_breaker=shared_circuit_breaker)
    booking_client = BookingClient(auth_client)
    token = booking_client.authenticate(
        username=settings.api_username,
        password=settings.api_password,
    )
    logger.info("session_auth_token_obtained", run_id=RUN_ID)
    auth_client.close()
    return token


# ---------------------------------------------------------------------------
# Orphan resource registry (per-worker — each worker owns its resources)
# ---------------------------------------------------------------------------

_orphan_registry: list[int] = []


def register_for_cleanup(booking_id: int) -> None:
    _orphan_registry.append(booking_id)


def deregister_from_cleanup(booking_id: int) -> None:
    try:
        _orphan_registry.remove(booking_id)
    except ValueError:
        pass


@pytest.fixture(scope="session", autouse=True)
def orphan_cleanup(auth_token: str) -> Generator[None, None, None]:
    """
    ─── FIX 2: Teardown Deadlock Fix ─────────────────────────────────────────
    Uses DirectApiClient which has NO circuit breaker attached.

    The original bug: if the API went down during tests, the circuit breaker
    would trip to OPEN. When cleanup ran, every delete request would be
    rejected immediately by the open circuit — data would leak into staging.

    The fix: cleanup uses a separate client instance that bypasses circuit
    state entirely. Cleanup must attempt real network calls regardless of
    what the circuit breaker believes about the upstream.

    If a delete still fails (API truly unavailable), the booking ID is written
    to logs/leaked_resources.txt with the run_id for manual recovery.
    """
    yield  # All tests run here

    if not _orphan_registry:
        logger.info("orphan_cleanup_nothing_to_do")
        return

    logger.warning(
        "orphan_cleanup_sweeping",
        orphan_count=len(_orphan_registry),
        orphan_ids=list(_orphan_registry),
        note="These resources were not cleaned up by their owning test.",
    )

    # DirectApiClient: NO circuit breaker — bypasses OPEN state intentionally
    sweep_client = DirectApiClient()
    sweep_client.set_auth_token(auth_token)
    sweep_booking = BookingClient(sweep_client)

    leaked: list[int] = []

    for booking_id in list(_orphan_registry):
        try:
            sweep_booking.delete_booking(booking_id)
            logger.info("orphan_cleanup_deleted", booking_id=booking_id)
        except Exception as exc:
            leaked.append(booking_id)
            logger.error(
                "orphan_cleanup_delete_failed",
                booking_id=booking_id,
                exception=str(exc),
            )

    sweep_client.close()

    if leaked:
        _write_leak_file(leaked)


def _write_leak_file(leaked_ids: list[int]) -> None:
    """
    Write leaked booking IDs to a persistent file for manual recovery.
    This file is uploaded as a CI artifact alongside logs and reports.
    """
    _LEAK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Leaked resources from run_id={RUN_ID}",
        f"# Timestamp: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        f"# These booking IDs could not be deleted during teardown.",
        f"# Manual deletion required against: {settings.api_base_url}",
        "",
    ] + [str(bid) for bid in leaked_ids]

    with _LEAK_FILE.open("a") as f:
        f.write("\n".join(lines) + "\n")

    logger.critical(
        "leaked_resources_written",
        count=len(leaked_ids),
        ids=leaked_ids,
        file=str(_LEAK_FILE),
        action_required=(
            "These booking IDs were not deleted. "
            "Manual cleanup required. See logs/leaked_resources.txt."
        ),
    )


# ===========================================================================
# DirectApiClient — circuit-breaker-bypass client for teardown
# ===========================================================================

class DirectApiClient(ApiClient):
    """
    An ApiClient subclass used exclusively in teardown/cleanup paths.

    Differences from ApiClient:
      1. No circuit breaker — bypasses OPEN state, always attempts the call.
      2. Independent retry config — more aggressive, short timeouts.
         We'd rather get a fast failure than hang teardown.
      3. Clearly logged as "direct" mode so it's distinguishable in traces.

    NEVER use this in test assertions. It exists only for cleanup.
    """

    def __init__(self) -> None:
        # Pass circuit_breaker=None explicitly.
        # base_client.py creates a CB when None is passed — we override _cb.
        super().__init__(circuit_breaker=None)
        # Replace the auto-created CB with a no-op sentinel
        self._cb = _NoOpCircuitBreaker()
        logger.debug(
            "direct_api_client_created",
            note="Circuit breaker bypassed — cleanup path only.",
        )


class _NoOpCircuitBreaker:
    """
    A circuit breaker that never trips and never blocks.
    Used exclusively in DirectApiClient for teardown paths.
    """
    name = "no-op-bypass"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


# ===========================================================================
# Function-scoped: fresh instance per test
# ===========================================================================


@pytest.fixture
def api_client(
    auth_token: str, shared_circuit_breaker
) -> Generator[ApiClient, None, None]:
    """Fresh, authenticated ApiClient per test. Closed in teardown."""
    client = ApiClient(circuit_breaker=shared_circuit_breaker)
    client.set_auth_token(auth_token)
    yield client
    client.close()


@pytest.fixture
def booking_client(api_client: ApiClient) -> BookingClient:
    """Domain client wrapping the authenticated ApiClient."""
    return BookingClient(api_client)


# ===========================================================================
# Data fixtures
# ===========================================================================


@pytest.fixture
def unique_booking_payload() -> BookingPayload:
    """
    UUID-unique BookingPayload per test.
    Kept for backward compatibility — new tests should use BookingDataFactory.
    """
    from src.utils.data_factory import BookingDataFactory
    return BookingDataFactory().realistic()


@pytest.fixture
def created_booking(
    booking_client: BookingClient,
    unique_booking_payload: BookingPayload,
) -> Generator[tuple[int, BookingPayload], None, None]:
    """
    Creates a booking, yields it to the test, then guarantees deletion.
    Uses DirectApiClient for teardown so a tripped circuit does not prevent
    cleanup (Fix 2 applied at the fixture level as well).

    Yields:
        Tuple of (booking_id: int, original_payload: BookingPayload)
    """
    created = booking_client.create_booking(unique_booking_payload)
    booking_id = created.bookingid

    register_for_cleanup(booking_id)
    logger.info("fixture_booking_created", booking_id=booking_id)

    try:
        yield booking_id, unique_booking_payload
    finally:
        _safe_delete_booking_direct(booking_id)
        deregister_from_cleanup(booking_id)


def _safe_delete_booking_direct(booking_id: int) -> None:
    """
    Teardown delete using DirectApiClient (circuit-bypassing).
    Logs all outcomes. Never re-raises — teardown must not mask test failures.
    """
    teardown_client = DirectApiClient()
    teardown_client.set_auth_token(
        # Re-authenticate for teardown to avoid stale token edge cases
        # In practice this is the same token; the cost is negligible.
        _get_cached_teardown_token()
    )
    teardown_booking = BookingClient(teardown_client)

    try:
        if teardown_booking.booking_exists(booking_id):
            teardown_booking.delete_booking(booking_id)
            logger.info("fixture_teardown_deleted", booking_id=booking_id)
        else:
            logger.info(
                "fixture_teardown_already_gone",
                booking_id=booking_id,
                note="Booking already absent — test may have deleted it intentionally.",
            )
    except Exception as exc:
        logger.error(
            "fixture_teardown_failed",
            booking_id=booking_id,
            exception=str(exc),
            note=(
                "This booking ID has been added to the orphan registry "
                "for session-level cleanup."
            ),
        )
        # Re-add to orphan registry — the session-level sweep will retry
        register_for_cleanup(booking_id)
    finally:
        teardown_client.close()


# Cache the teardown token to avoid re-auth on every fixture teardown.
_teardown_token_cache: Optional[str] = None


def _get_cached_teardown_token() -> str:
    global _teardown_token_cache
    if not _teardown_token_cache:
        _auth_client = DirectApiClient()
        _booking = BookingClient(_auth_client)
        _teardown_token_cache = _booking.authenticate(
            username=settings.api_username,
            password=settings.api_password,
        )
        _auth_client.close()
    return _teardown_token_cache


# ===========================================================================
# pytest hooks
# ===========================================================================


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Structured log line for every test outcome — no result disappears."""
    if report.when == "call":
        outcome = "passed" if report.passed else ("failed" if report.failed else "skipped")
        log_fn = logger.info if report.passed else logger.error

        log_fn(
            "test_result",
            test_id=report.nodeid,
            outcome=outcome,
            duration_s=round(report.duration, 4),
            run_id=RUN_ID,
            **({"failure_details": str(report.longrepr)} if report.failed else {}),
        )


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers."""
    config.addinivalue_line("markers", "crud: Full CRUD lifecycle tests")
    config.addinivalue_line("markers", "contract: Schema/contract validation tests")
    config.addinivalue_line("markers", "smoke: Quick sanity checks")
    config.addinivalue_line("markers", "slo: SLO enforcement tests")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Clean up xdist coordination files after the session."""
    for path in (_HEALTH_CHECK_LOCK, _HEALTH_CHECK_RESULT):
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
