# ADR-005: Data-only candidate model evaluation

Status: accepted for V1 evaluation tooling; not a release approval.

Candidate model output is supplied to a pure scorer against the versioned
evaluation corpus. Successful scores retain only normalized metrics and
threshold results. Malformed or negative runs (for example a Gemma4 strict
schema failure) retain a short failure code and no raw model output.

Previously normalized negative-result records can be imported only when their
schema version, corpus identity, failure code, unsafe count, and
`raw_output_retained: false` attestation validate. An imported negative result
never receives an invented score.

Candidate comparison is deterministic: reports are ordered by model name and a
recommendation is produced only from threshold-passing reports. This helper is
evidence for a later release decision; it does not select or ship a production
default model, rejects comparisons that mix corpus identities, and does not
invoke models, tools, Bench, or Ollama.
