"""Tests fuer den prozessinternen RateLimiter (rate_limiter.py)."""

import pytest

import rate_limiter
from rate_limiter import RateLimiter, RateLimitExceeded


@pytest.fixture
def clock(monkeypatch):
    """Ersetzt time.monotonic durch eine steuerbare Uhr."""
    state = {"t": 1000.0}
    monkeypatch.setattr(rate_limiter.time, "monotonic", lambda: state["t"])
    return state


async def test_allows_up_to_limit(clock):
    rl = RateLimiter(3)
    await rl.acquire()
    await rl.acquire()
    await rl.acquire()

    with pytest.raises(RateLimitExceeded) as excinfo:
        await rl.acquire()

    assert excinfo.value.limit == 3
    assert excinfo.value.retry_after >= 1


async def test_window_frees_up_after_60s(clock):
    rl = RateLimiter(3)
    for _ in range(3):
        await rl.acquire()

    # Innerhalb des Fensters weiterhin blockiert.
    clock["t"] += 59
    with pytest.raises(RateLimitExceeded):
        await rl.acquire()

    # Nach Ablauf des Fensters (>= 60 s) wieder frei.
    clock["t"] = 1060.0
    for _ in range(3):
        await rl.acquire()
    with pytest.raises(RateLimitExceeded):
        await rl.acquire()


async def test_retry_after_is_positive(clock):
    rl = RateLimiter(1)
    await rl.acquire()
    with pytest.raises(RateLimitExceeded) as excinfo:
        await rl.acquire()
    assert excinfo.value.retry_after >= 1


async def test_disabled_when_limit_non_positive(clock):
    rl = RateLimiter(0)
    for _ in range(100):
        await rl.acquire()  # darf nie werfen
