"""Client fuer die Mistral-OCR-API (``POST /v1/ocr``).

Wird der Volltext eines Dokuments nicht aus der Text-Rendition von Enaio,
sondern per OCR aus der Originaldatei gewonnen (Weiche ``FULLTEXT_SOURCE``),
laedt :class:`MistralOCRClient` die Datei als Base64-``data:``-URI zu Mistral
hoch und setzt den Volltext aus dem Markdown der einzelnen Seiten zusammen.

Bewusst ein eigenes Modul und ein eigener ``httpx.AsyncClient``, nicht die
Session des :class:`EnaioBackend`: jene laeuft ohne Zertifikatspruefung (interne
CA) und haengt an jeden Request die Enaio-Anmeldung (``JSESSIONID`` bzw. Basic
Auth). Beides waere gegenueber einer oeffentlichen API falsch.

Wie ``rate_limiter`` haelt sich das Modul frei von HTTP-/MCP-/FastAPI-
Abhaengigkeiten nach aussen: Jeder Fehlschlag wird auf :class:`OCRUnavailable`
normiert. Nur so kann die Aufrufstelle stillschweigend auf die Enaio-Rendition
zurueckfallen, statt den Toolaufruf scheitern zu lassen.
"""

import base64
import logging

import httpx

# Basis-URL und Pfad der OCR-API. Getrennt gehalten, damit die Basis-URL fuer
# ein Gateway/einen Proxy konfigurierbar bleibt (analog EnaioBackend.backend_url).
DEFAULT_BASE_URL = "https://api.mistral.ai"
OCR_PATH = "/v1/ocr"

# Modell-ID der OCR. "latest" zeigt jeweils auf den aktuellen Snapshot; ueber
# MISTRAL_OCR_MODEL laesst sich ein datierter Stand (z. B. mistral-ocr-2503)
# festnageln.
DEFAULT_MODEL = "mistral-ocr-latest"

# Zeitlimit eines OCR-Aufrufs. Grosszuegig, weil ein mehrseitiges Scan-PDF
# durchaus eine Minute braucht.
DEFAULT_TIMEOUT = 120.0

# Von Mistral dokumentierte Obergrenze der Dateigroesse (50 MB). Groessere
# Dateien werden gar nicht erst uebertragen.
DEFAULT_MAX_BYTES = 50 * 1024 * 1024

# MIME-Types, die an die OCR gehen. Bewusst nur PDF und Bilder: Fuer .docx,
# .msg und Aehnliches liefert Enaio selbst eine textnahe Rendition, die einer
# OCR ueberlegen ist.
DEFAULT_MIME_TYPES = (
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/tiff",
    "image/webp",
    "image/avif",
)

# Bilder werden als "image_url"-Chunk uebergeben, alles andere als
# "document_url"-Chunk. Der Schluesselname im Body wechselt mit dem Typ.
IMAGE_TYPE_PREFIX = "image/"

# Trenner zwischen den Seiten im zusammengesetzten Volltext.
PAGE_SEPARATOR = "\n\n"


class OCRUnavailable(Exception):
    """OCR war nicht moeglich.

    Anders als die Enaio-Zugriffe wird daraus keine ``HTTPException``: Der
    Aufrufer faengt die Exception und faellt auf die Text-Rendition von Enaio
    zurueck, damit ein Volltextabruf nie an der OCR scheitert.
    """


class MistralOCRClient:
    """Duenner Client fuer ``POST {base_url}/v1/ocr``.

    :param api_key: API-Key fuer die Bearer-Authentifizierung.
    :param model: Modell-ID der OCR.
    :param base_url: Basis-URL der API ohne Pfad.
    :param timeout: Zeitlimit eines Aufrufs in Sekunden.
    :param max_bytes: Dateien darueber werden ohne Aufruf abgelehnt.
    :param mime_types: MIME-Types, die per OCR gelesen werden.
    :param client: Vorgefertigter ``httpx.AsyncClient``; nur fuer Tests
        gedacht (``httpx.MockTransport``). Ohne Angabe wird ein eigener Client
        **mit** Zertifikatspruefung erzeugt.
    """

    def __init__(
        self,
        api_key,
        *,
        model=DEFAULT_MODEL,
        base_url=DEFAULT_BASE_URL,
        timeout=DEFAULT_TIMEOUT,
        max_bytes=DEFAULT_MAX_BYTES,
        mime_types=DEFAULT_MIME_TYPES,
        client=None,
    ):
        self.api_key = (api_key or "").strip()
        self.model = model
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.max_bytes = max_bytes
        self.mime_types = {mime_type.lower() for mime_type in mime_types}

        self.client = client or httpx.AsyncClient(timeout=timeout)

        self.logger = logging.getLogger(__name__)

    def supports(self, mime_type) -> bool:
        """Sagt, ob ein MIME-Type per OCR gelesen wird."""

        return (mime_type or "").lower() in self.mime_types

    async def extract_text(self, content: bytes, mime_type: str, *, context: str) -> str:
        """Liest den Text einer Datei per OCR.

        :param content: Rohinhalt der Datei.
        :param mime_type: MIME-Type der Datei, bestimmt die Art des Chunks.
        :param context: Kurzbeschreibung fuer Logmeldungen, z. B.
            ``"Dokument 2024-42"``.
        :returns: Den Volltext als Markdown, Seiten durch Leerzeile getrennt.
        :raises OCRUnavailable: Bei jedem Fehlschlag - fehlende Konfiguration,
            ungeeignete Datei, Netz-/API-Fehler oder leeres Ergebnis.
        """

        # Alle Vorbedingungen zuerst: Ein ungeeigneter Aufruf soll keinen
        # Netzverkehr und keine Kosten verursachen.
        if not self.api_key:
            raise OCRUnavailable("Kein MISTRAL_API_KEY konfiguriert")
        if not content:
            raise OCRUnavailable("Die Datei ist leer")
        if not self.supports(mime_type):
            raise OCRUnavailable(f"MIME-Type {mime_type} ist nicht fuer die OCR vorgesehen")
        if len(content) > self.max_bytes:
            raise OCRUnavailable(
                f"Die Datei ist mit {len(content)} Bytes groesser als das Limit "
                f"von {self.max_bytes} Bytes"
            )

        self.logger.info(
            "OCR fuer %s (%s, %d Bytes) mit Modell %s",
            context,
            mime_type,
            len(content),
            self.model,
        )

        payload = {
            "model": self.model,
            "document": self._build_document(content, mime_type),
            # Es wird nur Text gebraucht; mit True kaemen die Seitenbilder als
            # Base64 mit zurueck und wuerden die Antwort vervielfachen.
            "include_image_base64": False,
        }

        try:
            response = await self.client.post(
                self.base_url + OCR_PATH,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as e:
            raise OCRUnavailable(
                f"Die Mistral-OCR antwortete mit HTTP {e.response.status_code}"
            ) from e
        except httpx.RequestError as e:
            raise OCRUnavailable(f"Die Mistral-OCR ist nicht erreichbar: {e}") from e
        except ValueError as e:
            raise OCRUnavailable(f"Unlesbare Antwort der Mistral-OCR: {e}") from e

        text = self._pages_markdown(data)

        self.logger.info("OCR fuer %s lieferte %d Zeichen", context, len(text))

        return text

    def _build_document(self, content: bytes, mime_type: str) -> dict:
        """Baut den ``document``-Teil des Requests als Base64-``data:``-URI.

        Die API kennt zwei Chunk-Formen, bei denen mit dem ``type`` auch der
        Name des Wertfeldes wechselt: ``image_url`` fuer Bilder,
        ``document_url`` fuer alles andere.
        """

        data_uri = f"data:{mime_type};base64,{base64.b64encode(content).decode('ascii')}"

        if mime_type.lower().startswith(IMAGE_TYPE_PREFIX):
            return {"type": "image_url", "image_url": data_uri}

        return {"type": "document_url", "document_url": data_uri}

    @staticmethod
    def _pages_markdown(data) -> str:
        """Setzt den Volltext aus ``pages[].markdown`` zusammen.

        Ein leeres Ergebnis wird zum Fehler: Als Erfolg zurueckgegebener
        Leertext wuerde den Rueckfall auf die Enaio-Rendition stillschweigend
        verhindern.
        """

        pages = (data or {}).get("pages") or []

        markdown = [
            page.get("markdown", "").strip()
            for page in pages
            if isinstance(page, dict) and page.get("markdown", "").strip()
        ]

        if not markdown:
            raise OCRUnavailable("Die Mistral-OCR lieferte keinen Text")

        return PAGE_SEPARATOR.join(markdown)

    async def aclose(self) -> None:
        """Schliesst den HTTP-Client."""

        await self.client.aclose()
