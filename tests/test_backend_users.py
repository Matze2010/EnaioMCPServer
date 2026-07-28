"""Tests fuer ``EnaioBackend.get_users`` (Endpunkt /api/organization/users)."""

import httpx
import pytest
from fastapi import HTTPException


def _user(name, *, email="", locked="0", **extra):
    """Baut einen Eintrag in der Form, die der Enaio-Endpunkt liefert."""

    entry = {
        "id": 1,
        "name": name,
        "fullname": name.capitalize(),
        "description": "automatisch synchronisiert aus AD",
        "locked": locked,
        "limited": "0",
        "groups": ["STANDARD"],
        "email": email,
        "guid": f"GUID-{name}",
        "wfguid": f"WFGUID-{name}",
        "valid": True,
    }
    entry.update(extra)
    return entry


# Ein Auszug der echten Antwort: gueltige Nutzer, technische Konten ohne
# eMail-Adresse und gesperrte Konten mit und ohne eMail-Adresse.
SAMPLE_USERS = [
    _user("ROOT", email="", locked="0"),
    _user("ZELL", email="zell@datenschutz.saarland.de"),
    _user("REFERAT1", email="", locked="1"),
    _user("GISCH", email="gisch@datenschutz.saarland.de"),
    _user("TECH_ROOT", email="   ", locked="0"),
    _user("ORTINAU", email="ortinau@datenschutz.saarland.de", locked="1"),
    _user("ARMIN", email="armin@datenschutz.saarland.de"),
]


def _handler(entries, *, seen=None, status_code=200):
    def handle(request):
        if seen is not None:
            seen.append(request)
        return httpx.Response(status_code, json=entries)

    return handle


async def test_get_users_filters_locked_and_missing_email(make_backend):
    backend = make_backend(_handler(SAMPLE_USERS))

    users = await backend.get_users(session_id="SESSION-1")

    assert [user["name"] for user in users] == ["ARMIN", "GISCH", "ZELL"]


async def test_get_users_requests_organization_endpoint(make_backend):
    seen = []
    backend = make_backend(_handler(SAMPLE_USERS, seen=seen))

    await backend.get_users(session_id="SESSION-1")

    request = seen[0]
    assert request.method == "GET"
    assert str(request.url) == "https://enaio.test/api/organization/users"
    assert request.headers["Cookie"] == "JSESSIONID=SESSION-1"


async def test_get_users_returns_slim_record(make_backend):
    backend = make_backend(
        _handler(
            [
                _user(
                    "SCHOEMER",
                    email=" Schoemer@datenschutz.saarland.de ",
                    fullname="Schömer",
                    groups=["REFERAT-1", "POSTEINGANG"],
                )
            ]
        )
    )

    users = await backend.get_users(session_id="SESSION-1")

    assert users == [
        {
            "name": "SCHOEMER",
            "fullname": "Schömer",
            "email": "Schoemer@datenschutz.saarland.de",
            "groups": ["REFERAT-1", "POSTEINGANG"],
            "guid": "GUID-SCHOEMER",
            "wfguid": "WFGUID-SCHOEMER",
        }
    ]


async def test_get_users_tolerates_missing_fields(make_backend):
    # Im Echtbetrieb fehlt bei einzelnen Eintraegen das Feld 'wfguid'.
    entry = _user("HUWIG", email="huwig@datenschutz.saarland.de")
    del entry["wfguid"]
    del entry["groups"]
    backend = make_backend(_handler([entry]))

    users = await backend.get_users(session_id="SESSION-1")

    assert users[0]["wfguid"] is None
    assert users[0]["groups"] == []


@pytest.mark.parametrize("locked", ["1", 1, "true", "TRUE", " 1 "])
async def test_get_users_drops_locked_in_any_notation(make_backend, locked):
    backend = make_backend(
        _handler([_user("ZARTH", email="zarth@datenschutz.saarland.de", locked=locked)])
    )

    assert await backend.get_users(session_id="SESSION-1") == []


@pytest.mark.parametrize("locked", ["0", 0, "false", None])
async def test_get_users_keeps_unlocked_in_any_notation(make_backend, locked):
    backend = make_backend(
        _handler([_user("ZARTH", email="zarth@datenschutz.saarland.de", locked=locked)])
    )

    users = await backend.get_users(session_id="SESSION-1")

    assert [user["name"] for user in users] == ["ZARTH"]


async def test_get_users_rejects_non_list_response(make_backend):
    backend = make_backend(_handler({"message": "kein Array"}))

    with pytest.raises(HTTPException) as excinfo:
        await backend.get_users(session_id="SESSION-1")

    assert excinfo.value.status_code == 500


async def test_get_users_uses_basic_auth_without_session_id(make_backend):
    seen = []
    backend = make_backend(_handler(SAMPLE_USERS, seen=seen), auth_mode="basic")
    backend.set_auth("user", "secret")

    await backend.get_users()

    assert "Cookie" not in seen[0].headers
    assert seen[0].headers["Authorization"].startswith("Basic ")
