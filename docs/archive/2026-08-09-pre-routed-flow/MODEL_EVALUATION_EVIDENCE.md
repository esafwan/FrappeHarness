# Model evaluation evidence

This is a normalized, secret-free record—not a release approval.

On 2026-08-09, `gemma4:latest` completed the bounded local Ollama smoke
contract and was exercised against the golden Task Tracker proposal boundary.
The strict two-attempt extractor escalated after schema/entity/Link/screen
violations. No proposal was admitted, no command/provider capability was
exposed, and no raw model output was retained. The normalized result is
`fixtures/evaluation/results/gemma4_latest_2026-08-09.json`.

After adding a fixed Ollama seed, a separate reference-guided prompt containing
the already-confirmed specification reproduced the exact hash in one attempt.
That is recorded separately as
`gemma4_latest_reference_guided_2026-08-09.json`; it proves deterministic
typed copying, not free-form natural-language equivalence, so it is explicitly
not release-eligible.

On the same date, one bounded `gpt-oss:20b` natural-language proposal attempt
also escalated at the strict proposal boundary (`repair_attempts_exhausted`,
zero unsafe admissions). Only the normalized record
`gpt_oss_20b_2026-08-09.json` is retained; no prompt/response text is stored.

A separate batched 28-case V1 corpus attempt was time-bounded at eight seconds;
the local Ollama call failed before a valid prediction payload was produced.
The normalized record `gpt_oss_20b_corpus_2026-08-09.json` records
`ollama_run_failed`, zero unsafe admissions, and no retained output. It is not a
partial score and cannot satisfy release thresholds.

The strongest installed candidate, `qwen3-coder-next:latest`, was also given a
bounded 28-case batch attempt. Ollama produced no valid prediction payload;
`qwen3_coder_next_corpus_2026-08-09.json` records `ollama_run_failed`, zero
unsafe admissions, and no retained output. This is negative evidence only.

The repository now also contains an optional `OllamaCorpusPredictionAdapter`
for bounded per-case corpus evaluation. It accepts only the exact
`case_id`/`decision`/`reason_codes` object, evaluates through the existing
deterministic scorer, and converts any generation or parsing failure into a
code-only negative result. The complete Gemma4 corpus score is non-release
evidence because its thresholds fail and its unsafe-admission rate is nonzero.

The bounded `gemma4:latest` 28-case adapter run produced a complete but failing
score: decision accuracy `0.25`, reason precision `0`, reason recall `0.142857`,
and unsafe-admission rate `0.464285`. The evaluator records the corresponding
unsafe admission count as well; this remains negative evidence only and does
not satisfy EVA-03 or REL-GATE.

The evaluation helper can compare future candidates deterministically, but no
default model or release decision is asserted until a candidate has a complete
corpus score meeting every threshold.

A follow-up `safety_rubric` prompt run is retained as
`fixtures/evaluation/results/gemma4_rubric_2026-08-09.json`. The rubric adds
decision semantics and safety wording without exposing expected labels or
changing thresholds; the complete result still fails (decision accuracy
`0.428571`, reason precision `0`, reason recall `0.142857`, unsafe admissions
`14`). It is negative evidence only and retains no model output.

The aggregate record `comparison_blocked_2026-08-09.json` makes that conclusion
explicit: three candidates were observed, Gemma4 has a complete but failing
score, the other two runs are negative, zero candidates pass thresholds, no
model is recommended, and release eligibility is false.

The process-local `GEMINI_API_KEY` was also checked on 2026-08-09 with a
single data-only JSON connectivity request. `models/gemini-3.5-flash-lite`
returned the required JSON shape. The provider-neutral Gemini corpus adapter
now uses the same strict object schema, one schema-only retry, and no-raw-output
boundary as Ollama. A bounded 28-case run completed but failed all thresholds
(latest observed decision accuracy `0.642857`, reason precision `0`, reason
recall `0.142857`, unsafe admissions `10/28`). This is candidate evidence only;
it does not support LLM-GATE or REL-GATE approval.

The latest corpus result is now retained as the normalized, secret-free
`fixtures/evaluation/results/gemini_3_5_flash_lite_corpus_2026-08-09.json` and
imports successfully through `candidate_from_normalized_result`. It scored
decision accuracy `0.714285`, reason precision `0`, reason recall `0.142857`,
and 8 unsafe admissions; it fails every release threshold. The earlier
process-observed run remains historical context only.

The same model was then exercised through the existing bounded proposal
boundary using a representative Task Tracker requirement. It made two strict
attempts and escalated on `expected_array` validation errors; exact golden
equivalence was false and no proposal entered admission. The normalized,
secret-free record is `fixtures/evaluation/results/gemini_3_5_flash_lite_2026-08-09.json`.
