"""Write an experiment's result folder: config, raw attempts, metrics, Markdown report, charts.

results/<date>_<name>_<id>/
    config.yaml  results.csv  results.json  summary.json  report.md
    figures/ success_rate.png  latency_distribution.png  failure_categories.png  retry_recovery.png
"""

import csv
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # files only, no GUI window
import matplotlib.pyplot as plt  # noqa: E402
import yaml  # noqa: E402

from tester.metrics import Metrics, compute_metrics  # noqa: E402
from tester.models import ExperimentResult  # noqa: E402

RESULTS_DIR = Path("results")

# Reference dataviz palette (validated: light mode, CVD-safe for adjacent slots).
SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"

CSV_FIELDS = [
    "experiment_id", "task_id", "attempt_number", "started_at", "finished_at", "latency_seconds",
    "http_status", "success", "failure_type", "retryable", "validation_passed", "input_tokens",
    "output_tokens", "total_tokens", "error_message", "response_text",
]


def output_dir(result: ExperimentResult, base: Path = RESULTS_DIR) -> Path:
    suffix = f"{result.id:03d}" if result.id is not None else result.started_at.strftime("%H%M%S")
    return base / f"{result.started_at:%Y-%m-%d}_{result.config.name}_{suffix}"


def write_report(result: ExperimentResult, base: Path = RESULTS_DIR) -> Path:
    metrics = compute_metrics(result)
    out = output_dir(result, base)
    (out / "figures").mkdir(parents=True, exist_ok=True)

    (out / "config.yaml").write_text(
        yaml.safe_dump(result.config.model_dump(mode="json"), sort_keys=False), encoding="utf-8")
    (out / "results.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary(result, metrics), indent=2), encoding="utf-8")
    write_csv(result, out / "results.csv")
    figures = write_charts(result, metrics, out / "figures")
    (out / "report.md").write_text(markdown(result, metrics, figures), encoding="utf-8")
    return out


def summary(result: ExperimentResult, metrics: Metrics) -> dict:
    """Metrics plus everything needed to reproduce the run."""
    c = result.config
    return {
        "experiment_id": result.id,
        "name": c.name,
        "model": c.model.name,
        "base_url": c.model.base_url,
        "dataset": c.dataset,
        "dataset_version": result.dataset_version,
        "dataset_hash": result.dataset_hash,
        "app_version": result.app_version,
        "task_limit": result.task_limit,
        "started_at": result.started_at.isoformat(),
        "finished_at": result.finished_at.isoformat() if result.finished_at else None,
        "retry": c.retry.model_dump(mode="json"),
        "concurrency": c.test.concurrency,
        "timeout_seconds": c.request.timeout_seconds,
        "metrics": metrics.model_dump(),
    }


def write_csv(result: ExperimentResult, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for a in result.attempts:
            r = a.response
            w.writerow({
                "experiment_id": result.id, "task_id": a.task_id, "attempt_number": a.attempt_number,
                "started_at": a.started_at.isoformat(), "finished_at": a.finished_at.isoformat(),
                "latency_seconds": round(r.latency_seconds, 4), "http_status": r.http_status,
                "success": a.success, "failure_type": a.failure_type or "", "retryable": a.retryable,
                "validation_passed": a.validation_passed, "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens, "total_tokens": r.total_tokens,
                "error_message": a.validation_message or r.error_message or "", "response_text": r.text or "",
            })


# --- charts ---


def _axes(height: float = 2.6):
    fig, ax = plt.subplots(figsize=(7, height), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK_2, length=0)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    return fig, ax


def _title(ax, text: str) -> None:
    ax.set_title(text, loc="left", color=INK, fontsize=11, pad=10)


def _save(fig, path: Path) -> str:
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE, bbox_inches="tight", pad_inches=0.15)
    plt.close(fig)
    return path.name


def chart_success(m: Metrics, path: Path) -> str:
    fig, ax = _axes()
    labels, rates = ["Initial (1st attempt)", "Final (after retries)"], [m.initial_success_rate, m.final_success_rate]
    ax.barh(labels, rates, height=0.5, color=BLUE)
    for y, r in enumerate(rates):
        ax.text(r + 0.01, y, f"{r:.0%}", va="center", color=INK, fontsize=9)
    ax.set_xlim(0, 1.1)
    ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    ax.invert_yaxis()
    _title(ax, f"Success rate ({m.tasks} tasks)")
    return _save(fig, path)


def chart_latency(result: ExperimentResult, m: Metrics, path: Path) -> str:
    fig, ax = _axes(3)
    ax.hist([a.response.latency_seconds for a in result.attempts], bins=min(20, max(5, m.attempts // 2)),
            color=BLUE, edgecolor=SURFACE, linewidth=2)
    top = ax.get_ylim()[1]
    ax.set_ylim(0, top * 1.18)  # headroom so marker labels sit above the bars, not on them
    ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True))
    for value, name in ((m.attempt_latency.median, "median"), (m.attempt_latency.p95, "P95")):
        ax.axvline(value, color=INK_2, linestyle="--", linewidth=1)
        ax.text(value, top * 1.16, f" {name} {value:.2f}s", color=INK_2, fontsize=8, va="top")
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.grid(axis="x", visible=False)
    ax.set_xlabel("Attempt latency (s)", color=INK_2)
    ax.set_ylabel("Attempts", color=INK_2)
    _title(ax, f"Latency distribution ({m.attempts} attempts)")
    return _save(fig, path)


def chart_failures(m: Metrics, path: Path) -> str:
    items = list(m.attempt_failures.items())
    fig, ax = _axes(max(1.8, 0.45 * len(items) + 1.2))
    if items:
        names, counts = zip(*items)
        ax.barh(names, counts, height=0.5, color=BLUE)
        for y, c in enumerate(counts):
            ax.text(c, y, f" {c}", va="center", color=INK, fontsize=9)
        ax.set_xlim(0, max(counts) * 1.15)
        ax.invert_yaxis()
        ax.xaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True))
    else:
        ax.text(0.5, 0.5, "No failed attempts", ha="center", va="center", color=INK_2, transform=ax.transAxes)
        ax.set_xticks([])
        ax.set_yticks([])
    _title(ax, "Failure categories (all attempts)")
    return _save(fig, path)


def chart_recovery(m: Metrics, path: Path) -> str:
    fig, ax = _axes(1.9)
    parts = [("Passed first try", m.initial_successes, BLUE),
             ("Recovered by retry", m.recovered, ORANGE),
             ("Failed", m.tasks - m.final_successes, AQUA)]
    left = 0
    for name, count, color in parts:
        if count:
            # 2px surface gap between stacked segments; count labels so identity isn't color-only.
            ax.barh([0], [count], left=left, height=0.5, color=color, edgecolor=SURFACE, linewidth=2, label=name)
            ax.text(left + count / 2, 0, str(count), ha="center", va="center", color=SURFACE, fontsize=9,
                    fontweight="bold")
        left += count
    ax.set_xlim(0, m.tasks)
    ax.set_yticks([])
    ax.xaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True))
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.3), ncol=3, frameon=False, fontsize=8, labelcolor=INK_2)
    _title(ax, f"Retry recovery ({m.tasks} tasks)")
    return _save(fig, path)


def write_charts(result: ExperimentResult, m: Metrics, folder: Path) -> list[str]:
    return [
        chart_success(m, folder / "success_rate.png"),
        chart_latency(result, m, folder / "latency_distribution.png"),
        chart_failures(m, folder / "failure_categories.png"),
        chart_recovery(m, folder / "retry_recovery.png"),
    ]


# --- comparison across experiments ---


def label(result: ExperimentResult) -> str:
    return f"#{result.id} {result.config.name}" if result.id is not None else result.config.name


def comparison_rows(results: list[ExperimentResult], metrics: list[Metrics]) -> list[tuple[str, list[str]]]:
    def row(name, fn):
        return name, [fn(r, m) for r, m in zip(results, metrics)]

    return [
        row("Model", lambda r, m: r.config.model.name),
        row("Max attempts", lambda r, m: str(r.config.retry.max_attempts if r.config.retry.enabled else 1)),
        row("Tasks", lambda r, m: str(m.tasks)),
        row("Attempts per task", lambda r, m: f"{m.avg_attempts:.2f}"),
        row("Initial success", lambda r, m: pct(m.initial_success_rate)),
        row("Final success", lambda r, m: pct(m.final_success_rate)),
        row("Retry recovery", lambda r, m: pct(m.retry_recovery_rate)),
        row("Attempt latency mean (s)", lambda r, m: f"{m.attempt_latency.mean:.2f}"),
        row("Attempt latency P95 (s)", lambda r, m: f"{m.attempt_latency.p95:.2f}"),
        row("Task latency mean (s)", lambda r, m: f"{m.task_latency.mean:.2f}"),
        row("Task latency P95 (s)", lambda r, m: f"{m.task_latency.p95:.2f}"),
        row("Total tokens", lambda r, m: num(m.total_tokens)),
        row("Final failures", lambda r, m: ", ".join(f"{k} {v}" for k, v in m.final_failures.items()) or "none"),
    ]


def comparison_warnings(results: list[ExperimentResult]) -> list[str]:
    """Flag comparisons that are not controlled (different data or task sets)."""
    warnings = []
    if len({r.dataset_hash for r in results}) > 1:
        warnings.append("Experiments used different dataset files (hash differs).")
    task_sets = {frozenset(a.task_id for a in r.attempts) for r in results}
    if len(task_sets) > 1:
        warnings.append("Experiments ran different task sets (check --limit); rates are not directly comparable.")
    if len({r.config.model.name for r in results}) > 1:
        warnings.append("Experiments used different models.")
    if any(r.finished_at is None for r in results):
        warnings.append("At least one experiment was interrupted.")
    return warnings


def _grouped_bars(results, series: list[tuple[str, list[float], str]], path: Path, title: str,
                  fmt, ylabel: str, ylim: float | None = None, percent: bool = False) -> str:
    fig, ax = _axes(3.2)
    ax.grid(axis="x", visible=False)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    names = [label(r) for r in results]
    width = 0.6 / len(series)
    for i, (name, values, color) in enumerate(series):
        xs = [x + (i - (len(series) - 1) / 2) * width for x in range(len(names))]
        ax.bar(xs, values, width=width, color=color, edgecolor=SURFACE, linewidth=2, label=name)
        for x, v in zip(xs, values):
            ax.text(x, v, fmt(v), ha="center", va="bottom", color=INK, fontsize=8)
    ax.set_xticks(range(len(names)), names, color=INK_2)
    top = ylim or max(v for _, values, _ in series for v in values) or 1
    ax.set_ylim(0, top * 1.15)
    ax.set_ylabel(ylabel, color=INK_2)
    if percent:
        ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=len(series), frameon=False, fontsize=8,
              labelcolor=INK_2)
    _title(ax, title)
    return _save(fig, path)


def write_comparison(results: list[ExperimentResult], metrics: list[Metrics], base: Path = RESULTS_DIR) -> Path:
    out = base / ("compare_" + "_".join(str(r.id) for r in results))
    (out / "figures").mkdir(parents=True, exist_ok=True)
    rows = comparison_rows(results, metrics)
    headers = ["Metric"] + [label(r) for r in results]

    with (out / "comparison.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows([name, *values] for name, values in rows)

    figures = [
        _grouped_bars(results, [("Initial (1st attempt)", [m.initial_success_rate for m in metrics], BLUE),
                                ("Final (after retries)", [m.final_success_rate for m in metrics], ORANGE)],
                      out / "figures" / "success_by_experiment.png", "Success rate by experiment",
                      lambda v: f"{v:.0%}", "Tasks succeeded", ylim=1.0, percent=True),
        _grouped_bars(results, [("Mean", [m.task_latency.mean for m in metrics], BLUE),
                                ("P95", [m.task_latency.p95 for m in metrics], ORANGE)],
                      out / "figures" / "task_latency_by_experiment.png",
                      "Task latency by experiment (incl. retries + backoff)", lambda v: f"{v:.1f}s", "Seconds"),
    ]
    warnings = comparison_warnings(results)
    table = "\n".join(
        ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
        + [f"| {name} | " + " | ".join(values) + " |" for name, values in rows]
    )
    md = (f"# Comparison: {', '.join(label(r) for r in results)}\n\n"
          + ("".join(f"> **Warning:** {w}\n>\n" for w in warnings) + "\n" if warnings else "")
          + table + "\n\n" + "\n".join(f"![{f}](figures/{f})" for f in figures) + "\n")
    (out / "comparison.md").write_text(md, encoding="utf-8")
    return out


# --- markdown ---


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1%}"


def num(x) -> str:
    return "n/a" if x is None else (f"{x:,.2f}" if isinstance(x, float) else f"{x:,}")


def interpretation(result: ExperimentResult, m: Metrics) -> list[str]:
    lines = []
    if not result.config.retry.enabled:
        lines.append("Retry was disabled, so initial and final success are identical by design.")
    elif m.retry_recovery_rate is None:
        lines.append("Every task succeeded on its first attempt, so retry had nothing to recover.")
    else:
        lines.append(f"Retries recovered {m.recovered} of {m.tasks - m.initial_successes} initially failed tasks "
                     f"({pct(m.retry_recovery_rate)}), raising success from {pct(m.initial_success_rate)} "
                     f"to {pct(m.final_success_rate)} at {m.avg_attempts:.2f} attempts per task.")
    if m.final_failures:
        kind, count = next(iter(m.final_failures.items()))
        lines.append(f"The most common final failure was {kind} ({count} of {m.tasks} tasks).")
    overhead = m.task_latency.mean - m.attempt_latency.mean
    if result.config.retry.enabled and overhead > 0.01:
        lines.append(f"Mean task time exceeded mean attempt time by {overhead:.2f}s: the cost of extra "
                     f"attempts and backoff waits.")
    return lines


def markdown(result: ExperimentResult, m: Metrics, figures: list[str]) -> str:
    c = result.config
    retry = (f"enabled, max {c.retry.max_attempts} attempts, backoff {c.retry.backoff_seconds}s "
             f"(cap {c.retry.max_backoff_seconds}s), on {', '.join(map(str, c.retry.retry_on))}"
             if c.retry.enabled else "disabled")
    al, tl = m.attempt_latency, m.task_latency
    failures = "\n".join(
        f"| {k} | {m.attempt_failures.get(k, 0)} | {m.final_failures.get(k, 0)} |" for k in m.attempt_failures
    ) or "| (none) | 0 | 0 |"
    return f"""# Experiment report: {c.name}

## 1. Experiment information

| | |
|---|---|
| Experiment id | {result.id if result.id is not None else "n/a"} |
| Model | `{c.model.name}` @ {c.model.base_url} |
| Started (UTC) | {result.started_at:%Y-%m-%d %H:%M:%S} |
| Duration | {num(m.duration_seconds)} s |
| App version | {result.app_version} |
| Status | {"complete" if result.finished_at else "interrupted"}{f" (limited to {result.task_limit} tasks)" if result.task_limit else ""} |

## 2. Configuration

| | |
|---|---|
| Temperature | {c.request.temperature} |
| Max tokens | {c.request.max_tokens} |
| Timeout | {c.request.timeout_seconds} s |
| Concurrency | {c.test.concurrency} |
| Retry | {retry} |

## 3. Dataset

`{c.dataset}` version {result.dataset_version}, {m.tasks} tasks run.
SHA-256 `{result.dataset_hash}`

## 4–5. Success

| Metric | Value |
|---|---|
| Initial success rate | {pct(m.initial_success_rate)} ({m.initial_successes}/{m.tasks}) |
| Final success rate | {pct(m.final_success_rate)} ({m.final_successes}/{m.tasks}) |
| Failure rate | {pct(m.failure_rate)} |
| Attempts | {m.attempts} ({m.avg_attempts:.2f} per task) |

## 6. Latency (seconds)

| | Mean | Median | P95 | Min | Max |
|---|---|---|---|---|---|
| Per attempt | {al.mean:.3f} | {al.median:.3f} | {al.p95:.3f} | {al.min:.3f} | {al.max:.3f} |
| Per task (incl. retries + backoff) | {tl.mean:.3f} | {tl.median:.3f} | {tl.p95:.3f} | {tl.min:.3f} | {tl.max:.3f} |

Throughput: {num(m.throughput_tasks_per_min)} tasks/min.

## 7. Failure distribution

| Failure type | All attempts | Final (task failed) |
|---|---|---|
{failures}

## 8. Retry recovery

{m.recovered} recovered of {m.tasks - m.initial_successes} initially failed tasks: recovery rate {pct(m.retry_recovery_rate)}.

## 9. Token usage

| Input | Output | Total | Per task |
|---|---|---|---|
| {num(m.input_tokens)} | {num(m.output_tokens)} | {num(m.total_tokens)} | {num(m.tokens_per_task)} |

Failed requests usually report no tokens, so totals cover successful responses only.

## 10. Charts

{chr(10).join(f"![{f}](figures/{f})" for f in figures)}

## 11. Interpretation

{chr(10).join(f"- {line}" for line in interpretation(result, m))}

## 12. Limitations

- Single run of {m.tasks} tasks: small samples give wide uncertainty; repeat runs before drawing conclusions.
- Latency is measured client-side and includes network time from this machine.
- Shared or free-tier endpoints vary with other users' load; results reflect the time of the run.
- Validation is deterministic and strict (exact match is case-sensitive); a correct but differently formatted answer counts as a failure.
"""
