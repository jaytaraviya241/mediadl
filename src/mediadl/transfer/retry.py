"""Retry classification and backoff (spec section 7.8).

Full jitter, a hard attempt budget, and an explicit park threshold: when a
provider asks us to wait longer than the threshold, the job leaves the worker
instead of sleeping on it.
"""
from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from mediadl.errors import (
    LocatorExpiredError,
    MediaDLError,
    RateLimited,
    RetriesExhaustedError,
)
from mediadl.obs.logging import get_logger

log = get_logger(__name__)
T = TypeVar('T')


@dataclass(frozen=True, slots=True)
class RetryBudget:
    max_attempts: int = 5
    base_s: float = 1.0
    cap_s: float = 60.0
    park_threshold_s: float = 900.0


# --- decisions ------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Retry:
    after: float | None = None


@dataclass(frozen=True, slots=True)
class Fail:
    error: MediaDLError


@dataclass(frozen=True, slots=True)
class RateLimit:
    until: float


@dataclass(frozen=True, slots=True)
class Refresh:
    pass


Decision = Retry | Fail | RateLimit | Refresh


def transfer_error_class(exc: BaseException) -> Decision:
    """Map a transfer exception onto a retry decision."""
    if isinstance(exc, LocatorExpiredError):
        return Refresh()
    if isinstance(exc, RateLimited):
        after = exc.detail.get('retry_after') or exc.resume_at
        delay = float(after) if isinstance(after, (int, float)) else 60.0
        return RateLimit(time.monotonic() + delay)
    if isinstance(exc, MediaDLError):
        # The error decides: a 404 or a policy denial repeats identically.
        return Retry() if exc.retryable else Fail(exc)
    return Fail(_wrap_unexpected(exc))


def connector_error_class(exc: BaseException) -> Decision:
    if isinstance(exc, RateLimited):
        after = exc.detail.get('retry_after') or exc.resume_at
        delay = float(after) if isinstance(after, (int, float)) else 60.0
        return RateLimit(time.monotonic() + delay)
    if isinstance(exc, MediaDLError):
        return Retry() if exc.retryable else Fail(exc)
    return Fail(_wrap_unexpected(exc))


def _wrap_unexpected(exc: BaseException) -> MediaDLError:
    from mediadl.errors import InternalError

    return InternalError.from_exc(exc)


def backoff_delay(attempt: int, budget: RetryBudget, *, rng: random.Random | None = None) -> float:
    """Exponential backoff with full jitter."""
    rand = rng or random
    ceiling = min(budget.cap_s, budget.base_s * (2**attempt))
    return rand.uniform(0, ceiling)


async def with_retries(
    operation: Callable[[], Awaitable[T]],
    *,
    classify: Callable[[BaseException], Decision] = transfer_error_class,
    budget: RetryBudget | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    label: str = 'operation',
) -> T:
    budget = budget or RetryBudget()
    last: BaseException | None = None
    for attempt in range(budget.max_attempts):
        try:
            return await operation()
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except BaseException as exc:
            last = exc
            decision = classify(exc)
            match decision:
                case Fail(error=error):
                    raise error from exc
                case Refresh():
                    raise LocatorExpiredError('locator expired', label=label) from exc
                case RateLimit(until=until):
                    wait = max(0.0, until - time.monotonic())
                    if wait > budget.park_threshold_s:
                        raise RateLimited(
                            'provider asked for a long wait; parking the job',
                            resume_at=time.time() + wait,
                        ) from exc
                    log.info('rate_limited', label=label, wait_s=round(wait, 2))
                    await sleep(wait)
                case Retry(after=after):
                    if attempt >= budget.max_attempts - 1:
                        break
                    delay = after if after is not None else backoff_delay(attempt, budget)
                    log.info(
                        'retrying', label=label, attempt=attempt + 1,
                        delay_s=round(delay, 2), reason=type(exc).__name__,
                    )
                    await sleep(delay)
    raise RetriesExhaustedError(
        f'{label} failed after {budget.max_attempts} attempts',
        attempts=budget.max_attempts,
        last_error=type(last).__name__ if last else None,
    ) from last
