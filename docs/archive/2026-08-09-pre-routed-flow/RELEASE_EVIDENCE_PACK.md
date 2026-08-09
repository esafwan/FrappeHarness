# V1 release evidence pack

Status: not released. This checklist records current evidence and remaining
decision blockers; it does not substitute for product approval.

Verified controls:

- `python3 -m pytest -q` — Python contract/lifecycle/provider suite green.
- From the repository root, `npm --prefix frontend run verify` and
  `npm --prefix frontend run test:e2e` — fixed frontend green.
- `git diff --cached --check` — staged artifact hygiene clean.
- Live disposable run `82ef49c3-e40a-4a99-bd7d-b2c0a4b806b3` — `READY`/`SUCCEEDED`, raw metadata match, 82/82 backend/permission/REST, browser/session/CSRF evidence.
- Gemma4 smoke — bounded Ollama review contract green; free-form golden extraction escalated after two strict-schema attempts with zero unsafe admissions.
- One bounded `gpt-oss:20b` natural-language proposal attempt also escalated at the strict boundary with zero unsafe admissions; only normalized evidence is retained.
- Gemma4 completed a bounded 28-case corpus run, but failed every release threshold (decision accuracy 0.25; unsafe-admission rate 0.464285; 13 unsafe admissions); the normalized score is evidence only and is not a release claim.
- Separate gpt-oss:20b and qwen3-coder-next 28-case attempts failed before valid predictions (`ollama_run_failed`); they produced no partial scores and cannot satisfy thresholds.
- The strongest installed `qwen3-coder-next:latest` candidate likewise produced no valid 28-case payload; normalized failure evidence is retained only.
- A bounded Gemini 3.5 Flash-Lite 28-case run is retained as normalized per-case evidence at `fixtures/evaluation/results/gemini_3_5_flash_lite_corpus_2026-08-09.json`; the latest run scored 0.714285 decision accuracy, 0 reason precision, 0.142857 reason recall, and 8 unsafe admissions. It fails all thresholds and is included in the validated blocked comparison.
- Artifact and capability tests — no arbitrary shell/REST/model capability or retained secrets.
- Operational budget contract tests — BenchRunner and Ollama timeout/token caps, lifecycle retry cap, output byte caps, and numeric model-cost cap; provider-specific pricing remains caller-supplied and no raw model output is retained.
- Corpus adapter contract tests — one schema-only retry is bounded per malformed Ollama case; retry feedback contains neither gold labels nor raw model output, and persistent failures remain code-only.
- `frappe-harness release-resolve` now derives and emits blocked release facts from the normalized aggregate/candidate artifacts; it returns nonzero while blockers remain and does not record approval.

Release blockers:

- Free-form natural-language Gemma4 equivalence has not met the exact golden
  hash gate; reference-guided copying is explicitly not release-eligible.
- Candidate-model comparison has no threshold-passing natural-language run.
- Gemini is now retained through the normalized per-case result boundary and
  included in the blocked comparison; it still fails every threshold and does
  not provide a release candidate.
- `resolve_release_evidence` now derives candidate-threshold, corpus-coverage,
  unsafe-admission, and comparison-integrity facts from the normalized
  candidate artifacts and aggregate; the aggregate now carries an explicit
  `uncontrolled_command_paths_zero` boolean, with any caller flag treated only
  as a consistency assertion. The current resolver returns
  blockers and no release eligibility because no candidate passes.
- Comparison aggregate counts are type-checked as strict non-negative integers;
  JSON booleans cannot be accepted through Python's `bool`/`int` equivalence.
- The normalized corpus score covers decision accuracy, reason precision and
  recall, and unsafe admission. P9 also calls for structural accuracy,
  clarification quality, validation/repair rates, and build/test success;
  those dimensions are not yet present in the per-candidate score artifact.
- V2 modification and destructive no-mutation evidence are not yet composed
  into a final approved release decision.

REL-GATE invariant (not yet satisfied): a release decision requires a
threshold-passing candidate comparison with no missing or unknown corpus case
IDs, `unsafe_admission_rate == 0`, and explicit evidence that no uncontrolled
command path or destructive mutation passed a safety gate. A green smoke test,
negative model run, or live READY receipt alone cannot satisfy this invariant.

Therefore `REL-GATE` remains intentionally open.
