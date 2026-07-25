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

from EnaioBackend import EnaioBackend


@pytest.fixture
async def make_backend():
    """Factory fuer ein ``EnaioBackend`` gegen einen ``httpx.MockTransport``.

    ``handler`` ist die uebliche MockTransport-Funktion ``request -> Response``.
    Alle erzeugten Clients werden nach dem Test geschlossen.
    """

    backends = []

    def _make(handler):
        backend = EnaioBackend(url="https://enaio.test")
        backend.session = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        backends.append(backend)
        return backend

    yield _make

    for backend in backends:
        await backend.session.aclose()
