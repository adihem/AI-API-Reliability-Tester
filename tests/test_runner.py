"""Runner tests with a scripted fake client. No network."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tester import cli
from tester.config import load_config
from tester.dataset import load_dataset
from tester.models import APIResponse, Dataset, ExperimentConfig, FailureType, Task
from tester.runner import run_attempt, run_experiment, select_tasks

ROOT = Path(__file__).parent.parent


def ok(text):
    return APIResponse(success=True, text=text, http_status=200, latency_seconds=0.1, total_tokens=10)


def err(kind, status=None):
    return APIResponse(success=False, http_status=status, latency_seconds=0.2, error_type=kind, error_message="x")


class FakeClient:
    """Replies per prompt from a dict; unscripted prompts get a correct-looking default."""

    def __init__(self, replies=None, default=None):
        self.replies = replies or {}
        self.default = default
        self.prompts = []

    def send(self, prompt):
        self.prompts.append(prompt)
        reply = self.replies.get(prompt, self.default)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def cfg(requests=3, **retry):
    return ExperimentConfig.model_validate({
        "name": "t",
        "model": {"name": "m"},
        "request": {"temperature": 0, "max_tokens": 10, "timeout_seconds": 5},
        "test": {"requests": requests},
        "validation": {"type": "exact_match"},
        "retry": retry or {},
    })


def tasks(n=3):
    return Dataset(version="1", sha256="abc", tasks=[
        Task(id=f"t{i}", category="classification", prompt=f"p{i}", validation="exact_match", expected="BUG")
        for i in range(n)
    ])


def test_pass():
    r = run_attempt(FakeClient(default=ok("BUG")), tasks().tasks[0], 1, cfg())
    assert r.success and r.validation_passed is True and r.failure_type is None
    assert r.attempt_number == 1 and r.started_at <= r.finished_at


def test_wrong_answer_is_validation_error():
    r = run_attempt(FakeClient(default=ok("FEATURE")), tasks().tasks[0], 1, cfg())
    assert not r.success and r.validation_passed is False
    assert r.failure_type is FailureType.VALIDATION_ERROR and "FEATURE" in r.validation_message
    assert r.retryable is False


def test_api_failure_skips_validation():
    r = run_attempt(FakeClient(default=err(FailureType.TIMEOUT)), tasks().tasks[0], 1, cfg())
    assert r.validation_passed is None and r.failure_type is FailureType.TIMEOUT
    assert r.retryable is True


def test_retryable_follows_config():
    r = run_attempt(FakeClient(default=err(FailureType.TIMEOUT)), tasks().tasks[0], 1,
                    cfg(enabled=True, max_attempts=2, retry_on=["RATE_LIMIT"]))
    assert r.retryable is False


def test_client_crash_becomes_unknown_error():
    r = run_attempt(FakeClient(default=RuntimeError("bug")), tasks().tasks[0], 1, cfg())
    assert r.failure_type is FailureType.UNKNOWN_ERROR and "RuntimeError: bug" in r.response.error_message


def test_experiment_mixed_outcomes_in_order():
    client = FakeClient({"p0": ok("BUG"), "p1": err(FailureType.RATE_LIMIT, 429), "p2": ok("nope")})
    seen = []
    result = run_experiment(cfg(), tasks(), client, progress=lambda t, a: seen.append(t.id))
    assert client.prompts == ["p0", "p1", "p2"]
    assert seen == ["t0", "t1", "t2"]
    assert [a.failure_type for a in result.attempts] == [None, FailureType.RATE_LIMIT, FailureType.VALIDATION_ERROR]
    assert result.dataset_hash == "abc" and result.finished_at >= result.started_at


@pytest.mark.parametrize("requests,limit,expected", [(3, None, 3), (3, 2, 2), (2, None, 2), (2, 5, 2)])
def test_requests_and_limit(requests, limit, expected):
    assert len(select_tasks(cfg(requests), tasks(), limit)) == expected


def test_full_dataset_through_fake_api(monkeypatch):
    """Real config + real 30-task dataset, fake API answering every task correctly."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    config = load_config(ROOT / "config" / "baseline.yaml")
    ds = load_dataset(ROOT / config.dataset)
    answers = {}
    for t in ds.tasks:
        if t.validation == "exact_match":
            answers[t.prompt] = ok(t.expected)
        elif t.validation == "json":
            answers[t.prompt] = ok(json.dumps(t.expected))
        elif t.validation == "schema":
            sample = {"string": "s", "integer": 1, "number": 1.5, "boolean": True, "array": [], "object": {}}
            answers[t.prompt] = ok(json.dumps({k: sample[v] for k, v in t.expected.items()}))
        else:
            answers[t.prompt] = ok("Some text.")
    result = run_experiment(config, ds, FakeClient(answers))
    assert len(result.attempts) == 30
    assert all(a.success for a in result.attempts), [a for a in result.attempts if not a.success]


def test_cli_run_with_limit_saves_and_lists(monkeypatch, tmp_path):
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr("time.sleep", lambda s: None)
    db = str(tmp_path / "t.db")
    fake = FakeClient(default=ok("BUG"))
    monkeypatch.setattr(cli, "build_client", lambda model, request: fake)
    out = CliRunner().invoke(cli.app, ["run", "-c", "config/baseline.yaml", "--limit", "3", "--db", db])
    assert out.exit_code == 0, out.output
    assert len(fake.prompts) == 3
    assert "[ 3/3]" in out.output and "final success 0/3" in out.output
    assert "VALIDATION_ERROR x3" in out.output and "Saved as experiment #1" in out.output

    listing = CliRunner().invoke(cli.app, ["list", "--db", db]).output
    assert "baseline" in listing and "done (limit 3)" in listing
