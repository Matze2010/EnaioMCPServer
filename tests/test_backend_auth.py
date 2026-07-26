"""Tests fuer die Backend-Authentifizierung gegen die Enaio-API."""

import httpx
import pytest
from fastapi import HTTPException

from EnaioBackend import SESSION_AUTH_FAILED_MESSAGE


def _ok_search_response():
    return httpx.Response(200, json={"objects": []})


async def test_session_auth_sends_jsessionid_cookie_without_basic_auth(make_backend):
    captured = {}

    def handler(request):
        captured["cookie"] = request.headers.get("cookie")
        captured["authorization"] = request.headers.get("authorization")
        return _ok_search_response()

    backend = make_backend(handler, auth_mode="session")
    await backend.get_running_cases("gisch", session_id="SESSION-1")

    assert captured["cookie"] == "JSESSIONID=SESSION-1"
    assert captured["authorization"] is None


async def test_basic_auth_sends_authorization_without_jsessionid_cookie(make_backend):
    captured = {}

    def handler(request):
        captured["cookie"] = request.headers.get("cookie")
        captured["authorization"] = request.headers.get("authorization")
        return _ok_search_response()

    backend = make_backend(handler, auth_mode="basic")
    backend.set_auth("user", "password")
    await backend.get_running_cases("gisch", session_id="SESSION-1")

    assert captured["cookie"] is None
    assert captured["authorization"] == "Basic dXNlcjpwYXNzd29yZA=="


@pytest.mark.parametrize("status_code", [401, 403])
async def test_session_auth_failure_mentions_current_session_id(make_backend, status_code):
    backend = make_backend(lambda request: httpx.Response(status_code), auth_mode="session")

    with pytest.raises(HTTPException) as excinfo:
        await backend.get_running_cases("gisch", session_id="OLD-SESSION")

    assert excinfo.value.status_code == 401
    assert excinfo.value.detail == SESSION_AUTH_FAILED_MESSAGE


async def test_session_auth_failure_is_checked_when_status_handling_is_manual(make_backend):
    backend = make_backend(lambda request: httpx.Response(401), auth_mode="session")

    with pytest.raises(HTTPException) as excinfo:
        await backend.get_rendition("OBJ1", session_id="OLD-SESSION")

    assert excinfo.value.status_code == 401
    assert excinfo.value.detail == SESSION_AUTH_FAILED_MESSAGE
