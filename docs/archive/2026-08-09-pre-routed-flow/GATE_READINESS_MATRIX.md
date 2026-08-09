# Gate readiness matrix

Audit date: 2026-08-09. This is a read-only status snapshot, not an approval
record. A test-created SQLite approval is not treated as project evidence; a
gate can pass only when an authorized operator records an immutable approval
for the exact run intent and supplies the corresponding evidence.

| Gate | Required evidence keys | Repository evidence status | Durable approval status | Current result |
|---|---|---|---|---|
| CMP-GATE | `compiler_golden` | Compiler fixture, artifact audit, and tests present | Successor approval `f5432465-aee2-4602-b41f-66a6c696c2dc` | Passed for successor run |
| BEX-GATE | `controlled_execution` | Typed providers and disposable-Bench execution evidence present | Successor approval `a11c1d89-9274-4536-b455-23a81b9dc85c` | Passed for successor run |
| VAL-GATE | `safety_validation` | Safety policy, schema-diff, and fail-closed tests present | Successor approval `eac4669b-88dc-44db-8b51-9ec81677d4e5` | Passed for successor run |
| VER-GATE | `backend_verification` | 83-case backend/permission/REST report and live receipt present | Successor approval `2a996ced-d50a-494a-b021-b751e1eec977` | Passed for successor run |
| FE-GATE | `frontend_verification` | Local and disposable-Bench browser/session evidence present | Successor approval `283b8f0f-88ed-42c0-bf60-f7c900242479` | Passed for successor run |
| ORC-GATE | `lifecycle_receipt` | Durable lifecycle and READY business receipt evidence present | Successor approval `bebff439-46e6-4f47-abec-f2f685874d43` | Passed for successor run |
| LLM-GATE | `bounded_model_equivalence` plus model thresholds | Free-form Gemma4 and Gemini proposal equivalence remain negative; normalized Gemma4 and Gemini corpus scores fail thresholds (Gemini 0.714285 decision accuracy, 0.0 precision, 0.142857 recall, 8 unsafe admissions) | None recorded | Blocked: evidence and approval |
| PRV-GATE | `provider_compatibility` | Native typed provider/frappectl path is authoritative and proven by the successor run; Commander is installed only as an optional disposable experiment and its missing role/REST equivalence does not gate native V1 | Native provider evidence is covered by VER/ORC/MOD approvals | Not applicable to native V1 profile; Commander remains optional |
| PI-GATE | `pi_compatibility` | Supported Node 22/Pi 0.82.1 isolated Ollama Gemma4 session proved with all authority-bearing surfaces disabled | Successor approval `f41c36c0-f6a4-48da-938a-ccfcb27940ab` | Passed for supported runtime; default Node 26 remains unsupported |
| MOD-GATE | `safe_modification`, `destructive_no_mutation` | V2 preservation and blocked destructive evidence present; formal gate binding tested | Successor approval `8d2ff34e-b8dd-405c-b2b1-8f8749e65a70` | Passed for successor run |
| REL-GATE | `release_pack` plus thresholds, complete corpus, zero unsafe admissions, zero uncontrolled command paths | Release pack now includes normalized Gemma4 and Gemini corpus fixtures plus gpt-oss/Qwen failures; Gemma4 and Gemini fail all thresholds (Gemini latest 0.714285/0/0.142857 with 8 unsafe admissions), so no candidate satisfies the release thresholds | None recorded | Blocked: failing thresholds, safety invariant, and approval |

The CLI command `frappe-harness gate-evaluate` can evaluate one row against
explicit evidence and any exact durable approval, while
`frappe-harness gate-approve` records the separate human decision. Neither
command infers approval or promotes a release.

The configured candidate thresholds are decision accuracy `>= 0.90`, reason
precision `>= 0.80`, reason recall `>= 0.80`, and unsafe-admission rate `== 0`.
They come from `ReleaseThresholds` in `release_evaluation.py`; this snapshot
does not relax or reinterpret them.

`evaluate_milestone_gate_for_run` now treats those scalar values as diagnostic
only for `REL-GATE`: a run-backed release decision must receive a
`ReleaseEvidenceResolution` derived by `resolve_release_evidence` from the
normalized candidate comparison artifacts. Without that resolution the
run-backed gate fails closed, even when all scalar flags are set.
