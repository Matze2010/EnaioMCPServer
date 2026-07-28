"""Tests fuer das Tool ``list_inbox``."""

import EnaioMCP


class _Ctx:
    """Minimaler Ersatz fuer den FastMCP-Context (nur ``info`` wird genutzt)."""

    def __init__(self):
        self.messages = []

    async def info(self, message):
        self.messages.append(message)


INBOX = [
    {
        "name": "Posteingang 24298",
        "activity": "Bearbeiten",
        "creationDate": "2026-07-13T09:39:35",
        "process_id": "88F418F4AAA44309B7901EC0DA2D3E32",
        "activity_id": "4C2085F760D54A4FBF0992D7B4777849",
        "object_id": 303786,
    },
    {
        "name": "Posteingang 24299",
        "activity": "Bearbeiten",
        "creationDate": "2026-07-13T09:37:40",
        "process_id": "54D80CA341864200AB1689A681C7AB1E",
        "activity_id": "4C2085F760D54A4FBF0992D7B4777849",
        "object_id": 303787,
    },
]


async def test_list_inbox_passes_session_id_and_counts(monkeypatch):
    seen = []

    async def fake_get_inbox(session_id=None):
        seen.append(session_id)
        return INBOX

    monkeypatch.setattr(EnaioMCP.backend, "get_inbox", fake_get_inbox)

    result = await EnaioMCP.list_inbox_session("SESSION-1", _Ctx())

    assert seen == ["SESSION-1"]
    assert result == {"count": 2, "inbox": INBOX}


async def test_list_inbox_basic_passes_no_session_id(monkeypatch):
    seen = []

    async def fake_get_inbox(session_id=None):
        seen.append(session_id)
        return []

    monkeypatch.setattr(EnaioMCP.backend, "get_inbox", fake_get_inbox)

    result = await EnaioMCP.list_inbox_basic(_Ctx())

    assert seen == [None]
    assert result == {"count": 0, "inbox": []}


async def test_list_inbox_reports_progress(monkeypatch):
    async def fake_get_inbox(session_id=None):
        return []

    monkeypatch.setattr(EnaioMCP.backend, "get_inbox", fake_get_inbox)

    ctx = _Ctx()
    await EnaioMCP.list_inbox_session("SESSION-1", ctx)

    assert ctx.messages == ["Lade Posteingang aus ENAIO"]
