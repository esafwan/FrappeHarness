"""Optional local Ollama bridge for smoke-testing the model review contract.

This module is intentionally separate from the core model boundary. It uses the
local Ollama HTTP API to turn a typed proposal payload into a typed review
result, then validates that the returned data stays bound to the submitted
payload.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .model_gateway import (
    Clarification,
    FindingSeverity,
    ProposalPayload,
    ProposalReview,
    ValidationFinding,
    validate_review,
)
from .proposal_contract import ProposalIssue
from .operational_budget import ModelBudgetExceeded, ModelUsageLedger, OperationalBudget


@dataclass(frozen=True)
class OllamaGatewayConfig:
    model: str = "gemma4:latest"
    host: str = "http://127.0.0.1:11434"
    timeout_seconds: int = 120
    budget: OperationalBudget = field(default_factory=OperationalBudget)
    usage_ledger: ModelUsageLedger | None = field(default=None, repr=False, compare=False)
    prompt_rate_per_1k_usd: float | None = field(default=None, repr=False, compare=False)
    completion_rate_per_1k_usd: float | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (self.prompt_rate_per_1k_usd is None) != (self.completion_rate_per_1k_usd is None):
            raise ValueError("Ollama usage pricing requires both prompt and completion rates")
        if self.usage_ledger is not None and self.prompt_rate_per_1k_usd is None:
            raise ValueError("Ollama usage ledger requires caller-supplied pricing")
        if self.usage_ledger is not None and self.usage_ledger.budget != self.budget:
            raise ValueError("Ollama usage ledger must use the gateway budget envelope")

    @classmethod
    def from_env(cls) -> "OllamaGatewayConfig":
        host = os.environ.get("OLLAMA_HOST", cls.host)
        return cls(model=os.environ.get("FRAPPE_HARNESS_OLLAMA_MODEL", cls.model), host=host)


class OllamaGatewayError(RuntimeError):
    pass


def build_review_prompt(payload: ProposalPayload, feedback: tuple[ValidationFinding, ...] = ()) -> str:
    item_lines = "\n".join(f"- {item.key}: {item.text}" for item in payload.items)
    feedback_lines = "\n".join(f"- {item.item_key}: {item.code}: {item.message}" for item in feedback)
    return (
        "Return only one JSON object and no markdown.\n"
        "The JSON object must have exactly these top-level keys: proposal_id, findings, clarifications.\n"
        "findings must be an array of objects with keys severity, code, item_key, message.\n"
        "clarifications must be an array of objects with keys item_key, question.\n"
        f'proposal_id must be "{payload.proposal_id}".\n'
        "Use only the item keys listed below.\n"
        "For this smoke test, return one clarification for task_status and no findings.\n"
        "Previous validator feedback (repair these if present):\n"
        f"{feedback_lines or '- none'}\n"
        "Items:\n"
        f"{item_lines}"
    )


def review_with_ollama(
    payload: ProposalPayload,
    config: OllamaGatewayConfig | None = None,
    *,
    feedback: tuple[ValidationFinding, ...] = (),
) -> ProposalReview:
    config = config or OllamaGatewayConfig.from_env()
    response = _ollama_generate(config, build_review_prompt(payload, feedback))
    review = _review_from_mapping(payload, response)
    return validate_review(payload, review)


class OllamaModelGateway:
    """ModelGateway adapter whose only capability is the typed Ollama review."""

    def __init__(self, config: OllamaGatewayConfig | None = None) -> None:
        self.config = config

    def review(self, payload: ProposalPayload, *, feedback: tuple[ValidationFinding, ...] = ()) -> ProposalReview:
        return review_with_ollama(payload, self.config, feedback=feedback)


def build_proposal_prompt(prompt: str, feedback: tuple[ProposalIssue, ...] = ()) -> str:
    """Ask for only the data-only proposal shape accepted by the parser."""
    issues = "\n".join(f"- {item.path}: {item.code}: {item.message}" for item in feedback) or "- none"
    return (
        "Return only one JSON object and no markdown.\n"
        "The object must contain exactly: name, label, module, version, description, roles, entities, screens.\n"
        "Use these exact nested keys: role={name,description}; entity={name,label,fields,permissions,description,title_field,default_sort_field,default_sort_order,track_changes,search_fields,list_columns,standard_filters}; field={name,label,field_type,required,unique,default,options,description,read_only,hidden,in_list_view,in_standard_filter,position}; permission={role,read,create,write,delete}; screen={name,entity,kind,label,fields}.\n"
        "Use arrays for roles, entities, fields, permissions, screens, and Select/Link options; use booleans for required/permissions; use integer version=1. Never emit fieldname/fieldtype/reqd or nested screens inside an entity.\n"
        'Example shape (abbreviated but exact key style): {"name":"task_tracker","label":"Task Tracker","module":"task_tracker","version":1,"description":"","roles":[{"name":"Task User","description":""}],"entities":[{"name":"project","label":"Project","fields":[{"name":"title","label":"Title","field_type":"Data","required":true,"position":0}],"permissions":[{"role":"Task User","read":true}],"description":"","title_field":"title","default_sort_field":"title","default_sort_order":"asc","track_changes":true,"search_fields":["title"],"list_columns":["title"],"standard_filters":[]}],"screens":[{"name":"project_list","entity":"project","kind":"list","label":"Projects","fields":["title"]}]}.\n'
        "This is a Harness ProjectSpec proposal, not Frappe DocType JSON: never output doctype, fieldname, fieldtype, reqd, permissions as a map, or entity screens.\n"
        "Do not invent entities, fields, roles, screen targets, Link defaults, or search fields. Preserve identifiers and declared option values exactly from the requirement; a Link has exactly one string option and no default.\n"
        "Use only the Frappe Harness V1 proposal vocabulary: Data, Email, Phone, URL, Small Text, Long Text, Text, Text Editor, Integer, Float, Currency, Check, Date, Datetime, Select, Link.\n"
        "Do not include code, commands, hooks, APIs, paths, credentials, or extra properties.\n"
        f"Requirement:\n{prompt}\n"
        f"Previous validator feedback (repair only these issues):\n{issues}"
    )


class OllamaProposalExtractor:
    """Data-only natural-language extractor for ``BoundedProposalOrchestrator``."""

    def __init__(self, config: OllamaGatewayConfig | None = None) -> None:
        self.config = config

    def extract(self, prompt: str, *, feedback: tuple[ProposalIssue, ...] = ()) -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip():
            raise OllamaGatewayError("proposal prompt must be non-blank")
        return _ollama_generate(self.config or OllamaGatewayConfig.from_env(), build_proposal_prompt(prompt, feedback))


def _ollama_generate(config: OllamaGatewayConfig, prompt: str) -> dict[str, Any]:
    host = config.host if "://" in config.host else f"http://{config.host}"
    parsed = urlparse(host)
    if not parsed.scheme or not parsed.netloc:
        raise OllamaGatewayError(f"invalid Ollama host: {config.host!r}")
    url = f"{parsed.scheme}://{parsed.netloc}/api/generate"
    body = json.dumps(
        {
            "model": config.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            # A fixed seed makes bounded repair/evaluation reproducible across
            # retries on local Ollama; temperature remains zero for greedy
            # decoding, while the seed prevents tie-breaking drift.
            "options": {"temperature": 0, "seed": 0, "num_predict": config.budget.max_model_tokens},
        }
    ).encode("utf-8")
    request = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=min(config.timeout_seconds, config.budget.model_timeout_seconds)) as handle:
            raw = json.loads(handle.read().decode("utf-8"))
    except URLError as error:
        raise OllamaGatewayError(f"could not reach Ollama at {parsed.netloc}: {error}") from error
    if not isinstance(raw, dict):
        raise OllamaGatewayError("Ollama response must be a JSON object")
    if config.usage_ledger is not None:
        prompt_tokens = raw.get("prompt_eval_count")
        completion_tokens = raw.get("eval_count")
        if (
            isinstance(prompt_tokens, bool) or not isinstance(prompt_tokens, int) or prompt_tokens < 0
            or isinstance(completion_tokens, bool) or not isinstance(completion_tokens, int) or completion_tokens < 0
        ):
            raise OllamaGatewayError("Ollama response lacks valid usage counters for budget accounting")
        try:
            config.usage_ledger.record(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                prompt_rate_per_1k_usd=config.prompt_rate_per_1k_usd,
                completion_rate_per_1k_usd=config.completion_rate_per_1k_usd,
            )
        except (ModelBudgetExceeded, ValueError) as error:
            raise OllamaGatewayError("Ollama usage exceeds the configured cumulative budget") from error
    response = raw.get("response")
    if not isinstance(response, str):
        raise OllamaGatewayError("Ollama response payload must include a string response")
    try:
        parsed_response = json.loads(response)
    except json.JSONDecodeError as error:
        raise OllamaGatewayError(f"Ollama response was not valid JSON: {response!r}") from error
    if not isinstance(parsed_response, dict):
        raise OllamaGatewayError("Ollama response JSON must be an object")
    return parsed_response


def _review_from_mapping(payload: ProposalPayload, raw: dict[str, Any]) -> ProposalReview:
    proposal_id = raw.get("proposal_id")
    findings = tuple(_finding(item) for item in _sequence(raw.get("findings"), "findings"))
    clarifications = tuple(
        _clarification(item) for item in _sequence(raw.get("clarifications"), "clarifications")
    )
    return ProposalReview(proposal_id, findings=findings, clarifications=clarifications)


def _sequence(value: object, name: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise OllamaGatewayError(f"{name} must be a JSON array")
    items: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise OllamaGatewayError(f"{name}[{index}] must be a JSON object")
        items.append(item)
    return items


def _finding(raw: dict[str, Any]) -> ValidationFinding:
    severity = raw.get("severity")
    if severity not in {item.value for item in FindingSeverity}:
        raise OllamaGatewayError(f"invalid finding severity: {severity!r}")
    return ValidationFinding(
        FindingSeverity(severity),
        _text(raw, "code"),
        _text(raw, "item_key"),
        _text(raw, "message"),
    )


def _clarification(raw: dict[str, Any]) -> Clarification:
    return Clarification(_text(raw, "item_key"), _text(raw, "question"))


def _text(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise OllamaGatewayError(f"{key} must be a non-empty string")
    return value
