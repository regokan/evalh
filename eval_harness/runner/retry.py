from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from eval_harness.core.config import RetryPolicy
from eval_harness.core.errors import RetriableError

T = TypeVar("T")


async def with_retry(call: Callable[[], Awaitable[T]], policy: RetryPolicy) -> T:
    attempts = max(1, policy.max_attempts)
    for attempt in range(attempts):
        try:
            return await call()
        except RetriableError:
            if attempt + 1 == attempts:
                raise
            await asyncio.sleep(policy.backoff_seconds * (2**attempt))
    raise RuntimeError("unreachable")
