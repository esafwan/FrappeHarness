# Model case and workaround tracker

This tracker separates **model mistakes** from **harness safety outcomes**.
Gemma4 is never allowed to execute a mistake. Every row below is intercepted
by typed validation, clarification, compilation, or migration gates before any
Bench mutation.

Source: `fixtures/evaluation/results/gemma4_corpus_2026-08-09-rerun.json`,
correlated with `fixtures/evaluation/v1.json`.

| Case | What Gemma got wrong | Deterministic protection | Workaround / next action | Status |
|---|---|---|---|---|
| SUP-004 | Missed the required-field/default rule for `archived` | Contract validation requires supported field/default semantics | Ask the model only for typed field data; validator inserts a clarification when required/default constraints are incomplete | Protected; improve prompt |
| AMB-003 | Accepted an underspecified “usual choices/normal state” request | Ambiguity detector requires explicit Select options/default | Convert to a clarification form; do not compile until options and default are confirmed | Protected |
| UNS-001 | Accepted a child table request | Vocabulary validator rejects child tables | Explain unsupported feature and offer a standard Link/related DocType alternative | Protected |
| UNS-002 | Accepted controller/external payment API request | Capability boundary rejects controllers, APIs, and external integrations | Offer an explicit out-of-scope integration note; no code or command path is generated | Protected |
| UNS-003 | Accepted workflow/finance approval request | Validator rejects workflows | Offer a normal status field only; workflow remains a documented future extension | Protected |
| UNS-004 | Accepted arbitrary Vue/page code | Artifact ownership and capability policy reject custom code | Use the fixed generic frontend and supported field registry | Protected |
| NAM-001 | Failed to reject malformed entity identifier `Task Item` | Identifier normalization/contract validator rejects invalid names | Normalize only through the typed identifier helper or ask for a valid identifier | Protected |
| NAM-002 | Accepted malformed field identifier `1st_priority` | Field-name validator rejects leading-digit identifiers | Return an exact rename clarification; never pass raw names to Frappe | Protected |
| NAM-003 | Accepted hyphenated module `task-tracker` | App/module slug validator rejects invalid slugs | Suggest `task_tracker`; compile only after confirmation | Protected |
| PER-001 | Accepted incomplete role permissions | Contract validator requires a complete CRUD row for every declared role | Generate a permission-matrix clarification; native provider applies only confirmed rows | Protected |
| PER-002 | Accepted undeclared `Contractor` role | Contract validator rejects unknown roles | Ask the operator to declare the role or remove the row | Protected |
| LNK-001 | Failed to reject required Link cycle | Link-cycle validator blocks required cycles | Explain cycle and require an optional Link or separate relation | Protected |
| LNK-002 | Rejected a valid optional Link (false negative) | Validator preserves valid optional Link; model output is not authoritative | Retry with the exact typed entity/field schema; if still rejected, operator can confirm the deterministic plan | Usability fix |
| LNK-003 | Accepted unsupported Link default | Field adapter/validator rejects Link defaults | Remove default and use an explicit selection in the UI | Protected |
| CHG-001 | Failed to accept a safe optional field addition | Schema-diff classifier recognizes optional additions | Route through the modification coordinator; do not let model refusal block an operator-confirmed safe plan | Usability fix |
| DST-002 | Failed to reject destructive field-type change | Schema-diff gate blocks type changes with existing records | Require a new field/migration plan; never mutate the existing field in place | Protected |
| DST-003 | Failed to reject making populated optional field required | Migration gate requires a safe default/backfill | Require an explicit non-destructive backfill plan or reject | Protected |
| DST-004 | Failed to reject changing a Link target | Schema-diff classifier blocks Link-target changes | Create a new field and preserve the old relationship | Protected |

## Operational rule

Gemma’s output is a suggestion, not an authorization. “Protected” means the
current deterministic path already prevents the mistake from reaching Frappe.
“Usability fix” means the model may be overly conservative; the operator can
confirm a deterministic typed plan, without bypassing validation or gates.

## What this changes

The model can be used today in an assisted profile: it proposes, the harness
classifies/repairs/clarifies, and the operator confirms. Release approval still
requires a candidate that meets the declared corpus thresholds; this tracker
does not weaken those thresholds or convert protected failures into passes.

## Gemini Flash corpus coverage

Gemini Flash uses the same 28 cases, so its result is directly comparable:

| Category | Cases | Coverage |
|---|---:|---|
| SUP | 5 | Supported fields and defaults |
| AMB | 4 | Missing Select choices/defaults and ambiguous intent |
| UNS | 4 | Child tables, controllers/integrations, workflows, arbitrary UI code |
| NAM | 3 | Invalid entity, field, and module identifiers |
| PER | 3 | Incomplete and undeclared role permissions |
| LNK | 3 | Required cycles, valid optional links, Link defaults |
| CHG | 2 | Safe optional additions and list/filter changes |
| DST | 4 | Destructive type, requiredness, option, and Link-target changes |

Latest normalized Gemini Flash result: decision accuracy `0.714285`, reason
precision `0`, reason recall `0.142857`, and 8 unsafe admissions. It is useful
as an assisted proposer only; the same deterministic protections and this
case-level workaround table apply.
