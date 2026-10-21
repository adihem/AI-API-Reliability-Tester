"""Coordinate an experiment: dataset -> client -> validators -> results.

Three levels:
  run_attempt     one API call + validation
  run_test_case   one task: attempts + retries, as the RetryPolicy allows
  run_experiment  the whole dataset, sequentially
"""

import itertools
import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timezone

from tester import __version__, database
from tester.api_client import APIClient
from tester.models import APIResponse, Dataset, ExperimentConfig, ExperimentResult, FailureType, RequestResult, Task
from tester.retry import RetryPolicy
from tester.validators import validate

log = logging.getLogger(__name__)

Progress = Callable[[Task, list[RequestResult]], None]


def now() -> datetime:
    return datetime.now(timezone.utc)


def run_attempt(client: APIClient, task: Task, attempt_number: int, config: ExperimentConfig) -> RequestResult:
    started = now()
    try:
        resp = client.send(task.prompt)
    except Exception as e:  # a client bug must not abort a 30-task experiment
        log.exception("client crashed on task %s", task.id)
        resp = APIResponse(success=False, latency_seconds=(now() - started).total_seconds(),
                           error_type=FailureType.UNKNOWN_ERROR, error_message=f"{type(e).__name__}: {e}")
    finished = now()

    if resp.success:
        v = validate(task, resp.text)
        passed, message, failure = v.passed, v.message or None, v.failure_type
    else:
        passed, message, failure = None, None, resp.error_type

    return RequestResult(
        task_id=task.id,
        attempt_number=attempt_number,
        started_at=started,
        finished_at=finished,
        response=resp,
        validation_passed=passed,
        validation_message=message,
        failure_type=failure,
        retryable=failure in config.retry.retry_on,
    )


def run_test_case(client: APIClient, task: Task, config: ExperimentConfig,
                  policy: RetryPolicy | None = None) -> list[RequestResult]:
    """Attempt a task until it succeeds or the retry policy says stop. Returns every attempt."""
    policy = policy or RetryPolicy(config.retry)
    attempts = []
    for n in itertools.count(1):
        attempt = run_attempt(client, task, n, config)
        attempts.append(attempt)
        if not policy.should_retry(attempt.failure_type, n):
            return attempts
        wait = policy.delay(n)
        log.info("%s attempt %d failed (%s), retrying in %.1fs", task.id, n, attempt.failure_type, wait)
        policy.sleep(wait)


def select_tasks(config: ExperimentConfig, dataset: Dataset, limit: int | None = None) -> list[Task]:
    n = config.test.requests if limit is None else min(limit, config.test.requests)
    return dataset.tasks[:n]


def run_experiment(
    config: ExperimentConfig,
    dataset: Dataset,
    client: APIClient,
    limit: int | None = None,
    progress: Progress | None = None,
    conn: sqlite3.Connection | None = None,
    policy: RetryPolicy | None = None,
) -> ExperimentResult:
    """Run tasks in order. With `conn`, each task's attempts are committed as soon as it finishes."""
    policy = policy or RetryPolicy(config.retry)
    result = ExperimentResult(
        config=config,
        dataset_version=dataset.version,
        dataset_hash=dataset.sha256,
        app_version=__version__,
        task_limit=limit,
        started_at=now(),
    )
    if conn is not None:
        result.id = database.create_experiment(conn, result)
    # ponytail: sequential only; concurrency (asyncio + httpx.AsyncClient) once this is stable.
    last_start = None
    for task in select_tasks(config, dataset, limit):
        if last_start is not None:
            gap = config.test.min_interval_seconds - (time.monotonic() - last_start)
            if gap > 0:
                policy.sleep(gap)
        last_start = time.monotonic()
        attempts = run_test_case(client, task, config, policy)
        result.attempts.extend(attempts)
        if conn is not None:
            database.save_attempts(conn, result.id, attempts)
        if progress:
            progress(task, attempts)
    result.finished_at = now()
    if conn is not None:
        database.finish_experiment(conn, result.id, result.finished_at)
    return result
