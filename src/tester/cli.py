import logging
from collections import Counter
from pathlib import Path

import typer

from tester import __version__, database
from tester.api_client import MissingAPIKey, build_client
from tester.config import ConfigError, load_config
from tester.dataset import DatasetError, load_dataset
from tester.metrics import Metrics, compute_metrics
from tester.reporting import comparison_rows, comparison_warnings, label, write_comparison, write_report
from tester.runner import run_experiment, select_tasks
from tester.validators import validate as validate_response

app = typer.Typer(help="AI API Reliability Tester", no_args_is_help=True)


@app.command()
def version() -> None:
    """Print the application version."""
    typer.echo(__version__)


@app.command()
def validate(config: Path = typer.Option(..., "--config", "-c", help="Experiment YAML file")) -> None:
    """Check an experiment config file and the dataset it points to, without running it."""
    try:
        cfg = load_config(config)
        ds = load_dataset(cfg.dataset)
    except (ConfigError, DatasetError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    if cfg.test.requests > len(ds.tasks):
        typer.echo(f"test.requests={cfg.test.requests} but dataset has only {len(ds.tasks)} tasks", err=True)
        raise typer.Exit(1)

    typer.echo(f"OK: {config}")
    typer.echo(cfg.model_dump_json(indent=2))
    counts = Counter(t.category for t in ds.tasks)
    typer.echo(f"Dataset: {cfg.dataset} v{ds.version}, {len(ds.tasks)} tasks {dict(counts)}")
    typer.echo(f"Dataset sha256: {ds.sha256}")


@app.command()
def probe(
    config: Path = typer.Option(..., "--config", "-c", help="Experiment YAML file"),
    task: str = typer.Option(None, "--task", "-t", help="Task id (default: first task)"),
) -> None:
    """Send ONE task to the real API and print the normalized response. Costs one request."""
    try:
        cfg = load_config(config)
        ds = load_dataset(cfg.dataset)
        client = build_client(cfg.model, cfg.request)
    except (ConfigError, DatasetError, MissingAPIKey) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)

    chosen = next((t for t in ds.tasks if t.id == task), None) if task else ds.tasks[0]
    if chosen is None:
        typer.echo(f"No task with id {task!r}", err=True)
        raise typer.Exit(1)

    typer.echo(f"{cfg.model.name} @ {cfg.model.base_url}")
    typer.echo(f"Task {chosen.id}: {chosen.prompt}")
    typer.echo(f"Expected: {chosen.expected}")
    with client:
        resp = client.send(chosen.prompt)
    typer.echo(resp.model_dump_json(indent=2))
    if not resp.success:
        raise typer.Exit(2)
    v = validate_response(chosen, resp.text)
    typer.echo(f"Validation ({chosen.validation}): " + ("PASS" if v.passed else f"FAIL {v.failure_type}: {v.message}"))
    raise typer.Exit(0 if v.passed else 3)


@app.command()
def run(
    config: Path = typer.Option(..., "--config", "-c", help="Experiment YAML file"),
    limit: int = typer.Option(None, "--limit", "-n", min=1, help="Only run the first N tasks (saves API calls)"),
    db: Path = typer.Option(database.DEFAULT_DB, "--db", help="SQLite file for results"),
) -> None:
    """Run an experiment: every selected task, sequentially. Every attempt is saved to SQLite."""
    try:
        cfg = load_config(config)
        ds = load_dataset(cfg.dataset)
        client = build_client(cfg.model, cfg.request)
    except (ConfigError, DatasetError, MissingAPIKey) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)

    total = len(select_tasks(cfg, ds, limit))
    retry = f"up to {cfg.retry.max_attempts} attempts" if cfg.retry.enabled else "no retry"
    typer.echo(f"Experiment {cfg.name}: {total} tasks, {retry}, {cfg.model.name} @ {cfg.model.base_url}")
    show_retry_logs()
    done = 0

    def show(task, attempts):
        nonlocal done
        done += 1
        last = attempts[-1]
        status = "PASS" if last.success else f"FAIL {last.failure_type}"
        if len(attempts) > 1:
            status += f" ({len(attempts)} tries)"
        # Task time includes every attempt and backoff wait.
        elapsed = (last.finished_at - attempts[0].started_at).total_seconds()
        detail = "" if last.success else f"  {last.validation_message or last.response.error_message or ''}"[:120]
        typer.echo(f"[{done:>2}/{total}] {task.id:<20} {status:<32} {elapsed:6.2f}s{detail}")

    conn = database.connect(db)
    try:
        with client:
            result = run_experiment(cfg, ds, client, limit=limit, progress=show, conn=conn)
    except KeyboardInterrupt:
        typer.echo(f"\nInterrupted. Finished tasks are saved in {db} (experiment marked unfinished).", err=True)
        raise typer.Exit(130)
    finally:
        conn.close()
    typer.echo(f"\nSaved as experiment #{result.id} in {db}")

    print_metrics(compute_metrics(result))
    out = write_report(result, base=db.parent)
    typer.echo(f"Report: {out / 'report.md'}")


def print_metrics(m: Metrics) -> None:
    n = m.tasks
    typer.echo(f"Initial success {m.initial_successes}/{n} ({m.initial_success_rate:.0%}), "
               f"final success {m.final_successes}/{n} ({m.final_success_rate:.0%}), recovered by retry {m.recovered}")
    typer.echo(f"Attempts {m.attempts} ({m.avg_attempts:.2f}/task), attempt latency mean "
               f"{m.attempt_latency.mean:.2f}s / P95 {m.attempt_latency.p95:.2f}s, "
               f"task latency P95 {m.task_latency.p95:.2f}s")
    if m.final_failures:
        typer.echo("Final failures: " + ", ".join(f"{k} x{v}" for k, v in m.final_failures.items()))


def show_retry_logs() -> None:
    """Print the runner's retry messages while waiting, so long backoffs aren't silent."""
    log = logging.getLogger("tester")
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("        retry: %(message)s"))
        log.addHandler(handler)
    log.setLevel(logging.INFO)


@app.command("list")
def list_(db: Path = typer.Option(database.DEFAULT_DB, "--db", help="SQLite file for results")) -> None:
    """List stored experiments."""
    if not db.exists():
        typer.echo(f"No results yet ({db} does not exist).")
        return
    conn = database.connect(db)
    rows = database.list_experiments(conn)
    conn.close()
    typer.echo(f"{'id':>3}  {'name':<12} {'model':<30} {'tasks':>5} {'attempts':>8} {'passed':>6}  {'started (UTC)':<19}  status")
    for r in rows:
        status = "done" if r["finished_at"] else "unfinished"
        if r["task_limit"]:
            status += f" (limit {r['task_limit']})"
        typer.echo(f"{r['id']:>3}  {r['name']:<12} {r['model'][:30]:<30} {r['tasks']:>5} {r['attempts']:>8} "
                   f"{r['successes']:>6}  {r['created_at'][:19]:<19}  {status}")


DB_OPTION = typer.Option(database.DEFAULT_DB, "--db", help="SQLite file for results")
EXPERIMENT_HELP = "Experiment id (e.g. 5) or name (latest run with that name)"


def load_stored(db: Path, refs: list[str]):
    """Resolve ids/names to stored experiments plus their metrics, or exit with a message."""
    if not db.exists():
        typer.echo(f"No results yet ({db} does not exist). Run an experiment first.", err=True)
        raise typer.Exit(1)
    conn = database.connect(db)
    try:
        results = []
        for ref in refs:
            exp_id = int(ref) if ref.isdigit() else database.latest_experiment_id(conn, ref)
            if exp_id is None:
                raise KeyError(f"no experiment named {ref!r}")
            results.append(database.load_experiment(conn, exp_id))
    except KeyError as e:
        typer.echo(f"{e.args[0]}. See `python -m tester list`.", err=True)
        raise typer.Exit(1)
    finally:
        conn.close()
    try:
        return results, [compute_metrics(r) for r in results]
    except ValueError:
        typer.echo("An experiment has no attempts (interrupted before the first task finished).", err=True)
        raise typer.Exit(1)


@app.command()
def summary(
    experiment: str = typer.Option(..., "--experiment", "-e", help=EXPERIMENT_HELP),
    db: Path = DB_OPTION,
) -> None:
    """Show the metrics of one stored experiment."""
    (result,), (m,) = load_stored(db, [experiment])
    c = result.config
    status = "complete" if result.finished_at else "interrupted"
    if result.task_limit:
        status += f", limit {result.task_limit}"
    typer.echo(f"Experiment #{result.id} {c.name} ({status})")
    typer.echo(f"Model {c.model.name} @ {c.model.base_url}, started {result.started_at:%Y-%m-%d %H:%M:%S} UTC")
    retry = f"up to {c.retry.max_attempts} attempts, backoff {c.retry.backoff_seconds}s" if c.retry.enabled else "off"
    typer.echo(f"Retry {retry}; timeout {c.request.timeout_seconds}s; dataset v{result.dataset_version} "
               f"{result.dataset_hash[:12]}")
    print_metrics(m)
    if m.retry_recovery_rate is not None:
        typer.echo(f"Retry recovery rate {m.retry_recovery_rate:.0%}")
    if m.total_tokens is not None:
        typer.echo(f"Tokens {m.total_tokens} ({m.tokens_per_task:.1f}/task)")


@app.command()
def compare(
    experiments: list[str] = typer.Argument(..., help="Two or more experiment ids or names"),
    db: Path = DB_OPTION,
) -> None:
    """Compare experiments side by side and write a comparison report with charts."""
    if len(experiments) < 2:
        typer.echo("Give at least two experiments to compare.", err=True)
        raise typer.Exit(1)
    results, metrics = load_stored(db, experiments)
    rows = comparison_rows(results, metrics)
    headers = [label(r) for r in results]
    width = max(len(name) for name, _ in rows)
    cols = [max(len(h), *(len(values[i]) for _, values in rows)) for i, h in enumerate(headers)]
    typer.echo(" " * width + "  " + "  ".join(h.rjust(w) for h, w in zip(headers, cols)))
    for name, values in rows:
        typer.echo(name.ljust(width) + "  " + "  ".join(v.rjust(w) for v, w in zip(values, cols)))
    for w in comparison_warnings(results):
        typer.echo(f"Warning: {w}", err=True)
    out = write_comparison(results, metrics, base=db.parent)
    typer.echo(f"\nComparison report: {out / 'comparison.md'}")


@app.command()
def export(
    experiment: str = typer.Option(..., "--experiment", "-e", help=EXPERIMENT_HELP),
    db: Path = DB_OPTION,
    out: Path = typer.Option(None, "--out", "-o", help="Base folder (default: next to the database)"),
) -> None:
    """(Re)build an experiment's report folder from the database. Works for interrupted runs too."""
    (result,), _ = load_stored(db, [experiment])
    folder = write_report(result, base=out or db.parent)
    typer.echo(f"Exported experiment #{result.id} to {folder}")
