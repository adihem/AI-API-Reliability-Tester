"""SQLite persistence. Every attempt is stored, so retry recovery can be measured later.

Row layout per attempt:
  requests   always              (timing, HTTP status, SUCCESS/FAILURE)
  responses  if a 2xx came back  (text, tokens, validation verdict)
  errors     if the attempt failed (API failure type, or the validation failure)
"""

import sqlite3
from datetime import datetime
from pathlib import Path

from tester.models import APIResponse, ExperimentConfig, ExperimentResult, RequestResult

DEFAULT_DB = Path("results/experiments.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    model           TEXT NOT NULL,
    configuration   TEXT NOT NULL,      -- full ExperimentConfig as JSON
    dataset_version TEXT NOT NULL,
    dataset_hash    TEXT NOT NULL,
    app_version     TEXT NOT NULL,
    task_limit      INTEGER,            -- --limit used, NULL = config value
    created_at      TEXT NOT NULL,
    finished_at     TEXT                -- NULL = interrupted / still running
);
CREATE TABLE IF NOT EXISTS requests (
    id              INTEGER PRIMARY KEY,
    experiment_id   INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    task_id         TEXT NOT NULL,
    attempt_number  INTEGER NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT NOT NULL,
    latency_seconds REAL NOT NULL,
    http_status     INTEGER,
    status          TEXT NOT NULL CHECK (status IN ('SUCCESS', 'FAILURE')),
    UNIQUE (experiment_id, task_id, attempt_number)
);
CREATE TABLE IF NOT EXISTS responses (
    id                 INTEGER PRIMARY KEY,
    request_id         INTEGER NOT NULL UNIQUE REFERENCES requests(id) ON DELETE CASCADE,
    response_text      TEXT,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    total_tokens       INTEGER,
    validation_status  TEXT CHECK (validation_status IN ('PASS', 'FAIL'))  -- NULL = not validated
);
CREATE TABLE IF NOT EXISTS errors (
    id             INTEGER PRIMARY KEY,
    request_id     INTEGER NOT NULL UNIQUE REFERENCES requests(id) ON DELETE CASCADE,
    failure_type   TEXT NOT NULL,
    error_message  TEXT,
    retryable      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requests_experiment ON requests(experiment_id);
"""


def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def create_experiment(conn, result: ExperimentResult) -> int:
    """Insert the experiment header (attempts are saved separately). Returns its id."""
    c = result.config
    with conn:
        cur = conn.execute(
            "INSERT INTO experiments (name, model, configuration, dataset_version, dataset_hash,"
            " app_version, task_limit, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (c.name, c.model.name, c.model_dump_json(), result.dataset_version, result.dataset_hash,
             result.app_version, result.task_limit, result.started_at.isoformat()),
        )
    return cur.lastrowid


def save_attempts(conn, experiment_id: int, attempts: list[RequestResult]) -> None:
    """Commit one task's attempts at once, so an interrupted run keeps everything finished so far."""
    with conn:
        for a in attempts:
            r = a.response
            req_id = conn.execute(
                "INSERT INTO requests (experiment_id, task_id, attempt_number, started_at, finished_at,"
                " latency_seconds, http_status, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (experiment_id, a.task_id, a.attempt_number, a.started_at.isoformat(), a.finished_at.isoformat(),
                 r.latency_seconds, r.http_status, "SUCCESS" if a.success else "FAILURE"),
            ).lastrowid
            if r.http_status is not None and 200 <= r.http_status < 300:
                verdict = None if a.validation_passed is None else ("PASS" if a.validation_passed else "FAIL")
                conn.execute(
                    "INSERT INTO responses (request_id, response_text, input_tokens, output_tokens, total_tokens,"
                    " validation_status) VALUES (?, ?, ?, ?, ?, ?)",
                    (req_id, r.text, r.input_tokens, r.output_tokens, r.total_tokens, verdict),
                )
            if a.failure_type is not None:
                message = a.validation_message if r.success else r.error_message
                conn.execute(
                    "INSERT INTO errors (request_id, failure_type, error_message, retryable) VALUES (?, ?, ?, ?)",
                    (req_id, str(a.failure_type), message, int(a.retryable)),
                )


def finish_experiment(conn, experiment_id: int, finished_at: datetime) -> None:
    with conn:
        conn.execute("UPDATE experiments SET finished_at = ? WHERE id = ?", (finished_at.isoformat(), experiment_id))


def save_experiment(conn, result: ExperimentResult) -> int:
    """Store a complete in-memory result in one go."""
    exp_id = create_experiment(conn, result)
    save_attempts(conn, exp_id, result.attempts)
    if result.finished_at:
        finish_experiment(conn, exp_id, result.finished_at)
    return exp_id


def list_experiments(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT e.id, e.name, e.model, e.task_limit, e.created_at, e.finished_at,"
        " COUNT(DISTINCT r.task_id) AS tasks, COUNT(r.id) AS attempts,"
        # A SUCCESS ends a task's attempts, so this counts tasks with final success.
        " COUNT(DISTINCT CASE WHEN r.status = 'SUCCESS' THEN r.task_id END) AS successes"
        " FROM experiments e LEFT JOIN requests r ON r.experiment_id = e.id"
        " GROUP BY e.id ORDER BY e.id"
    ).fetchall()


def latest_experiment_id(conn, name: str) -> int | None:
    row = conn.execute("SELECT MAX(id) FROM experiments WHERE name = ?", (name,)).fetchone()
    return row[0]


def load_experiment(conn, experiment_id: int) -> ExperimentResult:
    exp = conn.execute("SELECT * FROM experiments WHERE id = ?", (experiment_id,)).fetchone()
    if exp is None:
        raise KeyError(f"no experiment with id {experiment_id}")
    rows = conn.execute(
        "SELECT r.*, s.response_text, s.input_tokens, s.output_tokens, s.total_tokens, s.validation_status,"
        " e.failure_type, e.error_message, e.retryable, s.id IS NOT NULL AS has_response"
        " FROM requests r LEFT JOIN responses s ON s.request_id = r.id LEFT JOIN errors e ON e.request_id = r.id"
        " WHERE r.experiment_id = ? ORDER BY r.id",
        (experiment_id,),
    ).fetchall()
    return ExperimentResult(
        id=exp["id"],
        config=ExperimentConfig.model_validate_json(exp["configuration"]),
        dataset_version=exp["dataset_version"],
        dataset_hash=exp["dataset_hash"],
        app_version=exp["app_version"],
        task_limit=exp["task_limit"],
        started_at=exp["created_at"],
        finished_at=exp["finished_at"],
        attempts=[_attempt(row) for row in rows],
    )


def _attempt(row: sqlite3.Row) -> RequestResult:
    # A validation verdict exists only if the API call itself succeeded.
    api_ok = row["validation_status"] is not None
    response = APIResponse(
        success=api_ok,
        text=row["response_text"],
        http_status=row["http_status"],
        latency_seconds=row["latency_seconds"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        total_tokens=row["total_tokens"],
        error_type=None if api_ok else row["failure_type"],
        error_message=None if api_ok else row["error_message"],
    )
    return RequestResult(
        task_id=row["task_id"],
        attempt_number=row["attempt_number"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        response=response,
        validation_passed={"PASS": True, "FAIL": False, None: None}[row["validation_status"]],
        validation_message=row["error_message"] if api_ok else None,
        failure_type=row["failure_type"],
        retryable=bool(row["retryable"]),
    )
