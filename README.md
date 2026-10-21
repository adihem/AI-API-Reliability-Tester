# AI API Reliability Tester

Python developer tool that measures AI API reliability through controlled, reproducible experiments.

**Main question:** how does bounded retry behavior affect the reliability and latency of AI API requests?

An experiment is defined in YAML, run against a fixed evaluation dataset, and every attempt is stored in SQLite. Metrics (initial/final success rate, retry recovery, latency P50/P95, failure categories, token usage) are written to CSV/JSON/Markdown reports with matplotlib charts.

```bash
python -m tester run --config config/baseline.yaml   # available from Phase 9
```

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Project setup | done |
| 1 | Configuration + data models | done |
| 2 | Dataset | done |
| 3 | API client (OpenAI-compatible) | done |
| 4 | Validation | done |
| 5 | Runner (sequential, `--limit`) | done |
| 6 | SQLite persistence | done |
| 7 | Retry engine | done |
| 8 | Metrics + reporting | done |
| 9 | CLI | done |

## Setup

Requires Python 3.12+.

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1         # Windows PowerShell
# source .venv/Scripts/activate    # Git Bash;  source .venv/bin/activate on macOS/Linux
pip install -e ".[dev]"
cp .env.example .env               # then put your real API key in .env
```

## Verify

```bash
pytest
python -m tester version
```

## Usage

```bash
python -m tester validate -c config/baseline.yaml          # check config + dataset, no API calls
python -m tester probe    -c config/baseline.yaml -t json_001   # one real request, printed
python -m tester run      -c config/baseline.yaml --limit 3     # run (all tasks without --limit)
python -m tester list                                      # stored experiments
python -m tester summary  -e baseline                      # metrics of latest 'baseline' (or an id)
python -m tester compare  baseline retry_1 retry_2         # side-by-side table + charts
python -m tester export   -e 5                             # rebuild a report folder from the DB
```

Every attempt is stored in `results/experiments.db`; each run writes
`results/<date>_<name>_<id>/` with `config.yaml`, `results.csv`, `results.json`,
`summary.json`, `report.md` and `figures/`.

## Layout

```text
src/tester/   application package (modular monolith)
tests/        pytest suite (fake API, no real calls)
config/       experiment YAML files            (Phase 1)
datasets/     fixed evaluation tasks           (Phase 2)
results/      experiment outputs (git-ignored)
docs/         protocol, metrics, limitations   (Phase 11)
```

## Security

API keys live only in `.env`, which is git-ignored. Keys and auth headers are never logged.
