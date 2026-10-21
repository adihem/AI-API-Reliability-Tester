"""SQLite round-trip tests. Each test uses its own temporary database file."""

import pytest

from tester import database
from tester.models import FailureType
from tester.runner import run_experiment
from test_runner import FakeClient, cfg, err, ok, tasks


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "results" / "exp.db"


def mixed_run(conn=None):
    """t0 passes, t1 rate-limited, t2 wrong answer."""
    client = FakeClient({"p0": ok("BUG"), "p1": err(FailureType.RATE_LIMIT, 429), "p2": ok("FEATURE")})
    return run_experiment(cfg(), tasks(), client, conn=conn)


def test_round_trip_is_lossless(db_path):
    conn = database.connect(db_path)
    result = mixed_run(conn)
    loaded = database.load_experiment(conn, result.id)
    assert loaded == result


def test_survives_reconnect(db_path):
    """The Phase 6 goal: results remain after the application exits."""
    conn = database.connect(db_path)
    exp_id = mixed_run(conn).id
    conn.close()

    conn = database.connect(db_path)
    loaded = database.load_experiment(conn, exp_id)
    assert [a.failure_type for a in loaded.attempts] == [None, FailureType.RATE_LIMIT, FailureType.VALIDATION_ERROR]
    assert loaded.config.name == "t" and loaded.dataset_hash == "abc" and loaded.finished_at is not None


def test_row_layout(db_path):
    conn = database.connect(db_path)
    exp_id = mixed_run(conn).id
    q = lambda sql: [tuple(r) for r in conn.execute(sql).fetchall()]
    assert q("SELECT task_id, attempt_number, http_status, status FROM requests ORDER BY id") == [
        ("t0", 1, 200, "SUCCESS"), ("t1", 1, 429, "FAILURE"), ("t2", 1, 200, "FAILURE")]
    assert q("SELECT response_text, total_tokens, validation_status FROM responses ORDER BY id") == [
        ("BUG", 10, "PASS"), ("FEATURE", 10, "FAIL")]
    assert q("SELECT failure_type, retryable FROM errors ORDER BY id") == [
        ("RATE_LIMIT", 1), ("VALIDATION_ERROR", 0)]
    assert q("SELECT name, model, finished_at IS NOT NULL FROM experiments") == [("t", "m", 1)]
    assert exp_id == 1


def test_empty_response_round_trip(db_path):
    """2xx with blank content: response row (text, tokens) + API error row, no validation verdict."""
    conn = database.connect(db_path)
    empty = ok("").model_copy(update={"success": False, "error_type": FailureType.EMPTY_RESPONSE, "error_message": "empty"})
    result = run_experiment(cfg(1), tasks(1), FakeClient(default=empty), conn=conn)
    loaded = database.load_experiment(conn, result.id)
    assert loaded == result
    assert loaded.attempts[0].response.total_tokens == 10 and loaded.attempts[0].validation_passed is None


def test_interrupted_run_keeps_finished_tasks(db_path):
    class Interrupt(FakeClient):
        def send(self, prompt):
            if prompt == "p2":
                raise KeyboardInterrupt
            return ok("BUG")

    conn = database.connect(db_path)
    with pytest.raises(KeyboardInterrupt):
        run_experiment(cfg(), tasks(), Interrupt(), conn=conn)
    conn.close()

    conn = database.connect(db_path)
    (row,) = database.list_experiments(conn)
    assert row["finished_at"] is None and row["tasks"] == 2 and row["successes"] == 2


def test_list_and_latest(db_path):
    conn = database.connect(db_path)
    first = mixed_run(conn).id
    second = mixed_run(conn).id
    rows = database.list_experiments(conn)
    assert [r["id"] for r in rows] == [first, second]
    assert rows[0]["attempts"] == 3 and rows[0]["successes"] == 1
    assert database.latest_experiment_id(conn, "t") == second
    assert database.latest_experiment_id(conn, "nope") is None


def test_save_experiment_after_the_fact(db_path):
    result = mixed_run()
    conn = database.connect(db_path)
    exp_id = database.save_experiment(conn, result)
    assert database.load_experiment(conn, exp_id) == result.model_copy(update={"id": exp_id})


def test_duplicate_attempt_rejected(db_path):
    conn = database.connect(db_path)
    result = mixed_run(conn)
    with pytest.raises(Exception):
        database.save_attempts(conn, result.id, result.attempts[:1])


def test_missing_experiment(db_path):
    with pytest.raises(KeyError):
        database.load_experiment(database.connect(db_path), 99)
