"""Tests fuer das Tool ``create_case_document``."""

import pytest

import EnaioMCP
from rate_limiter import RateLimiter


class _Ctx:
    """Minimaler Ersatz fuer den FastMCP-Context (nur ``info`` wird genutzt)."""

    def __init__(self):
        self.messages = []

    async def info(self, message):
        self.messages.append(message)


@pytest.fixture
def stubbed_document(monkeypatch, tmp_path):
    """Haengt Vorlage, Rendering und Upload so ab, dass kein Enaio noetig ist."""

    monkeypatch.setattr(EnaioMCP, "ASSETS_DIR", tmp_path)
    monkeypatch.setattr(EnaioMCP, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(EnaioMCP, "OFFICE_WEB_URL", "https://enaio.test")
    monkeypatch.setattr(EnaioMCP, "upload_limiter", RateLimiter(10))
    (tmp_path / "Vorlage_Vermerk.docx").write_bytes(b"PK")

    def fake_render(template_path, content, out_path, betreff, fields):
        # Datei wirklich anlegen, damit _discard_temp_file sie loeschen kann.
        out_path.write_bytes(b"PK")
        return out_path

    monkeypatch.setattr(EnaioMCP, "_render_document", fake_render)

    def set_object_id(object_id):
        async def fake_upload(reference, file_path, document_type, betreff, filename):
            return {"objectId": object_id, "reference_nr": reference}

        monkeypatch.setattr(EnaioMCP.backend, "upload_document", fake_upload)

    return set_object_id


async def test_create_case_document_returns_edit_link(stubbed_document):
    stubbed_document("305821")

    result = await EnaioMCP.create_case_document(
        "DS.1.2-2024-1234",
        "Vermerk",
        [{"type": "para", "text": "Inhalt"}],
        "SESSION-1",
        _Ctx(),
    )

    assert result["enaio_object_id"] == "305821"
    assert (
        result["edit_link"]
        == "https://enaio.test/office/desktop/edit/edit/262146/305821"
    )


async def test_create_case_document_without_link_when_object_id_missing(stubbed_document):
    # Ohne ObjectID aus Enaio bleibt das Feld weg statt kaputt zu sein.
    stubbed_document(None)

    result = await EnaioMCP.create_case_document(
        "DS.1.2-2024-1234",
        "Vermerk",
        [{"type": "para", "text": "Inhalt"}],
        "SESSION-1",
        _Ctx(),
    )

    assert result["enaio_object_id"] is None
    assert "edit_link" not in result


async def test_guardrail_reaches_the_client():
    # FastMCP schneidet den Docstring am :param-Block ab. Stand die Aufrufregel dahinter,
    # kam sie beim Modell nie an - deshalb hier gegen die tatsaechlich ausgelieferte
    # Tool-Beschreibung pruefen, nicht gegen __doc__.
    tool = await EnaioMCP.mcp.get_tool("create_case_document")
    description = tool.description

    for marker in (
        "KERNREGEL",
        "ZULÄSSIGKEITSPRÜFUNG",
        "WAS ALS AUSDRÜCKLICHE SPEICHERANWEISUNG GILT",
        "WAS AUSDRÜCKLICH KEINE SPEICHERANWEISUNG IST",
        "VERBOT IMPLIZITER ANNAHMEN",
        "FAIL-CLOSED-REGEL",
        "MERKSATZ",
    ):
        assert marker in description

    # Der Merksatz muss der Schluss der Beschreibung bleiben (Recency), der :param-Block
    # wird von FastMCP entfernt.
    assert description.rstrip().endswith("in Enaio zu speichern.")
    assert ":param" not in description


async def test_create_case_document_is_marked_destructive():
    # Clients, die auf destructiveHint reagieren, sollen vor dem Speichern nachfragen.
    tool = await EnaioMCP.mcp.get_tool("create_case_document")

    assert tool.annotations.destructiveHint is True
    assert tool.annotations.readOnlyHint is False


async def test_server_instructions_mention_the_save_rule():
    instructions = EnaioMCP.mcp.instructions

    assert "create_case_document" in instructions
    assert "Speicheranweisung" in instructions
    assert "SessionID" in instructions
    assert "Resources" in instructions
