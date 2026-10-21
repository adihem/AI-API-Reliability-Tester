"""Retry engine tests. Fake sleep records waits instead of sleeping."""

import pytest
from typer.testing import CliRunner

from tester import cli, database
from tester.models import FailureType, RetryConfig
from tester.retry import RetryPolicy, recovered
from tester.runner import run_experiment, run_test_case
from test_runner import ROOT, cfg, err, ok, tasks

T, RL, AUTH, BAD, SRV = (FailureType.TIMEOUT, FailureType.RATE_LIMIT, FailureType.AUTHENTICATION_ERROR,
                         FailureType.BAD_REQUEST, FailureType.SERVER_ERROR)


class SequenceClient:
    """Returns the scripted responses in order, regardless of prompt."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def send(self, prompt):
        self.calls += 1
        return self.responses.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def policy(max_attempts=3, enabled=True, **kw):
    waits = []
    p = RetryPolicy(RetryConfig(enabled=enabled, max_attempts=max_attempts, **kw), sleep=waits.append)
    return p, waits




@pytest.mark.parametrize(
    "failure,attempt,expected",
    [
        (T, 1, True), (RL, 1, True), (SRV, 2, True), (FailureType.CONNECTION_ERROR, 1, True),
        (T, 3, False),
        (AUTH, 1, False), (BAD, 1, False),
        (FailureType.VALIDATION_ERROR, 1, False), (FailureType.FORMAT_ERROR, 1, False),
        (None, 1, False),
    ],
)
def test_should_retry(failure, attempt, expected):
    p, _ = policy()
    assert p.should_retry(failure, attempt) is expected


def test_disabled_never_retries():
    p, _ = policy(max_attempts=1, enabled=False)
    assert not p.should_retry(T, 1)


def test_retry_on_is_configurable():
    p, _ = policy(retry_on=["FORMAT_ERROR"])
    assert p.should_retry(FailureType.FORMAT_ERROR, 1)
    assert not p.should_retry(T, 1)


def test_exponential_backoff_capped():
    p, _ = policy(backoff_seconds=1, max_backoff_seconds=5)
    assert [p.delay(n) for n in range(1, 6)] == [1, 2, 4, 5, 5]
    p, _ = policy(backoff_seconds=0)
    assert p.delay(3) == 0




def run(client, p, task_cfg=None):
    return run_test_case(client, tasks(1).tasks[0], task_cfg or cfg(enabled=True, max_attempts=3), p)


def test_fail_then_success():
    p, waits = policy()
    attempts = run(SequenceClient(err(T), ok("BUG")), p)
    assert [a.success for a in attempts] == [False, True]
    assert [a.attempt_number for a in attempts] == [1, 2]
    assert waits == [1] and recovered(attempts)


def test_fail_fail_success():
    p, waits = policy()
    attempts = run(SequenceClient(err(RL, 429), err(SRV, 503), ok("BUG")), p)
    assert [a.failure_type for a in attempts] == [RL, SRV, None]
    assert waits == [1, 2] and recovered(attempts)


def test_fail_fail_fail_gives_up():
    p, waits = policy()
    client = SequenceClient(err(T), err(T), err(T), ok("never reached"))
    attempts = run(client, p)
    assert len(attempts) == 3 and client.calls == 3
    assert waits == [1, 2]
    assert not recovered(attempts)


def test_auth_error_not_retried():
    p, waits = policy()
    client = SequenceClient(err(AUTH, 401), ok("BUG"))
    attempts = run(client, p)
    assert len(attempts) == 1 and client.calls == 1 and waits == []


def test_wrong_answer_not_retried_by_default():
    p, waits = policy()
    attempts = run(SequenceClient(ok("FEATURE"), ok("BUG")), p)
    assert len(attempts) == 1 and attempts[0].failure_type is FailureType.VALIDATION_ERROR


def test_first_try_success_single_attempt():
    p, waits = policy()
    attempts = run(SequenceClient(ok("BUG")), p)
    assert len(attempts) == 1 and waits == [] and not recovered(attempts)


def test_default_policy_comes_from_config():
    attempts = run_test_case(SequenceClient(err(T), ok("BUG")), tasks(1).tasks[0], cfg())
    assert len(attempts) == 1




def test_experiment_stores_every_attempt(tmp_path):
    conn = database.connect(tmp_path / "r.db")
    p, waits = policy(max_attempts=2)
    client = SequenceClient(err(T), ok("BUG"),
                            ok("BUG"),
                            err(RL), err(RL))
    result = run_experiment(cfg(enabled=True, max_attempts=2), tasks(3), client, conn=conn, policy=p)
    assert [(a.task_id, a.attempt_number, a.success) for a in result.attempts] == [
        ("t0", 1, False), ("t0", 2, True), ("t1", 1, True), ("t2", 1, False), ("t2", 2, False)]
    assert waits == [1, 1]
    assert database.load_experiment(conn, result.id) == result


def test_cli_summary_shows_recovery(monkeypatch, tmp_path):
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr("time.sleep", lambda s: None)
    client = SequenceClient(err(T), ok("Paris"), ok("42"))
    monkeypatch.setattr(cli, "build_client", lambda model, request: client)
    out = CliRunner().invoke(cli.app, ["run", "-c", "config/retry_1.yaml", "--limit", "2",
                                       "--db", str(tmp_path / "r.db")])
    assert out.exit_code == 0, out.output
    assert "up to 2 attempts" in out.output and "PASS (2 tries)" in out.output
    assert "Initial success 1/2 (50%), final success 2/2 (100%), recovered by retry 1" in out.output
    assert "Attempts 3 (1.50/task)" in out.output




def test_pacing_waits_between_task_starts():
    config = cfg(3).model_copy(update={"test": cfg(3).test.model_copy(update={"min_interval_seconds": 5})})
    p, waits = policy(max_attempts=1, enabled=False)
    run_experiment(config, tasks(3), SequenceClient(ok("BUG"), ok("BUG"), ok("BUG")), policy=p)
    assert len(waits) == 2 and all(4.5 < w <= 5 for w in waits)


def test_no_pacing_by_default():
    p, waits = policy(max_attempts=1, enabled=False)
    run_experiment(cfg(3), tasks(3), SequenceClient(ok("BUG"), ok("BUG"), ok("BUG")), policy=p)
    assert waits == []


def test_daily_quota_stops_run(monkeypatch, tmp_path):
    monkeypatch.chdir(ROOT)
    quota = err(FailureType.RATE_LIMIT, 429).model_copy(
        update={"error_message": "HTTP 429: Rate limit exceeded: free-models-per-day"})
    client = SequenceClient(ok("Paris"), quota, ok("never sent"))
    monkeypatch.setattr(cli, "build_client", lambda model, request: client)
    monkeypatch.setattr("time.sleep", lambda s: None)
    db = tmp_path / "q.db"
    out = CliRunner().invoke(cli.app, ["run", "-c", "config/baseline.yaml", "--limit", "3", "--db", str(db)])
    assert out.exit_code == 4 and "daily request quota" in out.output
    assert client.calls == 2
    (row,) = database.list_experiments(database.connect(db))
    assert row["finished_at"] is None
