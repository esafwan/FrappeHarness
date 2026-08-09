from __future__ import annotations

from hashlib import sha256
import json

import pytest

from frappe_harness.frappe_rest_provider import (
    DocumentQuery,
    EqualityFilter,
    FrappeApiToken,
    FrappeRestProvider,
    FrappeRestProviderError,
    FrappeSessionCookie,
    RestErrorKind,
    _WireResponse,
)


class ScriptedTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def send(self, request, *, timeout_seconds):
        self.requests.append((request, timeout_seconds))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _provider(transport):
    return FrappeRestProvider("https://example.invalid", FrappeApiToken("key", "secret"), transport=transport)


def test_provider_has_only_fixed_crud_list_and_meta_wire_shapes():
    transport = ScriptedTransport(*[_WireResponse(200, b"{}") for _ in range(6)])
    provider = _provider(transport)

    provider.inspect_meta("Task")
    provider.list_documents("Task", DocumentQuery((EqualityFilter("status", "Open"),), ("name", "status"), "name", "asc", 2, 1))
    provider.get_document("Task", "abc123")
    provider.create_document("Task", {"title": "A"})
    provider.update_document("Task", "abc123", {"title": "B"})
    provider.delete_document("Task", "abc123")

    requests = [item[0] for item in transport.requests]
    assert [(request.method, request.url) for request in requests] == [
        ("GET", "https://example.invalid/api/v2/doctype/Task/meta"),
        ("GET", "https://example.invalid/api/v2/document/Task?fields=%5B%22name%22%2C%22status%22%5D&filters=%5B%5B%22status%22%2C%22%3D%22%2C%22Open%22%5D%5D&limit=2&order_by=name+asc&start=1"),
        ("GET", "https://example.invalid/api/v2/document/Task/abc123/"),
        ("POST", "https://example.invalid/api/v2/document/Task"),
        ("PATCH", "https://example.invalid/api/v2/document/Task/abc123/"),
        ("DELETE", "https://example.invalid/api/v2/document/Task/abc123/"),
    ]
    assert requests[3].body == b'{"title":"A"}'
    assert requests[4].body == b'{"title":"B"}'
    assert set(requests[0].headers) == {"Accept", "Authorization"}
    assert "secret" not in repr(provider)
    assert "secret" not in repr(FrappeApiToken("key", "secret"))


def test_result_preserves_status_and_redacts_body_to_digest_receipt():
    body = b'{"data":{"name":"abc123"}}'
    result = _provider(ScriptedTransport(_WireResponse(201, body))).create_document("Task", {"title": "A"})

    assert result.ok
    assert result.payload == {"data": {"name": "abc123"}}
    assert result.receipt.status == 201
    assert result.receipt.response_bytes == len(body)
    assert result.receipt.response_sha256 == sha256(body).hexdigest()
    assert "abc123" not in repr(result.receipt)


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, b'{"errors":[{"type":"AuthenticationError"}]}', RestErrorKind.AUTHENTICATION),
        (403, b'{"errors":[{"type":"PermissionError"}]}', RestErrorKind.PERMISSION),
        (417, b'{"errors":[{"type":"MandatoryError"}]}', RestErrorKind.VALIDATION),
        (422, b'{"errors":[{"type":"ValidationError"}]}', RestErrorKind.VALIDATION),
        (404, b"{}", RestErrorKind.NOT_FOUND),
        (409, b"{}", RestErrorKind.CONFLICT),
        (500, b'{"errors":[{"type":"ValidationError"}]}', RestErrorKind.VALIDATION),
    ],
)
def test_provider_normalizes_status_and_frappe_error_type_without_retaining_error_body(status, body, expected):
    result = _provider(ScriptedTransport(_WireResponse(status, body))).get_document("Task", "abc123")

    assert result.error_kind is expected
    assert result.payload is None
    assert result.receipt.status == status
    assert result.receipt.response_sha256 == sha256(body).hexdigest()


def test_transport_exception_becomes_redacted_transport_result():
    result = _provider(ScriptedTransport(RuntimeError("secret server detail"))).get_document("Task", "abc123")

    assert result.error_kind is RestErrorKind.TRANSPORT
    assert result.receipt.status is None
    assert result.receipt.response_sha256 == sha256(b"").hexdigest()


def test_unauthenticated_frappe_403_is_an_authentication_result_but_authenticated_403_is_permission_denied():
    anonymous = FrappeRestProvider("https://example.invalid", None, transport=ScriptedTransport(_WireResponse(403, b"{}")))
    authenticated = _provider(ScriptedTransport(_WireResponse(403, b"{}")))

    anonymous_result = anonymous.list_documents("Task")
    authenticated_result = authenticated.list_documents("Task")
    session_authenticated = FrappeRestProvider(
        "https://example.invalid", None,
        session_cookie=FrappeSessionCookie("sid=opaque"),
        transport=ScriptedTransport(_WireResponse(403, b"{}")),
    ).list_documents("Task")

    assert anonymous_result.error_kind is RestErrorKind.AUTHENTICATION
    assert authenticated_result.error_kind is RestErrorKind.PERMISSION
    assert session_authenticated.error_kind is RestErrorKind.PERMISSION


@pytest.mark.parametrize(
    "action",
    [
        lambda provider: provider.get_document("Task", "../escape"),
        lambda provider: provider.create_document("Task", {"../escape": "x"}),
        lambda provider: provider.list_documents("Task", DocumentQuery(order_by="name;drop")),
    ],
)
def test_provider_rejects_unsafe_or_raw_request_parts(action):
    with pytest.raises(FrappeRestProviderError):
        action(_provider(ScriptedTransport()))


@pytest.mark.parametrize("base_url", ["https://example.invalid/a", "https://key:secret@example.invalid", "ftp://example.invalid", "https://example.invalid/?q=1"])
def test_provider_requires_an_explicit_origin_only(base_url):
    with pytest.raises(FrappeRestProviderError):
        FrappeRestProvider(base_url, FrappeApiToken("key", "secret"), transport=ScriptedTransport())


def test_payload_is_json_canonical_before_entering_transport():
    transport = ScriptedTransport(_WireResponse(200, json.dumps({"ok": True}).encode()))
    _provider(transport).create_document("Task", {"status": "Open", "title": "A"})

    assert transport.requests[0][0].body == b'{"status":"Open","title":"A"}'


def test_session_cookie_is_a_typed_alternative_to_api_token():
    transport = ScriptedTransport(_WireResponse(200, b'{"data":{}}'))
    provider = FrappeRestProvider(
        "https://example.invalid", None,
        session_cookie=FrappeSessionCookie("sid=opaque"), transport=transport,
    )
    assert provider.inspect_meta("Task").ok
    assert transport.requests[0][0].headers["Cookie"] == "sid=opaque"
    with pytest.raises(FrappeRestProviderError, match="either"):
        FrappeRestProvider(
            "https://example.invalid", FrappeApiToken("key", "secret"),
            session_cookie=FrappeSessionCookie("sid=opaque"),
        )
