"""Fail-closed semantic translation for the optional Commander spike.

This module is deliberately *not* a Commander CLI wrapper.  It translates a
confirmed :class:`MetadataPlan` into opaque, typed mutation intents which a
future, pinned Bench adapter may implement.  In particular, it neither emits
shell argv nor exposes Commander's field mini-language to a caller.

Commander is an experimental development-site provider.  Its documented
DocType and field-creation capability is useful for the compatibility spike,
but the project has not established a complete role/permission mapping.  A
plan containing permission rows therefore fails closed instead of silently
dropping or guessing those mutations.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Literal

from .provider_plan import MetadataPlan, ProviderField, ProviderOperation


class CommanderPlanError(ValueError):
    """Raised when a plan is not safe to translate for Commander."""


class UnsupportedCommanderOperation(CommanderPlanError):
    """Raised for a source-plan feature without an approved Commander map."""


_SUPPORTED_FIELD_TYPES = frozenset(
    {
        "Data",
        "Int",
        "Float",
        "Currency",
        "Date",
        "Datetime",
        "Check",
        "Select",
        "Link",
        "Small Text",
        "Long Text",
        "Text",
        "Text Editor",
        "URL",
    }
)


@dataclass(frozen=True)
class CommanderCreateDoctype:
    """Opaque arguments for one DocType creation intent."""

    doctype: str
    module: str
    permissions: tuple[tuple[str, bool, bool, bool, bool], ...] = ()


@dataclass(frozen=True)
class CommanderAddField:
    """Opaque arguments for one field creation intent.

    ``after`` is an identifier, not Commander syntax.  An adapter is solely
    responsible for turning it into the pinned provider's positioning API.
    """

    doctype: str
    name: str
    label: str
    field_type: str
    required: bool
    unique: bool
    default: Any | None
    select_options: tuple[str, ...]
    link_target: str | None
    after: str | None


CommanderArguments = CommanderCreateDoctype | CommanderAddField


@dataclass(frozen=True)
class CommanderAction:
    """One typed semantic action, never a command line or free-form DSL."""

    id: str
    kind: Literal["create_doctype", "add_field"]
    depends_on: tuple[str, ...]
    arguments: CommanderArguments


@dataclass(frozen=True)
class CommanderCompatibilityPlan:
    """A reviewable mutation intent for a pinned Commander compatibility spike."""

    version: int
    provider: Literal["commander-spike"]
    target_frappe_major: int
    source_spec_hash: str
    source_plan_hash: str
    actions: tuple[CommanderAction, ...]
    plan_hash: str


def build_commander_compatibility_plan(source: MetadataPlan) -> CommanderCompatibilityPlan:
    """Translate supported source operations or reject the entire plan.

    Translation is intentionally all-or-nothing.  The caller must not execute
    a partial plan after this function reports an unsupported operation.
    """

    _validate_source(source)
    actions: list[CommanderAction] = []
    for operation in source.operations:
        actions.extend(_translate_operation(operation))

    hash_input = {
        "version": 1,
        "provider": "commander-spike",
        "target_frappe_major": source.target_frappe_major,
        "source_spec_hash": source.spec_hash,
        "source_plan_hash": source.plan_hash,
        "actions": [asdict(action) for action in actions],
    }
    plan_hash = hashlib.sha256(_canonical_json(hash_input).encode("utf-8")).hexdigest()
    return CommanderCompatibilityPlan(
        version=1,
        provider="commander-spike",
        target_frappe_major=source.target_frappe_major,
        source_spec_hash=source.spec_hash,
        source_plan_hash=source.plan_hash,
        actions=tuple(actions),
        plan_hash=plan_hash,
    )


def commander_plan_json(plan: CommanderCompatibilityPlan) -> str:
    """Return canonical review data; this output is not executable text."""

    return _canonical_json(asdict(plan)) + "\n"


def _validate_source(source: MetadataPlan) -> None:
    if source.version != 1:
        raise UnsupportedCommanderOperation(f"unsupported metadata-plan version {source.version}")
    if source.target_frappe_major != 16:
        raise UnsupportedCommanderOperation(
            f"Commander spike supports Frappe 16 only, not {source.target_frappe_major}"
        )
    if not source.spec_hash or not source.plan_hash:
        raise CommanderPlanError("source plan must carry spec and plan hashes")
    seen: set[str] = set()
    for operation in source.operations:
        if operation.id in seen:
            raise CommanderPlanError(f"duplicate provider operation id {operation.id!r}")
        seen.add(operation.id)
        if operation.kind != "create_doctype":
            raise UnsupportedCommanderOperation(
                f"operation {operation.id!r} has unsupported kind {operation.kind!r}"
            )


def _translate_operation(operation: ProviderOperation) -> list[CommanderAction]:
    create_id = f"commander:{operation.id}"
    actions: list[CommanderAction] = [
        CommanderAction(
            id=create_id,
            kind="create_doctype",
            depends_on=tuple(f"commander:{dependency}" for dependency in operation.depends_on),
            arguments=CommanderCreateDoctype(
                doctype=operation.doctype,
                module=operation.module,
                permissions=tuple(
                    (p.role, p.read, p.create, p.write, p.delete) for p in operation.permissions
                ),
            ),
        )
    ]
    previous: str | None = None
    for field in operation.fields:
        _validate_field(operation.id, field)
        action_id = f"commander:add-field:{operation.entity}:{field.name}"
        actions.append(
            CommanderAction(
                id=action_id,
                kind="add_field",
                depends_on=(create_id,),
                arguments=CommanderAddField(
                    doctype=operation.doctype,
                    name=field.name,
                    label=field.label,
                    field_type=field.field_type,
                    required=field.required,
                    unique=field.unique,
                    default=field.default,
                    select_options=field.select_options,
                    link_target=field.link_target,
                    after=previous,
                ),
            )
        )
        previous = field.name
    return actions


def _validate_field(operation_id: str, field: ProviderField) -> None:
    if field.field_type not in _SUPPORTED_FIELD_TYPES:
        raise UnsupportedCommanderOperation(
            f"operation {operation_id!r}, field {field.name!r}: unsupported field type {field.field_type!r}"
        )
    if field.field_type == "Select" and not field.select_options:
        raise CommanderPlanError(f"operation {operation_id!r}, Select field {field.name!r} has no options")
    if field.field_type != "Select" and field.select_options:
        raise CommanderPlanError(
            f"operation {operation_id!r}, non-Select field {field.name!r} has select options"
        )
    if field.field_type == "Link" and not field.link_target:
        raise CommanderPlanError(f"operation {operation_id!r}, Link field {field.name!r} has no target")
    if field.field_type != "Link" and field.link_target is not None:
        raise CommanderPlanError(
            f"operation {operation_id!r}, non-Link field {field.name!r} has a Link target"
        )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
