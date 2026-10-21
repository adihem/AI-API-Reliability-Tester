# Test protocol

How experiments are designed, run and reproduced.

## Research question

> How does bounded retry behavior affect the reliability and latency of AI API requests?

Sub-questions:

1. Which failure types occur, and how often?
2. How many initial failures can retries recover?
3. How much latency and API usage does retrying add?

## Controlled variables

Within a comparison only **one** variable changes. Everything else is held fixed:

| Held constant | How it is enforced |
|---|---|
| Dataset | Same file for every run; its SHA-256 is stored per experiment. `compare` warns if hashes differ. `.gitattributes` keeps the file's bytes identical across machines. |
| Task set | Full 30 tasks (no `--limit`). `compare` warns on different task sets. |
| Model and endpoint | Same `model.name` and `base_url`. `compare` warns on different models. |
| Prompt parameters | `temperature: 0.2`, `max_tokens: 500` |
| Validation | Deterministic validators, chosen per task in the dataset |
| Request pacing | `min_interval_seconds: 3.5` (≈17 tasks/min) |
| Execution | Sequential, one request in flight (`concurrency: 1`) |
| Backoff | `backoff_seconds: 1`, doubling, cap 30 s; retry on `TIMEOUT`, `CONNECTION_ERROR`, `RATE_LIMIT`, `SERVER_ERROR` |

### Why pacing

The free OpenRouter tier allows about 20 requests per minute. Unpaced, a 30-task run sends ~30 requests in ~30 s and the account limit returns 429s. Those failures come from the test setup, not from API reliability, so they would contaminate the data. Pacing task starts 3.5 s apart keeps runs under the cap.

For the same reason, `run` **stops** (exit code 4, experiment marked unfinished) when the provider reports its daily quota is used up, rather than recording every remaining request as a failure.

## Dataset

[`datasets/prompts.json`](../datasets/prompts.json), version 1.0, 30 tasks:

| Category | Tasks | Validation |
|---|---|---|
| simple_text | 10 | 8 `exact_match` (facts, arithmetic), 2 `non_empty` (free text) |
| structured_output | 10 | 6 `json` (exact value), 4 `schema` (fields and types) |
| classification | 5 | `exact_match` labels (BUG/FEATURE/DOCUMENTATION, sentiment) |
| transformation | 5 | `exact_match` (uppercase, word reversal, °C→°F, date format, vowel count) |

Every prompt states the required output format ("Reply with only the label", "Return only JSON"), so exact-match validation is fair. The loader rejects duplicate IDs and `expected` values that don't fit the validation type.

## Experiments run

All on `minimax/minimax-m3.1-flash:free` via OpenRouter, 2026-10-21, 30 tasks each:

| Id | Config | Timeout | Max attempts | Purpose |
|---|---|---|---|---|
| 1 | `baseline` | 20 s | 1 | Reference reliability |
| 2 | `retry_1` | 20 s | 2 | Retry under normal timeout |
| 3 | `retry_2` | 20 s | 3 | Retry under normal timeout |
| 4 | `timeout_a1` | 1.5 s | 1 | Short timeout, no retry |
| 5 | `timeout_a2` | 1.5 s | 2 | Short timeout, 1 retry |
| 6 | `timeout_a3` | 1.5 s | 3 | Short timeout, 2 retries |

Runs 1–3 produced no transport failures, so retries had nothing to act on. Runs 4–6 use a **1.5 s timeout, set at the baseline's P95 latency (1.53 s)**. This turns the slowest ~5–10 % of responses into `TIMEOUT` failures, a realistic transient fault that retries can act on.

## Procedure

```bash
# 0. Setup (once)
python -m venv .venv && source .venv/Scripts/activate
pip install -e ".[dev]"
cp .env.example .env            # put the API key in .env, never on the command line
pytest                          # 200+ tests, no network

# 1. Check configs and dataset (no API calls)
python -m tester validate -c config/baseline.yaml

# 2. Smoke test: one request
python -m tester probe -c config/baseline.yaml -t classification_001

# 3. Run each experiment on all 30 tasks (~2 min each with pacing)
python -m tester run -c config/baseline.yaml
python -m tester run -c config/retry_1.yaml
python -m tester run -c config/retry_2.yaml
python -m tester run -c config/timeout_a1.yaml
python -m tester run -c config/timeout_a2.yaml
python -m tester run -c config/timeout_a3.yaml

# 4. Compare within each group
python -m tester compare baseline retry_1 retry_2
python -m tester compare timeout_a1 timeout_a2 timeout_a3
```

Rules while running:

- Do not use `--limit` for data that will be compared. It exists for debugging.
- Run a comparison group back to back, so all its runs see similar provider load.
- If a run is interrupted or quota-stopped, rerun the whole experiment. Don't merge partial runs.

## What is recorded

Per experiment (`experiments` table and `summary.json`): name, model, endpoint, full config as JSON, dataset version and SHA-256, app version, `--limit` used, start and finish times.

Per attempt (`requests`, `responses`, `errors` tables and `results.csv`): task, attempt number, start/finish timestamps, latency, HTTP status, response text, token counts, validation verdict, failure type, error message, retryable flag.

API keys and `Authorization` headers are never stored or logged. Error messages are scrubbed of the key before storage.

## Reproducing a run

1. Check out the code the experiment ran on. All six runs here used the code committed as `b838911` (`app_version` 0.1.0 in `summary.json`).
2. Confirm `datasets/prompts.json` matches the stored `dataset_hash`: `python -m tester validate -c <config>` prints it.
3. Use the stored `config.yaml` from the experiment's result folder.
4. `python -m tester run -c <that config.yaml>`.

Model outputs and provider load are not deterministic, so a rerun reproduces the **procedure**, not the exact numbers. See [limitations.md](limitations.md).
