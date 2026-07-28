"""Tests fuer das Tool ``list_users``."""

import EnaioMCP


class _Ctx:
    """Minimaler Ersatz fuer den FastMCP-Context (nur ``info`` wird genutzt)."""

    def __init__(self):
        self.messages = []

    async def info(self, message):
        self.messages.append(message)


USERS = [
    {
        "name": "GISCH",
        "fullname": "Gisch",
        "email": "gisch@datenschutz.saarland.de",
        "groups": ["REFERAT-2", "LEITUNG"],
        "guid": "GUID-1",
        "wfguid": "WFGUID-1",
    },
    {
        "name": "ZELL",
        "fullname": "Zell",
        "email": "zell@datenschutz.saarland.de",
        "groups": ["REFERAT-3"],
        "guid": "GUID-2",
        "wfguid": "WFGUID-2",
    },
]


async def test_list_users_passes_session_id_and_counts(monkeypatch):
    seen = []

    async def fake_get_users(session_id=None):
        seen.append(session_id)
        return USERS

    monkeypatch.setattr(EnaioMCP.backend, "get_users", fake_get_users)

    result = await EnaioMCP.list_users_session("SESSION-1", _Ctx())

    assert seen == ["SESSION-1"]
    assert result == {"count": 2, "users": USERS}


async def test_list_users_basic_passes_no_session_id(monkeypatch):
    seen = []

    async def fake_get_users(session_id=None):
        seen.append(session_id)
        return []

    monkeypatch.setattr(EnaioMCP.backend, "get_users", fake_get_users)

    result = await EnaioMCP.list_users_basic(_Ctx())

    assert seen == [None]
    assert result == {"count": 0, "users": []}
