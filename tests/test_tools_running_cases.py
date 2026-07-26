"""Tests fuer das Tool ``list_running_cases``."""

import re

import EnaioMCP


class _Ctx:
    """Minimaler Ersatz fuer den FastMCP-Context (nur ``info`` wird genutzt)."""

    def __init__(self):
        self.messages = []

    async def info(self, message):
        self.messages.append(message)


async def test_list_running_cases_adds_dms_link(monkeypatch):
    cases = [
        {"reference_nr": "DS.1.2-2024-1234", "object_id": "132887"},
        {"reference_nr": "DS.1.2-2024-5678", "object_id": "132888"},
    ]

    async def fake_get_running_cases(user):
        assert user == "gisch"
        return cases

    monkeypatch.setattr(EnaioMCP, "DMS_WEB_URL", "https://enaio.test")
    monkeypatch.setattr(EnaioMCP.backend, "get_running_cases", fake_get_running_cases)

    result = await EnaioMCP.list_running_cases("gisch", "SESSION-1", _Ctx())

    assert result["count"] == 2
    for case, object_id in zip(result["cases"], ["132887", "132888"]):
        assert re.fullmatch(
            rf"https://enaio\.test/osweb/#/folder/{object_id}/0"
            rf"\?state=\d+&currentId={object_id}&currentTypeId=0",
            case["dms_link"],
        )


async def test_list_running_cases_without_link_when_unconfigured(monkeypatch):
    async def fake_get_running_cases(user):
        return [{"reference_nr": "DS.1.2-2024-1234", "object_id": "132887"}]

    # Ohne konfigurierte Basis-URL bleibt das Feld weg statt kaputt zu sein.
    monkeypatch.setattr(EnaioMCP, "DMS_WEB_URL", "DEFAULT_URL")
    monkeypatch.setattr(EnaioMCP.backend, "get_running_cases", fake_get_running_cases)

    result = await EnaioMCP.list_running_cases("gisch", "SESSION-1", _Ctx())

    assert "dms_link" not in result["cases"][0]
