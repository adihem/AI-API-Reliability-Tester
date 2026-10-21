"""Metrics on hand-built results with known answers, plus report output."""

import csv
import json
from datetime import datetime, timedelta, timezone

import pytest

from tester.metrics import compute_metrics, percentile
from tester.models import APIResponse, ExperimentResult, FailureType, RequestResult
from tester.reporting import write_report
from test_runner import cfg

T0 = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def attempt(task, n, start, latency, failure=None, tokens=None):
    ok = failure in (None, FailureType.VALIDATION_ERROR)
    resp = APIResponse(success=ok, text="x" if ok else None, http_status=200 if ok else 429,
                       latency_seconds=latency, total_tokens=tokens, input_tokens=tokens and tokens - 2,
                       output_tokens=tokens and 2, error_type=None if ok else failure)
    s = T0 + timedelta(seconds=start)
    return RequestResult(task_id=task, attempt_number=n, started_at=s, finished_at=s + timedelta(seconds=latency),
                         response=resp, failure_type=failure, validation_passed=True if failure is None else None)


@pytest.fixture
def result():
    """4 tasks, 7 attempts, 60s run:
    a: pass first try                         (1 attempt)
    b: TIMEOUT -> pass (waits 1s)             (2) recovered
    c: RATE_LIMIT -> RATE_LIMIT -> pass       (3) recovered
    d: wrong answer                           (1) final failure, not retried
    """
    TO, RL, VE = FailureType.TIMEOUT, FailureType.RATE_LIMIT, FailureType.VALIDATION_ERROR
    attempts = [
        attempt("a", 1, 0, 1.0, tokens=10),
        attempt("b", 1, 2, 3.0, TO), attempt("b", 2, 6, 1.0, tokens=12),
        attempt("c", 1, 8, 0.5, RL), attempt("c", 2, 9.5, 0.5, RL), attempt("c", 3, 12, 2.0, tokens=14),
        attempt("d", 1, 20, 4.0, VE, tokens=8),
    ]
    return ExperimentResult(id=7, config=cfg(4, enabled=True, max_attempts=3), dataset_version="1",
                            dataset_hash="abc", app_version="0.1.0", started_at=T0,
                            finished_at=T0 + timedelta(seconds=60), attempts=attempts)


def test_success_and_recovery(result):
    m = compute_metrics(result)
    assert (m.tasks, m.attempts, m.avg_attempts) == (4, 7, 1.75)
    assert (m.initial_successes, m.final_successes, m.recovered) == (1, 3, 2)
    assert (m.initial_success_rate, m.final_success_rate, m.failure_rate) == (0.25, 0.75, 0.25)
    assert m.retry_recovery_rate == 2 / 3


def test_failure_distributions(result):
    m = compute_metrics(result)
    assert m.final_failures == {"VALIDATION_ERROR": 1}
    assert m.attempt_failures == {"RATE_LIMIT": 2, "TIMEOUT": 1, "VALIDATION_ERROR": 1}


def test_latency(result):
    m = compute_metrics(result)
    assert m.attempt_latency.mean == pytest.approx(12 / 7)
    assert m.attempt_latency.median == 1.0
    assert m.attempt_latency.p95 == pytest.approx(3.7)
    assert (m.attempt_latency.min, m.attempt_latency.max) == (0.5, 4.0)
    assert m.task_latency.mean == pytest.approx(4.0)
    assert m.task_latency.max == 6.0


def test_tokens_and_throughput(result):
    m = compute_metrics(result)
    assert m.total_tokens == 44 and m.input_tokens == 36 and m.output_tokens == 8
    assert m.tokens_per_task == 11
    assert m.duration_seconds == 60 and m.throughput_tasks_per_min == 4


def test_no_retry_needed_gives_none_recovery(result):
    only_a = result.model_copy(update={"attempts": result.attempts[:1]})
    m = compute_metrics(only_a)
    assert m.retry_recovery_rate is None and m.final_failures == {} and m.attempt_latency.p95 == 1.0


def test_no_tokens_reported():
    r = ExperimentResult(config=cfg(1), dataset_version="1", dataset_hash="h", app_version="v", started_at=T0,
                         attempts=[attempt("a", 1, 0, 1.0, FailureType.TIMEOUT)])
    m = compute_metrics(r)
    assert m.total_tokens is None and m.tokens_per_task is None
    assert m.duration_seconds is None and m.throughput_tasks_per_min is None


def test_empty_rejected():
    r = ExperimentResult(config=cfg(1), dataset_version="1", dataset_hash="h", app_version="v", started_at=T0)
    with pytest.raises(ValueError):
        compute_metrics(r)


@pytest.mark.parametrize("values,p,expected", [([5.0], 95, 5.0), ([1.0, 2.0], 50, 1.5), ([0, 10], 95, 9.5)])
def test_percentile(values, p, expected):
    assert percentile(values, p) == pytest.approx(expected)




def test_write_report(result, tmp_path):
    out = write_report(result, base=tmp_path)
    assert out.name == "2026-10-21_t_007"
    expected = {"config.yaml", "results.csv", "results.json", "summary.json", "report.md", "figures"}
    assert {p.name for p in out.iterdir()} == expected
    figures = {p.name for p in (out / "figures").iterdir()}
    assert figures == {"success_rate.png", "latency_distribution.png", "failure_categories.png", "retry_recovery.png"}
    assert all((out / "figures" / f).stat().st_size > 1000 for f in figures)

    rows = list(csv.DictReader((out / "results.csv").open(encoding="utf-8")))
    assert len(rows) == 7 and rows[1]["failure_type"] == "TIMEOUT" and rows[2]["attempt_number"] == "2"

    summary = json.loads((out / "summary.json").read_text())
    assert summary["dataset_hash"] == "abc" and summary["metrics"]["recovered"] == 2
    assert ExperimentResult.model_validate_json((out / "results.json").read_text()) == result

    md = (out / "report.md").read_text(encoding="utf-8")
    for section in ["## 1. Experiment information", "## 6. Latency", "## 8. Retry recovery", "## 12. Limitations"]:
        assert section in md
    assert "Initial success rate | 25.0% (1/4)" in md
    assert "Retries recovered 2 of 3 initially failed tasks (66.7%)" in md
    assert "![success_rate.png](figures/success_rate.png)" in md


def test_report_with_no_failures(result, tmp_path):
    only_a = result.model_copy(update={"attempts": result.attempts[:1]})
    md = (write_report(only_a, base=tmp_path) / "report.md").read_text(encoding="utf-8")
    assert "| (none) | 0 | 0 |" in md and "nothing to recover" in md
