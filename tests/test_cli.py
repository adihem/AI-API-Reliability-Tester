"""CLI workflow tests: run (fake API) -> list -> summary -> compare -> export. No network."""

import pytest
from typer.testing import CliRunner

from tester import cli, database
from tester.models import FailureType
from tester.retry import RetryPolicy
from tester.runner import run_experiment
from test_retry import SequenceClient
from test_runner import cfg, err, ok, tasks

invoke = CliRunner().invoke


@pytest.fixture
def db(tmp_path):
    """Two stored experiments over the same 3 tasks:
    #1 'base'  no retry:  t0 pass, t1 TIMEOUT, t2 RATE_LIMIT        -> 1/3
    #2 'retry' 2 attempts: t0 pass, t1 TIMEOUT->pass, t2 RL->RL      -> 2/3, recovered 1
    """
    path = tmp_path / "results" / "exp.db"
    conn = database.connect(path)
    base = cfg(3).model_copy(update={"name": "base"})
    run_experiment(base, tasks(), SequenceClient(ok("BUG"), err(FailureType.TIMEOUT), err(FailureType.RATE_LIMIT)),
                   conn=conn)
    retry = cfg(3, enabled=True, max_attempts=2).model_copy(update={"name": "retry"})
    run_experiment(retry, tasks(), SequenceClient(ok("BUG"), err(FailureType.TIMEOUT), ok("BUG"),
                                                  err(FailureType.RATE_LIMIT), err(FailureType.RATE_LIMIT)),
                   conn=conn, policy=RetryPolicy(retry.retry, sleep=lambda s: None))
    conn.close()
    return path


def test_help_lists_all_commands():
    out = invoke(cli.app, ["--help"]).output
    for command in ["validate", "run", "summary", "compare", "export", "list", "probe", "version"]:
        assert command in out


def test_list(db):
    out = invoke(cli.app, ["list", "--db", str(db)]).output
    assert "base" in out and "retry" in out


@pytest.mark.parametrize("ref", ["2", "retry"])
def test_summary_by_id_or_name(db, ref):
    out = invoke(cli.app, ["summary", "--experiment", ref, "--db", str(db)])
    assert out.exit_code == 0, out.output
    assert "Experiment #2 retry (complete)" in out.output
    assert "Initial success 1/3 (33%), final success 2/3 (67%), recovered by retry 1" in out.output
    assert "Retry recovery rate 50%" in out.output


def test_summary_unknown_experiment(db):
    out = invoke(cli.app, ["summary", "-e", "nope", "--db", str(db)])
    assert out.exit_code == 1 and "no experiment named 'nope'" in out.output


def test_summary_no_database(tmp_path):
    out = invoke(cli.app, ["summary", "-e", "1", "--db", str(tmp_path / "none.db")])
    assert out.exit_code == 1 and "No results yet" in out.output


def test_compare(db):
    out = invoke(cli.app, ["compare", "base", "retry", "--db", str(db)])
    assert out.exit_code == 0, out.output
    lines = out.output.splitlines()
    assert any(l.startswith("Initial success") and "33.3%" in l for l in lines)
    final = next(l for l in lines if l.startswith("Final success"))
    assert final.split()[-2:] == ["33.3%", "66.7%"]
    assert "Warning" not in out.output  # same dataset, same tasks, same model

    folder = db.parent / "compare_1_2"
    assert {p.name for p in folder.iterdir()} == {"comparison.md", "comparison.csv", "figures"}
    assert {p.name for p in (folder / "figures").iterdir()} == {
        "success_by_experiment.png", "task_latency_by_experiment.png"}
    md = (folder / "comparison.md").read_text(encoding="utf-8")
    assert "| Final success | 33.3% | 66.7% |" in md


def test_compare_warns_on_uncontrolled_comparison(db):
    conn = database.connect(db)
    run_experiment(cfg(3).model_copy(update={"name": "short"}), tasks(), SequenceClient(ok("BUG")), limit=1, conn=conn)
    conn.close()
    out = invoke(cli.app, ["compare", "1", "3", "--db", str(db)])
    assert out.exit_code == 0 and "different task sets" in out.output


def test_compare_needs_two(db):
    out = invoke(cli.app, ["compare", "1", "--db", str(db)])
    assert out.exit_code == 1 and "at least two" in out.output


def test_export(db, tmp_path):
    out = invoke(cli.app, ["export", "-e", "retry", "--db", str(db), "--out", str(tmp_path / "exp")])
    assert out.exit_code == 0, out.output
    (folder,) = (tmp_path / "exp").iterdir()
    assert folder.name.endswith("_retry_002")
    assert (folder / "report.md").exists() and len(list((folder / "figures").iterdir())) == 4


def test_export_interrupted_run(db):
    conn = database.connect(db)
    exp_id = database.create_experiment(conn, run_experiment(cfg(1), tasks(1), SequenceClient(ok("BUG"))))
    conn.close()
    out = invoke(cli.app, ["export", "-e", str(exp_id), "--db", str(db)])
    assert out.exit_code == 1 and "no attempts" in out.output  # header only, nothing to report
