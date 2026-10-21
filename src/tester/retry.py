"""Retry decisions and backoff. Independent of the API client: works on failure types only."""

import time
from collections.abc import Callable

from tester.models import FailureType, RequestResult, RetryConfig


class RetryPolicy:
    def __init__(self, config: RetryConfig, sleep: Callable[[float], None] | None = None):
        self.config = config
        self.sleep = sleep or time.sleep  # injectable so tests don't actually wait

    def should_retry(self, failure_type: FailureType | None, attempt_number: int) -> bool:
        """After attempt `attempt_number` ended with `failure_type`, try again?"""
        return (
            self.config.enabled
            and failure_type is not None
            and failure_type in self.config.retry_on
            and attempt_number < self.config.max_attempts
        )

    def delay(self, retry_number: int) -> float:
        """Wait before retry `retry_number` (1-based): base, 2*base, 4*base, ... capped."""
        # ponytail: no jitter and no Retry-After header; fine for one sequential client.
        # Add jitter with concurrency, Retry-After if 429s dominate.
        return min(self.config.backoff_seconds * 2 ** (retry_number - 1), self.config.max_backoff_seconds)


def recovered(attempts: list[RequestResult]) -> bool:
    """First attempt failed but a later one succeeded."""
    return len(attempts) > 1 and not attempts[0].success and attempts[-1].success
