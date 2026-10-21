# Metrics

How every number in a report is defined and computed. Implementation: [`src/tester/metrics.py`](../src/tester/metrics.py).

## Units of measurement

| Term | Meaning |
|---|---|
| **Attempt** | One HTTP request to the API plus validation of its response. Stored as one row in `requests`. |
| **Task** | One item of the dataset (e.g. `json_001`). A task has 1..`max_attempts` attempts. |
| **Experiment** | One run of a config over the selected tasks. |

Every attempt is stored, so all metrics can be recomputed from the database at any time (`tester export`).

## Outcome of an attempt

An attempt passes four levels in order and stops at the first failure:

| Level | Check | Failure type |
|---|---|---|
| 1. Transport | Connection made, response received before the timeout | `TIMEOUT`, `CONNECTION_ERROR` |
| 2. API response | HTTP 2xx and a well-formed chat completion | `AUTHENTICATION_ERROR` (401/403), `RATE_LIMIT` (429), `BAD_REQUEST` (other 4xx), `SERVER_ERROR` (5xx), `INVALID_RESPONSE` (2xx, malformed body), `UNKNOWN_ERROR` |
| 3. Format | Non-empty; for JSON tasks, parses as JSON of the right shape | `EMPTY_RESPONSE`, `FORMAT_ERROR` |
| 4. Correctness | Answer matches the task's expected value | `VALIDATION_ERROR` |

An attempt **succeeds** only if it clears all four levels. HTTP 200 alone is not success.

### Validators

| Task `validation` | Passes when | Notes |
|---|---|---|
| `non_empty` | Response has non-blank text | Free-text tasks |
| `exact_match` | Normalized response == `expected` | Case-sensitive. Strips surrounding whitespace, quotes, backticks, `*` and one trailing period: `**Paris.**` matches `Paris`. |
| `json` | Response parses and equals `expected` | JSON-type equality: `true` ≠ `1`, `"21"` ≠ `21`, `21` = `21.0`; key order ignored; array order matters; extra keys fail. |
| `schema` | Response is a JSON object with the given fields and JSON types | Pydantic strict types; extra fields allowed; `integer` rejects `true` and `30.5`. |

A surrounding Markdown code fence (```` ```json … ``` ````) is stripped before JSON parsing. The raw text is stored, so results can be re-scored strictly later.

### Retryable failures

A failure is **retryable** if its type is in the config's `retry.retry_on` list. Default:

| Retryable | Not retryable |
|---|---|
| `TIMEOUT`, `CONNECTION_ERROR`, `RATE_LIMIT`, `SERVER_ERROR` | everything else: auth and request errors won't change on retry; wrong answers are the model's, not the transport's |

## Success metrics

With *N* = number of tasks:

| Metric | Formula |
|---|---|
| Initial success rate | tasks whose **first** attempt succeeded ÷ *N* |
| Final success rate | tasks whose **last** attempt succeeded ÷ *N* |
| Failure rate | 1 − final success rate |
| Recovered tasks | tasks whose first attempt failed and last attempt succeeded |
| Retry recovery rate | recovered ÷ tasks whose first attempt failed. `n/a` when nothing failed initially. |
| Average attempts | total attempts ÷ *N* |

A success always ends a task's attempts, so "last attempt succeeded" is the same as "any attempt succeeded".

## Latency metrics

Two latencies are reported, because retry cost only shows in the second:

| Metric | Measured from → to | Includes |
|---|---|---|
| **Attempt latency** | just before the HTTP request → response or error | one API call (client-side, includes network) |
| **Task latency** | start of first attempt → end of last attempt | all attempts **and** backoff waits |

Each is summarized as mean, median, P95, min and max.

**P95** uses linear interpolation between closest ranks (`statistics.quantiles(..., method="inclusive")`), the same as the numpy/pandas default. With one value, P95 is that value.

**Timeout semantics:** httpx applies `timeout_seconds` to each phase (connect, each read), not as one wall-clock budget. A response that keeps sending data can therefore take slightly longer than the timeout and still succeed. In the 1.5 s runs the slowest successful attempt took 2.21 s.

## Failure distribution

| Metric | Counts |
|---|---|
| Attempt failures | every failed attempt by type, including ones later recovered |
| Final failures | tasks that ultimately failed, by the type of their last attempt |

Example: a task that times out and then succeeds adds 1 to attempt failures (`TIMEOUT`) and 0 to final failures.

## Tokens

`input_tokens`, `output_tokens` and `total_tokens` are summed over all attempts that reported usage. Failed requests (timeouts, 429s) usually report none, so token totals cover successful responses. `tokens_per_task` = total ÷ *N*.

## Throughput

`tasks / (experiment end − experiment start) × 60` tasks per minute. Configs set `min_interval_seconds: 3.5`, so throughput is capped near 17 tasks/min by design. It measures the pacing, not the API's capacity, until a concurrent runner exists.

## Comparing experiments

`tester compare` puts these metrics side by side and warns when a comparison is not controlled:

- different dataset hash
- different task sets (e.g. different `--limit`)
- different models
- an interrupted experiment
