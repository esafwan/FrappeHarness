"""A deliberately tiny, read-only command policy for ``frappectl``.

This module only constructs argv tokens.  It never executes a process and has
no escape hatch for raw commands, paths, filters, queries, or mutation flags.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal


class FrappectlPolicyError(ValueError):
    """A request is outside the explicit read-only command allowlist."""


_DOCTYPE = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{0,139}$")
_DOCUMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _./:@-]{0,139}$")
_PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_LIMIT = 100


@dataclass(frozen=True)
class DocListRequest:
    doctype: str
    limit: int = 20


@dataclass(frozen=True)
class DocGetRequest:
    doctype: str
    name: str


@dataclass(frozen=True)
class DoctypeShowRequest:
    name: str
    raw: bool = False


ReadOnlyRequest = DocListRequest | DocGetRequest | DoctypeShowRequest


def build_readonly_argv(request: ReadOnlyRequest, *, profile: str | None = None) -> tuple[str, ...]:
    """Build one exact, JSON-only frappectl read command.

    Accepted forms are intentionally limited to ``doctype show <name>``,
    ``doc list <doctype> --limit <1..100>``, and ``doc get <doctype> <name>``.
    The resulting argv must be passed directly to a subprocess API with
    ``shell=False`` by a separate, audited execution adapter.
    """

    prefix = ("frappectl", "--json") + _profile_args(profile)
    if isinstance(request, DoctypeShowRequest):
        if not isinstance(request.raw, bool):
            raise FrappectlPolicyError("raw must be a boolean")
        return prefix + ("doctype", "show", _doctype(request.name, "name")) + (("--raw",) if request.raw else ())
    if isinstance(request, DocListRequest):
        doctype = _doctype(request.doctype, "doctype")
        if isinstance(request.limit, bool) or not isinstance(request.limit, int) or not 1 <= request.limit <= _LIMIT:
            raise FrappectlPolicyError(f"limit must be an integer from 1 to {_LIMIT}")
        return prefix + ("doc", "list", doctype, "--limit", str(request.limit))
    if isinstance(request, DocGetRequest):
        return prefix + (
            "doc", "get", _doctype(request.doctype, "doctype"),
            _document_name(request.name),
        )
    raise FrappectlPolicyError("only typed read-only frappectl requests are allowed")


def _doctype(value: object, label: Literal["doctype", "name"]) -> str:
    if not isinstance(value, str) or not _DOCTYPE.fullmatch(value):
        raise FrappectlPolicyError(f"{label} must be a simple DocType name")
    return value


def _document_name(value: object) -> str:
    # Slashes are allowed in legacy Frappe document names, but leading dots,
    # backslashes, shell control characters, and argv switches are not.
    if not isinstance(value, str) or not _DOCUMENT_NAME.fullmatch(value) or value.startswith(("-", ".")) or ".." in value:
        raise FrappectlPolicyError("document name contains unsafe characters")
    return value


def _profile_args(profile: str | None) -> tuple[str, ...]:
    """Bind requests to a configured local profile, never a URL or credential."""

    if profile is None:
        return ()
    if not isinstance(profile, str) or not _PROFILE.fullmatch(profile):
        raise FrappectlPolicyError("profile must be a configured simple profile name")
    return ("--site", profile)
