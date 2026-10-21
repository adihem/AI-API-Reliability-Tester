"""Reliability metrics computed from stored attempts.

Definitions (per task = all attempts at one dataset task):
  initial success   first attempt succeeded
  final success     last attempt succeeded (after any retries)
  recovered         first attempt failed, last succeeded
  retry recovery    recovered / tasks whose first attempt failed
  attempt latency   API time of one attempt
  task latency      first attempt start -> last attempt end (includes backoff waits)
  P95               linear interpolation, same as numpy/pandas default
"""

import statistics
from collections import Counter

from pydantic import BaseModel

from tester.models import ExperimentResult, RequestResult
from tester.retry import recovered


class LatencyStats(BaseModel):
    mean: float
    median: float
    p95: float
    min: float
    max: float


class Metrics(BaseModel):
    tasks: int
    attempts: int
    avg_attempts: float

    initial_successes: int
    final_successes: int
    recovered: int
    initial_success_rate: float
    final_success_rate: float
    failure_rate: float
    retry_recovery_rate: float | None

    attempt_latency: LatencyStats
    task_latency: LatencyStats

    final_failures: dict[str, int]
    attempt_failures: dict[str, int]

    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    tokens_per_task: float | None

    duration_seconds: float | None
    throughput_tasks_per_min: float | None


def percentile(values: list[float], pct: float) -> float:
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[round(pct) - 1]


def latency_stats(values: list[float]) -> LatencyStats:
    return LatencyStats(mean=statistics.fmean(values), median=statistics.median(values),
                        p95=percentile(values, 95), min=min(values), max=max(values))


def group_by_task(attempts: list[RequestResult]) -> dict[str, list[RequestResult]]:
    groups: dict[str, list[RequestResult]] = {}
    for a in attempts:
        groups.setdefault(a.task_id, []).append(a)
    return groups


def token_sum(attempts: list[RequestResult], field: str) -> int | None:
    values = [getattr(a.response, field) for a in attempts if getattr(a.response, field) is not None]
    return sum(values) if values else None


def compute_metrics(result: ExperimentResult) -> Metrics:
    if not result.attempts:
        raise ValueError("experiment has no attempts")
    tasks = group_by_task(result.attempts)
    n = len(tasks)
    initial = sum(a[0].success for a in tasks.values())
    final = sum(a[-1].success for a in tasks.values())
    rescued = sum(recovered(a) for a in tasks.values())
    initially_failed = n - initial

    total_tokens = token_sum(result.attempts, "total_tokens")
    duration = (result.finished_at - result.started_at).total_seconds() if result.finished_at else None

    return Metrics(
        tasks=n,
        attempts=len(result.attempts),
        avg_attempts=len(result.attempts) / n,
        initial_successes=initial,
        final_successes=final,
        recovered=rescued,
        initial_success_rate=initial / n,
        final_success_rate=final / n,
        failure_rate=(n - final) / n,
        retry_recovery_rate=rescued / initially_failed if initially_failed else None,
        attempt_latency=latency_stats([a.response.latency_seconds for a in result.attempts]),
        task_latency=latency_stats([(a[-1].finished_at - a[0].started_at).total_seconds() for a in tasks.values()]),
        final_failures=dict(Counter(str(a[-1].failure_type) for a in tasks.values() if not a[-1].success).most_common()),
        attempt_failures=dict(Counter(str(a.failure_type) for a in result.attempts if not a.success).most_common()),
        input_tokens=token_sum(result.attempts, "input_tokens"),
        output_tokens=token_sum(result.attempts, "output_tokens"),
        total_tokens=total_tokens,
        tokens_per_task=total_tokens / n if total_tokens is not None else None,
        duration_seconds=duration,
        throughput_tasks_per_min=n / duration * 60 if duration else None,
    )
