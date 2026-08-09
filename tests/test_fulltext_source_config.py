"""Tests fuer die Auswertung von FULLTEXT_SOURCE beim Start des Servers.

EnaioMCP liest seine Konfiguration beim Import, deshalb wird das Modul hier je
Test mit gesetzter Umgebung neu geladen.
"""

import importlib
import logging

import pytest

import EnaioMCP
from EnaioBackend import DEFAULT_FULLTEXT_MAX_CHARS
from mistral_ocr import DEFAULT_BASE_URL, DEFAULT_MODEL, MistralOCRClient

OCR_ENV = (
    "FULLTEXT_SOURCE",
    "FULLTEXT_MAX_CHARS",
    "MISTRAL_API_KEY",
    "MISTRAL_API_URL",
    "MISTRAL_OCR_MAX_BYTES",
    "MISTRAL_OCR_MIME_TYPES",
    "MISTRAL_OCR_MODEL",
    "MISTRAL_OCR_TIMEOUT",
)


@pytest.fixture(autouse=True)
def restore_enaio_mcp(monkeypatch):
    """Laedt EnaioMCP nach jedem Test mit sauberer Umgebung neu.

    Ohne das behielte das Modul das zuletzt gesetzte FULLTEXT_SOURCE und andere
    Testdateien saehen ein fremdes ``EnaioMCP.backend``.
    """

    yield

    for name in OCR_ENV:
        monkeypatch.delenv(name, raising=False)
    importlib.reload(EnaioMCP)


@pytest.fixture
def load_enaio_mcp(monkeypatch):
    """Laedt EnaioMCP mit den uebergebenen Umgebungsvariablen neu."""

    def _load(**env):
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        return importlib.reload(EnaioMCP)

    return _load


def test_default_leaves_fulltext_on_enaio(load_enaio_mcp):
    module = load_enaio_mcp()

    assert module.FULLTEXT_SOURCE == "enaio"
    assert module.backend.ocr_client is None
    assert module.backend.fulltext_max_chars == DEFAULT_FULLTEXT_MAX_CHARS


def test_invalid_value_prevents_start(load_enaio_mcp):
    with pytest.raises(RuntimeError) as error:
        load_enaio_mcp(FULLTEXT_SOURCE="quatsch")

    assert "mistral-ocr" in str(error.value)


def test_value_is_normalised(load_enaio_mcp):
    module = load_enaio_mcp(FULLTEXT_SOURCE="  Mistral-OCR  ", MISTRAL_API_KEY="KEY")

    assert module.FULLTEXT_SOURCE == "mistral-ocr"
    assert isinstance(module.backend.ocr_client, MistralOCRClient)


def test_mistral_without_api_key_warns_and_keeps_enaio(load_enaio_mcp, caplog):
    with caplog.at_level(logging.WARNING, logger="EnaioMCP"):
        module = load_enaio_mcp(FULLTEXT_SOURCE="mistral-ocr")

    assert module.backend.ocr_client is None
    assert "MISTRAL_API_KEY" in caplog.text


def test_mistral_with_api_key_builds_client(load_enaio_mcp):
    module = load_enaio_mcp(FULLTEXT_SOURCE="mistral-ocr", MISTRAL_API_KEY="KEY")

    client = module.backend.ocr_client
    assert isinstance(client, MistralOCRClient)
    assert client.api_key == "KEY"
    assert client.model == DEFAULT_MODEL
    assert client.base_url == DEFAULT_BASE_URL


def test_client_settings_come_from_environment(load_enaio_mcp):
    module = load_enaio_mcp(
        FULLTEXT_SOURCE="mistral-ocr",
        MISTRAL_API_KEY="KEY",
        MISTRAL_OCR_MODEL="mistral-ocr-2503",
        MISTRAL_API_URL="https://gateway.test/",
        MISTRAL_OCR_MAX_BYTES="1024",
        MISTRAL_OCR_MIME_TYPES=" application/pdf , IMAGE/PNG ",
    )

    client = module.backend.ocr_client
    assert client.model == "mistral-ocr-2503"
    # Der abschliessende Schraegstrich darf den Pfad nicht verdoppeln.
    assert client.base_url == "https://gateway.test"
    assert client.max_bytes == 1024
    assert client.mime_types == {"application/pdf", "image/png"}


def test_empty_mime_type_list_falls_back_to_defaults(load_enaio_mcp):
    module = load_enaio_mcp(
        FULLTEXT_SOURCE="mistral-ocr", MISTRAL_API_KEY="KEY", MISTRAL_OCR_MIME_TYPES=" , "
    )

    assert "application/pdf" in module.backend.ocr_client.mime_types


def test_fulltext_max_chars_reaches_the_backend(load_enaio_mcp):
    module = load_enaio_mcp(FULLTEXT_MAX_CHARS="1234")

    assert module.backend.fulltext_max_chars == 1234
