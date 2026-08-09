"""Gemeinsame Test-Konfiguration.

Legt das Projektwurzelverzeichnis auf den Importpfad, damit die Module des
Servers (``EnaioBackend``, ``EnaioMCP``, ``vorlage``, ``rate_limiter``) ohne
Installation importierbar sind, und stellt einen Helfer fuer ein EnaioBackend
mit gemocktem HTTP-Transport bereit.
"""

import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from EnaioBackend import DEFAULT_FULLTEXT_MAX_CHARS, EnaioBackend
from mistral_ocr import MistralOCRClient


@pytest.fixture
async def make_backend():
    """Factory fuer ein ``EnaioBackend`` gegen einen ``httpx.MockTransport``.

    ``handler`` ist die uebliche MockTransport-Funktion ``request -> Response``.
    Ueber ``ocr_client`` laesst sich die Volltextweiche auf OCR stellen.
    Alle erzeugten Clients werden nach dem Test geschlossen.
    """

    backends = []

    def _make(
        handler,
        auth_mode="session",
        ocr_client=None,
        fulltext_max_chars=DEFAULT_FULLTEXT_MAX_CHARS,
    ):
        backend = EnaioBackend(
            url="https://enaio.test",
            auth_mode=auth_mode,
            ocr_client=ocr_client,
            fulltext_max_chars=fulltext_max_chars,
        )
        backend.session = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        backends.append(backend)
        return backend

    yield _make

    for backend in backends:
        await backend.session.aclose()


@pytest.fixture
async def make_ocr_client():
    """Factory fuer einen ``MistralOCRClient`` gegen einen ``httpx.MockTransport``."""

    clients = []

    def _make(handler, api_key="TEST-KEY", **kwargs):
        client = MistralOCRClient(
            api_key,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            **kwargs,
        )
        clients.append(client)
        return client

    yield _make

    for client in clients:
        await client.aclose()
