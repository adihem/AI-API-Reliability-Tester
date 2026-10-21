# Limitations

What the results in [results.md](results.md) can and cannot support.

## Sample size

- **Few failures.** Group B produced 7 failed attempts in total; group A produced none at the transport level. "Recovered 5 of 5" is a strong signal, not a precise rate: with n = 5, the true recovery rate could be well below 100 %.
- **One run per configuration.** Each config ran once. Run-to-run variance is large (P95 latency ranged from 1.5 s to 5.8 s between runs two minutes apart), so differences between single runs, especially between `timeout_a2` and `timeout_a3`, are within noise.
- **30 tasks.** Enough to exercise every category and validator, too few for per-category conclusions beyond "classification is fastest".

Repeating each configuration several times, at different times of day, would turn these observations into estimates with uncertainty.

## Conditions are not fixed

- **Shared, free-tier endpoint.** Latency and failures depend on other users' load on OpenRouter and the upstream provider. Runs in one group were back to back (about 2 minutes apart) to keep conditions similar, but not identical.
- **One model, one provider, one day.** All final results come from `minimax/minimax-m3.1-flash:free` on 2026-10-21. Another model, provider or hour could fail differently. The earlier Gemma pool returned 429 on every request.
- **Model outputs vary.** At temperature 0.2 the model is not deterministic (`transform_005` was wrong once in six runs). Reruns reproduce the procedure, not the exact numbers.

## Induced failures

- **Group B's failures were caused by the test.** A 1.5 s timeout is shorter than a production client would use. It turns slow responses into failures so retry can be measured. Real-world failure types (5xx, 429, network drops) may recover differently, for example 429s need longer backoff.
- **Pacing hides rate-limit behavior.** Requests are spaced 3.5 s apart to stay under the account's 20/min limit, so the results say nothing about behavior under bursty load.

## Measurement

- **Timeout is per phase, not total.** httpx applies `timeout_seconds` to connect and to each read separately. Some "1.5 s" requests took up to 2.2 s. A strict wall-clock deadline would produce slightly more timeouts.
- **Latency is client-side.** It includes network time from one home connection on Windows, not just server time.
- **Token counts come from the provider** and include ~220 tokens of provider-side overhead per request. Timed-out requests report no tokens even though the provider may have done (and billed) the work, so "+6 % tokens" understates retry's real compute cost.
- **Validation is strict and deterministic.** Exact match is case-sensitive and does not accept paraphrases. A correct answer in a different format counts as wrong. Code fences around JSON are accepted, which is a deliberate leniency.

## Not implemented

- **Concurrency.** The runner is sequential; `concurrency` above 1 is rejected. Concurrency effects (research question 4) were not studied.
- **Backoff details.** No jitter and no `Retry-After` header handling. They matter mostly for 429s and concurrent clients, neither of which occurred here.
- **Retrying wrong answers.** `VALIDATION_ERROR`/`FORMAT_ERROR` are not retried by default. Whether asking again fixes model errors was not tested (it can be enabled via `retry_on`).
- **Second model/provider** (research question 5) was not run in the final experiments.

## Scope

This is a measurement tool and a small controlled study, not a benchmark. It compares retry policies under the observed conditions; it does not rank models or providers.
