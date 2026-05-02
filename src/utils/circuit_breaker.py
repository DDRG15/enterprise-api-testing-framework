"""
src/utils/circuit_breaker.py
==============================
Public interface for the circuit breaker subsystem.

Re-exports from circuit_breaker_redis so all existing imports continue
to work unchanged while the underlying implementation has been upgraded
to support distributed state via Redis.

Import from here, not from circuit_breaker_redis directly:
    from src.utils.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError
"""
from src.utils.circuit_breaker_redis import (
    CircuitBreaker,           # In-memory (single-process mode)
    CircuitBreakerOpenError,
    CircuitState,
    RedisCircuitBreaker,
    make_circuit_breaker,     # Factory — returns correct impl based on config
)

__all__ = [
    "CircuitBreaker",
    "CircuitBreakerOpenError",
    "CircuitState",
    "RedisCircuitBreaker",
    "make_circuit_breaker",
]
