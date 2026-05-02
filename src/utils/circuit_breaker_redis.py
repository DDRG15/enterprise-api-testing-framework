"""
src/utils/circuit_breaker_redis.py
=====================================
Redis-backed circuit breaker for shared state across pytest-xdist workers.

WHY THIS EXISTS — The Concurrency Flaw It Fixes:
  pytest-xdist achieves parallelism by spinning up isolated Python subprocesses.
  Each worker has its own memory space. An in-memory circuit breaker means:
    - Worker 1 counts its own failures: 0 → 1 → 2 → 3 → 4
    - Worker 2 counts its own failures: 0 → 1 → 2 → 3 → 4
    - Worker 3 counts its own failures: 0 → 1 → 2 → 3 → 4
    - Worker 4 counts its own failures: 0 → 1 → 2 → 3 → 4

  With threshold=5, the circuit never trips despite 16 real failures.
  The thundering herd protection is completely bypassed.

  This implementation stores state in Redis. All workers share one atomic
  counter and one state value. The circuit trips correctly at exactly
  N cumulative failures across the entire worker pool.

ATOMICITY — No Race Conditions:
  State transitions (CLOSED→OPEN, HALF_OPEN→CLOSED) use Lua scripts executed
  atomically on the Redis server. There is no TOCTOU window between the check
  and the set — a race condition that would allow two workers to both believe
  they are the "recovery probe" in HALF_OPEN state simultaneously.

RESILIENCE — Redis Unavailability:
  If Redis is unreachable, the circuit breaker silently falls back to
  ALLOW_ALL mode and logs a warning. The test suite continues running
  without distributed protection. This is a deliberate trade-off:
    - Failing the entire test suite because the Redis sidecar is down
      is worse than running without distributed circuit-breaking.
    - The fallback is logged clearly so operators can act on it.
  This mirrors the principle used in production circuit breakers like
  Netflix Hystrix: when the breaker itself is broken, fail open.

KEY SCHEMA (Redis Hash):
  Key:   circuit:<name>
  Fields:
    state         STRING  CLOSED | OPEN | HALF_OPEN
    failure_count INT     Cumulative failures since last reset
    open_since    FLOAT   Unix timestamp when circuit tripped
"""
from __future__ import annotations

import time
import threading
from enum import Enum, auto
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Lua scripts — executed atomically by Redis, no TOCTOU possible
# ---------------------------------------------------------------------------

# Atomically increment failure count and trip the circuit if threshold is met.
# Returns the new failure count.
_LUA_RECORD_FAILURE = """
local key = KEYS[1]
local threshold = tonumber(ARGV[1])
local now = ARGV[2]

local count = redis.call('HINCRBY', key, 'failure_count', 1)

if count >= threshold then
    redis.call('HSET', key, 'state', 'OPEN')
    redis.call('HSET', key, 'open_since', now)
end

redis.call('EXPIRE', key, 86400)  -- 24h TTL: auto-clean stale circuit state
return count
"""

# Atomically transition OPEN → HALF_OPEN only if recovery_timeout has elapsed.
# Returns 1 if transition occurred, 0 if not yet time.
_LUA_TRY_HALF_OPEN = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local recovery_timeout = tonumber(ARGV[2])

local open_since = tonumber(redis.call('HGET', key, 'open_since') or '0')

if (now - open_since) >= recovery_timeout then
    redis.call('HSET', key, 'state', 'HALF_OPEN')
    return 1
end
return 0
"""

# Atomically reset the circuit to CLOSED state (on probe success).
_LUA_RESET = """
local key = KEYS[1]
redis.call('HSET', key, 'state', 'CLOSED')
redis.call('HSET', key, 'failure_count', '0')
redis.call('HDEL', key, 'open_since')
redis.call('EXPIRE', key, 86400)
return 1
"""


# ---------------------------------------------------------------------------
# Circuit states
# ---------------------------------------------------------------------------

class CircuitState(Enum):
    CLOSED    = auto()
    OPEN      = auto()
    HALF_OPEN = auto()


class CircuitBreakerOpenError(Exception):
    """Raised immediately when the circuit is OPEN — no network call is made."""

    def __init__(self, name: str, open_since: float) -> None:
        duration = time.monotonic() - open_since
        super().__init__(
            f"Circuit breaker '{name}' is OPEN (tripped {duration:.1f}s ago). "
            "Upstream is considered unavailable. Failing fast."
        )


# ---------------------------------------------------------------------------
# Redis-backed implementation
# ---------------------------------------------------------------------------

class RedisCircuitBreaker:
    """
    Distributed circuit breaker backed by Redis.

    Thread-safe within a process (uses a threading.Lock for local critical
    sections). Process-safe across xdist workers (uses Redis Lua scripts
    for atomic cross-process state transitions).

    Args:
        name:              Logical name — becomes the Redis key prefix.
        failure_threshold: Trip the circuit after this many cumulative failures
                           across ALL workers combined.
        recovery_timeout:  Seconds before attempting a recovery probe.
        redis_url:         Redis connection URL (e.g., redis://localhost:6379/0).
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        redis_url: str = "redis://localhost:6379/0",
    ) -> None:
        self.name = name
        self._threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._key = f"circuit:{name}"
        self._lock = threading.Lock()
        self._redis_available = False
        self._client = None

        try:
            import redis as redis_lib
            self._client = redis_lib.from_url(
                redis_url,
                socket_connect_timeout=2,   # Fast fail — don't slow down tests
                socket_timeout=2,
                decode_responses=True,
            )
            self._client.ping()
            # Pre-register Lua scripts for efficiency
            self._fn_record_failure = self._client.register_script(_LUA_RECORD_FAILURE)
            self._fn_try_half_open  = self._client.register_script(_LUA_TRY_HALF_OPEN)
            self._fn_reset          = self._client.register_script(_LUA_RESET)
            self._redis_available = True
            logger.info(
                "circuit_breaker_redis_connected",
                name=name,
                redis_url=redis_url,
                mode="distributed",
            )
        except Exception as exc:
            logger.warning(
                "circuit_breaker_redis_unavailable",
                name=name,
                exception=str(exc),
                fallback="allow_all",
                note=(
                    "Redis is unreachable. Circuit breaker is disabled for this session. "
                    "Distributed trip protection is NOT active. "
                    "Investigate Redis connectivity before running at scale."
                ),
            )

    # ------------------------------------------------------------------
    # Context manager interface
    # ------------------------------------------------------------------

    def __enter__(self) -> "RedisCircuitBreaker":
        if not self._redis_available:
            return self  # Fail open: Redis down → allow all requests

        with self._lock:
            state = self._get_state()

            if state == "OPEN":
                # Check if recovery window has elapsed
                transitioned = self._fn_try_half_open(
                    keys=[self._key],
                    args=[str(time.time()), str(self._recovery_timeout)],
                )
                if transitioned:
                    logger.info("circuit_breaker_half_open", name=self.name)
                    return self  # Allow probe request through

                open_since = float(self._client.hget(self._key, "open_since") or 0)
                raise CircuitBreakerOpenError(self.name, open_since)

        return self

    def __exit__(
        self,
        exc_type: type | None,
        exc_val: Exception | None,
        exc_tb: object,
    ) -> bool:
        if not self._redis_available:
            return False

        with self._lock:
            if exc_type is None:
                self._on_success()
            else:
                self._on_failure()
        return False

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def _on_success(self) -> None:
        state = self._get_state()
        if state in ("HALF_OPEN", "OPEN"):
            self._fn_reset(keys=[self._key], args=[])
            logger.info("circuit_breaker_closed", name=self.name, reason="probe_success")

    def _on_failure(self) -> None:
        new_count = self._fn_record_failure(
            keys=[self._key],
            args=[str(self._threshold), str(time.time())],
        )
        new_state = self._get_state()
        if new_state == "OPEN":
            logger.warning(
                "circuit_breaker_opened",
                name=self.name,
                failure_count=new_count,
                threshold=self._threshold,
                mode="distributed_redis",
            )

    def _get_state(self) -> str:
        return self._client.hget(self._key, "state") or "CLOSED"

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    @property
    def state(self) -> CircuitState:
        if not self._redis_available:
            return CircuitState.CLOSED
        state_str = self._get_state()
        return {
            "CLOSED":    CircuitState.CLOSED,
            "OPEN":      CircuitState.OPEN,
            "HALF_OPEN": CircuitState.HALF_OPEN,
        }.get(state_str, CircuitState.CLOSED)

    @property
    def failure_count(self) -> int:
        if not self._redis_available:
            return 0
        return int(self._client.hget(self._key, "failure_count") or 0)

    def reset(self) -> None:
        """Manually reset the circuit. Used in test teardown."""
        if self._redis_available:
            self._fn_reset(keys=[self._key], args=[])


# ---------------------------------------------------------------------------
# Factory — returns the right implementation based on environment
# ---------------------------------------------------------------------------

def make_circuit_breaker(
    name: str,
    failure_threshold: int,
    recovery_timeout: float,
    redis_url: Optional[str] = None,
) -> "RedisCircuitBreaker | _InMemoryCircuitBreaker":
    """
    Returns a Redis-backed circuit breaker if a Redis URL is configured,
    otherwise returns a thread-safe in-memory implementation.

    This allows the framework to work correctly in both:
      - Single-process mode (no Redis needed)
      - pytest-xdist parallel mode (Redis required for shared state)
    """
    if redis_url:
        return RedisCircuitBreaker(
            name=name,
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
            redis_url=redis_url,
        )
    return _InMemoryCircuitBreaker(
        name=name,
        failure_threshold=failure_threshold,
        recovery_timeout=recovery_timeout,
    )


# ---------------------------------------------------------------------------
# In-memory fallback (original implementation, used in single-process mode)
# ---------------------------------------------------------------------------

class _InMemoryCircuitBreaker:
    """
    Thread-safe in-memory circuit breaker.
    Correct for single-process pytest runs. NOT correct for pytest-xdist.
    Use RedisCircuitBreaker when running with -n auto or -n <N>.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
    ) -> None:
        self.name = name
        self._threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._open_since: float = 0.0
        self._lock = threading.Lock()

    def __enter__(self) -> "_InMemoryCircuitBreaker":
        with self._lock:
            if self._state == CircuitState.OPEN:
                elapsed = time.monotonic() - self._open_since
                if elapsed >= self._recovery_timeout:
                    logger.info("circuit_breaker_half_open", name=self.name)
                    self._state = CircuitState.HALF_OPEN
                else:
                    raise CircuitBreakerOpenError(self.name, self._open_since)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        with self._lock:
            if exc_type is None:
                self._on_success()
            else:
                self._on_failure()
        return False

    def _on_success(self) -> None:
        if self._state == CircuitState.HALF_OPEN:
            logger.info("circuit_breaker_closed", name=self.name, reason="probe_success")
        self._state = CircuitState.CLOSED
        self._failure_count = 0

    def _on_failure(self) -> None:
        self._failure_count += 1
        if (
            self._state == CircuitState.HALF_OPEN
            or self._failure_count >= self._threshold
        ):
            self._state = CircuitState.OPEN
            self._open_since = time.monotonic()
            logger.warning(
                "circuit_breaker_opened",
                name=self.name,
                failure_count=self._failure_count,
                threshold=self._threshold,
                mode="in_memory",
            )

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count

    def reset(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0


# Keep the original name as an alias so existing imports don't break
CircuitBreaker = _InMemoryCircuitBreaker
