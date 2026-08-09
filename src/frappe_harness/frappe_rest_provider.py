"""Closed, typed Frappe v2 document provider for verification runners.

This is deliberately not a general HTTP client.  It has a fixed Frappe v2
surface and accepts neither arbitrary paths, HTTP verbs, nor caller-supplied
headers.  Bodies are available only to the immediate caller; durable evidence
is represented by a redacted digest receipt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class FrappeRestProviderError(ValueError):
    """A request is outside the provider's closed typed operation set."""


class RestErrorKind(str, Enum):
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    VALIDATION = "validation"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    TRANSPORT = "transport"
    UNKNOWN = "unknown"


_DOCTYPE = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{0,139}$")
_DOCUMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _./:@-]{0,139}$")
_FIELD = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_DIRECTION = frozenset({"asc", "desc"})
_MAX_PAGE_SIZE = 100


@dataclass(frozen=True)
class FrappeApiToken:
    """Typed token credentials; callers cannot provide arbitrary headers."""

    key: str = field(repr=False)
    secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key or not isinstance(self.secret, str) or not self.secret:
            raise FrappeRestProviderError("API token key and secret must be non-empty strings")


@dataclass(frozen=True)
class FrappeSessionCookie:
    """Short-lived, process-local session cookie for read-only browser parity."""

    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value.strip() or any(char in self.value for char in "\r\n"):
            raise FrappeRestProviderError("session cookie must be a non-blank single-line string")


@dataclass(frozen=True)
class EqualityFilter:
    field: str
    value: str | int | float | bool | None

    def __post_init__(self) -> None:
        _field(self.field, "filter field")
        if isinstance(self.value, (str, int, float, bool)) or self.value is None:
            return
        raise FrappeRestProviderError("filter values must be JSON scalars")


@dataclass(frozen=True)
class DocumentQuery:
    filters: tuple[EqualityFilter, ...] = ()
    fields: tuple[str, ...] = ()
    order_by: str | None = None
    direction: str = "asc"
    limit: int = 20
    start: int = 0

    def __post_init__(self) -> None:
        if len(set(filter_.field for filter_ in self.filters)) != len(self.filters):
            raise FrappeRestProviderError("document query filter fields must be unique")
        if len(set(self.fields)) != len(self.fields):
            raise FrappeRestProviderError("document query fields must be unique")
        for field in self.fields:
            _field(field, "query field")
        if self.order_by is not None:
            _field(self.order_by, "order field")
        if self.direction not in _DIRECTION:
            raise FrappeRestProviderError("order direction must be asc or desc")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or not 1 <= self.limit <= _MAX_PAGE_SIZE:
            raise FrappeRestProviderError(f"limit must be an integer from 1 to {_MAX_PAGE_SIZE}")
        if isinstance(self.start, bool) or not isinstance(self.start, int) or self.start < 0:
            raise FrappeRestProviderError("start must be a non-negative integer")


@dataclass(frozen=True)
class RestReceipt:
    operation: str
    status: int | None
    response_bytes: int
    response_sha256: str


@dataclass(frozen=True)
class FrappeRestResult:
    receipt: RestReceipt
    payload: Any | None
    error_kind: RestErrorKind | None

    @property
    def ok(self) -> bool:
        return self.error_kind is None and self.receipt.status is not None and 200 <= self.receipt.status < 300


@dataclass(frozen=True)
class _WireRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None


@dataclass(frozen=True)
class _WireResponse:
    status: int
    body: bytes


class FrappeRestTransport(Protocol):
    """Injected I/O seam.  Public provider callers never construct wire requests."""

    def send(self, request: _WireRequest, *, timeout_seconds: float) -> _WireResponse: ...


class FrappeRestTransportFailure(RuntimeError):
    """The request did not produce an HTTP response."""


class _SameOriginRedirectBlocker(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class UrlLibFrappeRestTransport:
    """Small stdlib-only transport which does not follow redirects."""

    def send(self, request: _WireRequest, *, timeout_seconds: float) -> _WireResponse:
        try:
            wire = Request(request.url, data=request.body, headers=dict(request.headers), method=request.method)
            with build_opener(_SameOriginRedirectBlocker()).open(wire, timeout=timeout_seconds) as response:
                return _WireResponse(response.status, response.read())
        except HTTPError as error:
            return _WireResponse(error.code, error.read())
        except (URLError, OSError, ValueError) as error:
            raise FrappeRestTransportFailure("Frappe REST transport failed") from error


class FrappeRestProvider:
    """Execute only typed Frappe v2 DocType metadata and document operations."""

    def __init__(
        self,
        base_url: str,
        token: FrappeApiToken | None,
        *,
        session_cookie: FrappeSessionCookie | None = None,
        transport: FrappeRestTransport | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._origin = _origin(base_url)
        self._token = token
        if token is not None and session_cookie is not None:
            raise FrappeRestProviderError("provide either an API token or a session cookie, not both")
        if session_cookie is not None and not isinstance(session_cookie, FrappeSessionCookie):
            raise FrappeRestProviderError("session_cookie must be a FrappeSessionCookie")
        self._session_cookie = session_cookie
        self._transport = transport or UrlLibFrappeRestTransport()
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise FrappeRestProviderError("timeout_seconds must be positive")
        self._timeout_seconds = float(timeout_seconds)

    def inspect_meta(self, doctype: str) -> FrappeRestResult:
        return self._send("inspect_meta", "GET", ("doctype", _doctype(doctype), "meta"))

    def list_documents(self, doctype: str, query: DocumentQuery = DocumentQuery()) -> FrappeRestResult:
        if not isinstance(query, DocumentQuery):
            raise FrappeRestProviderError("query must be a DocumentQuery")
        params: dict[str, str] = {"start": str(query.start), "limit": str(query.limit)}
        if query.fields:
            params["fields"] = _compact_json(list(query.fields))
        if query.filters:
            params["filters"] = _compact_json([[item.field, "=", item.value] for item in query.filters])
        if query.order_by:
            params["order_by"] = f"{query.order_by} {query.direction}"
        return self._send("list_documents", "GET", ("document", _doctype(doctype)), params=params)

    def get_document(self, doctype: str, name: str) -> FrappeRestResult:
        return self._send("get_document", "GET", ("document", _doctype(doctype), _document_name(name)), trailing_slash=True)

    def create_document(self, doctype: str, payload: Mapping[str, Any]) -> FrappeRestResult:
        return self._send("create_document", "POST", ("document", _doctype(doctype)), payload=_payload(payload))

    def update_document(self, doctype: str, name: str, payload: Mapping[str, Any]) -> FrappeRestResult:
        return self._send("update_document", "PATCH", ("document", _doctype(doctype), _document_name(name)), payload=_payload(payload), trailing_slash=True)

    def delete_document(self, doctype: str, name: str) -> FrappeRestResult:
        return self._send("delete_document", "DELETE", ("document", _doctype(doctype), _document_name(name)), trailing_slash=True)

    def _send(
        self,
        operation: str,
        method: str,
        segments: tuple[str, ...],
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
        trailing_slash: bool = False,
    ) -> FrappeRestResult:
        path = "/api/v2/" + "/".join(quote(segment, safe="") for segment in segments)
        if trailing_slash:
            path += "/"
        url = self._origin + path
        if params:
            url += "?" + urlencode(sorted(params.items()))
        body = _compact_json(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if self._token is not None:
            headers["Authorization"] = f"token {self._token.key}:{self._token.secret}"
        if self._session_cookie is not None:
            headers["Cookie"] = self._session_cookie.value
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = self._transport.send(_WireRequest(method, url, headers, body), timeout_seconds=self._timeout_seconds)
        except FrappeRestTransportFailure:
            return _result(operation, None, b"", RestErrorKind.TRANSPORT)
        except Exception:
            # An injected transport must not make arbitrary exception text durable.
            return _result(operation, None, b"", RestErrorKind.TRANSPORT)
        if not isinstance(response, _WireResponse) or isinstance(response.status, bool) or not isinstance(response.status, int):
            return _result(operation, None, b"", RestErrorKind.TRANSPORT)
        if not isinstance(response.body, bytes):
            return _result(operation, response.status, b"", RestErrorKind.TRANSPORT)
        if 200 <= response.status < 300:
            try:
                return _result(operation, response.status, response.body, None, json.loads(response.body.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return _result(operation, response.status, response.body, RestErrorKind.UNKNOWN)
        return _result(
            operation,
            response.status,
            response.body,
            _error_kind(
                response.status,
                response.body,
                unauthenticated=self._token is None and self._session_cookie is None,
            ),
        )


def _result(operation: str, status: int | None, body: bytes, error_kind: RestErrorKind | None, payload: Any | None = None) -> FrappeRestResult:
    return FrappeRestResult(RestReceipt(operation, status, len(body), sha256(body).hexdigest()), payload, error_kind)


def _error_kind(status: int, body: bytes, *, unauthenticated: bool = False) -> RestErrorKind:
    if status == 401:
        return RestErrorKind.AUTHENTICATION
    if status == 403:
        # Frappe v2 may use 403 rather than 401 for an anonymous document
        # request.  An explicitly credential-free provider is therefore an
        # authentication probe; a token-authenticated 403 remains a true
        # permission denial.
        return RestErrorKind.AUTHENTICATION if unauthenticated else RestErrorKind.PERMISSION
    if status == 404:
        return RestErrorKind.NOT_FOUND
    if status == 409:
        return RestErrorKind.CONFLICT
    # Frappe's standard MandatoryError response uses 417 Expectation Failed.
    # Treat it as a validation result alongside conventional API statuses.
    if status in {400, 417, 422}:
        return RestErrorKind.VALIDATION
    # Frappe v2 error types can distinguish validation errors carried under a
    # non-standard 4xx status without persisting the server message.
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return RestErrorKind.UNKNOWN
    types = [str(item.get("type", "")).lower() for item in value.get("errors", []) if isinstance(item, Mapping)] if isinstance(value, Mapping) else []
    return RestErrorKind.VALIDATION if any("validation" in item for item in types) else RestErrorKind.UNKNOWN


def _origin(base_url: object) -> str:
    if not isinstance(base_url, str):
        raise FrappeRestProviderError("base_url must be an explicit same-origin HTTP URL")
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment or parts.path not in {"", "/"}:
        raise FrappeRestProviderError("base_url must be an origin only, without credentials, path, query, or fragment")
    return f"{parts.scheme}://{parts.netloc}"


def _doctype(value: object) -> str:
    if not isinstance(value, str) or not _DOCTYPE.fullmatch(value):
        raise FrappeRestProviderError("doctype must be a simple DocType name")
    return value


def _document_name(value: object) -> str:
    if not isinstance(value, str) or not _DOCUMENT_NAME.fullmatch(value) or value.startswith(("-", ".")) or ".." in value:
        raise FrappeRestProviderError("document name contains unsafe characters")
    return value


def _field(value: object, label: str) -> str:
    if not isinstance(value, str) or not _FIELD.fullmatch(value):
        raise FrappeRestProviderError(f"{label} must be a safe field identifier")
    return value


def _payload(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise FrappeRestProviderError("document payload must be a non-empty object")
    payload = dict(value)
    for key in payload:
        _field(key, "payload field")
    try:
        _compact_json(payload)
    except (TypeError, ValueError) as error:
        raise FrappeRestProviderError("document payload must be JSON serializable") from error
    return payload


def _compact_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
