import os
import re
import base64

from datetime import datetime
from pathlib import Path

from fastmcp import FastMCP, Context
from fastmcp.tools.tool import ToolAnnotations
from typing import Annotated, List, Optional
from fastapi import HTTPException

import vorlage
from EnaioBackend import EnaioBackend, RUNNING_CASE_STATUS
from rate_limiter import RateLimiter, RateLimitExceeded
from logging_config import configure_logging

# Logging prozessweit konfigurieren (Level ueber LOG_LEVEL, Default INFO).
# Auf Modulebene, damit sowohl "fastmcp run ..." als auch "python EnaioMCP.py"
# die Konfiguration erhalten.
configure_logging()

url = os.environ.get('URL', 'DEFAULT_URL')
username = os.environ.get('USERNAME', 'DEFAULT_USERNAME')
password = os.environ.get('PASSWORD', 'DEFAULT_PASSWORD')

backend = EnaioBackend(url=url)
backend.set_auth(username, password)


# Verzeichnis mit den .docx-Hausvorlagen sowie Ausgabeverzeichnis fuer die
# erzeugten Dokumente. Beide sind ueber Umgebungsvariablen konfigurierbar.
ASSETS_DIR = Path(os.environ.get("ASSETS_DIR", Path(__file__).resolve().parent / "assets"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", Path(__file__).resolve().parent / "output"))

# Maximale Anzahl an Enaio-Uploads pro Minute. Wird das Limit ueberschritten,
# lehnt create_case_document den Upload sofort mit HTTP 429 ab. Ueber die
# Umgebungsvariable UPLOAD_RATE_LIMIT_PER_MINUTE konfigurierbar (Default 30);
# ein Wert <= 0 deaktiviert die Begrenzung.
UPLOAD_RATE_LIMIT_PER_MINUTE = int(os.environ.get("UPLOAD_RATE_LIMIT_PER_MINUTE", "30"))
upload_limiter = RateLimiter(UPLOAD_RATE_LIMIT_PER_MINUTE)

# Zuordnungsliste: Dokumententyp -> zugehoerige Vorlage. Die Betreffzeile wird in den
# Vorlagen einheitlich ueber den Platzhalter [Betreff] gesetzt (siehe vorlage.py). Der
# Lookup erfolgt case-insensitive ueber den Schluessel.
DOCUMENT_TEMPLATES = {
        "vermerk": {"template": "Vorlage_Vermerk.docx"},
        "brief": {"template": "Vorlage_Brief.docx"},
}


def _sanitize_filename(text: str) -> str:
        """Ersetzt fuer Dateinamen problematische Zeichen durch Unterstriche."""
        return re.sub(r"[^A-Za-z0-9._-]+", "_", (text or "").strip()).strip("._") or "dokument"


def _resolve_template(document_type: str):
        """Ermittelt die Vorlagendatei zu einem Dokumententyp.

        :returns: Tupel ``(vorlagenname, pfad)``.
        :raises HTTPException: 400 bei unbekanntem Typ, 404 bei fehlender Datei.
        """

        type_key = (document_type or "").strip().lower()
        mapping = DOCUMENT_TEMPLATES.get(type_key)
        if mapping is None:
                available = ", ".join(sorted(DOCUMENT_TEMPLATES)) or "(keine)"
                raise HTTPException(
                        status_code=400,
                        detail=(
                                f"Unbekannter Dokumententyp '{document_type}'. "
                                f"Verfuegbare Typen: {available}."
                        ),
                )

        template_name = mapping["template"]
        template_path = ASSETS_DIR / template_name
        if not template_path.exists():
                raise HTTPException(
                        status_code=404,
                        detail=(
                                f"Vorlage fuer Dokumententyp '{document_type}' nicht gefunden: "
                                f"{template_path}."
                        ),
                )

        return template_name, template_path


def _output_path(document_type: str, betreff: Optional[str]):
        """Baut Zielpfad und Dateinamen des zu erzeugenden Dokuments.

        Schema: ``<Zeitstempel>_<Dokumententyp>[_<Betreff>].docx``.

        :returns: Tupel ``(pfad, dateiname)``.
        """

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = f"_{_sanitize_filename(betreff)}" if betreff else ""
        out_name = f"{timestamp}_{_sanitize_filename(document_type)}{suffix}.docx"
        return OUTPUT_DIR / out_name, out_name


def _render_document(template_path, content, out_path, betreff, fields):
        """Fuellt die Vorlage und meldet Vorlagenfehler als HTTP 422."""

        try:
                return vorlage.fill_document(
                        template_path,
                        content,
                        out_path,
                        betreff=betreff,
                        fields=fields,
                )
        except (ValueError, FileNotFoundError) as e:
                raise HTTPException(status_code=422, detail=f"Fehler beim Fuellen der Vorlage: {e}")


async def _enforce_upload_rate_limit():
        """Belegt einen Upload-Slot oder lehnt mit HTTP 429 ab (kein Warten)."""

        try:
                await upload_limiter.acquire()
        except RateLimitExceeded as e:
                raise HTTPException(
                        status_code=429,
                        detail=str(e),
                        headers={"Retry-After": str(e.retry_after)},
                )


async def _discard_temp_file(path, ctx: Context):
        """Loescht die lokal erzeugte Datei; ein Fehlschlag ist nicht kritisch."""

        try:
                Path(path).unlink()
        except OSError as e:
                await ctx.info(f"Warnung: temporaere Datei {path} konnte nicht geloescht werden: {e}")


async def _load_document_content(document_id: str, content_format: str, ctx: Context):
        """Laedt den Inhalt eines Dokuments in der gewuenschten Repraesentation.

        :param content_format: ``"file"`` fuer die Originaldatei (bytes), sonst
            die Text-Rendition (str).
        """

        what = "Datei" if content_format == "file" else "Textinhalt"
        await ctx.info(f"Lade {what} zum Dokument {document_id}")

        document = await backend.get_document(document_id, content_format)
        return document["content"]


mcp = FastMCP(
        "Enaio MCP Server",
        instructions=(
                "Dieser Server gibt Zugriff auf Vorgänge (Akten) und deren Dokumente im "
                "Dokumenten-Management-System Enaio. Ein Vorgang wird über sein Aktenzeichen "
                "identifiziert, z. B. 'DS.1.2-2024-1234'. Wird ein Vorgang, eine Akte, ein Fall "
                "oder ein Aktenzeichen erwähnt, ist get_case_metadata das passende Tool. "
                "Dokumente werden über ihre Dokument-ID referenziert, die man aus dem "
                "'documents'-Feld der get_case_metadata-Antwort erhält. "
                "Wird nach allen offenen bzw. laufenden Vorgängen einer Person gefragt "
                "('welche Vorgänge laufen bei mir', 'offene Akten von ...'), ist "
                "list_running_cases das passende Tool."
        ),
)


@mcp.tool(
        annotations=ToolAnnotations(
                title="Vorgang / Akte abrufen",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
        ),
)
async def get_case_metadata(
        reference: Annotated[
                str,
                "Aktenzeichen des Vorgangs, z. B. 'DS.1.2-2024-1234' "
                "(Format: DS.<Zahl>.<Zahl>-<Jahr>-<laufende Nummer>).",
        ],
        ctx: Context,
) -> dict:
        """
        Ruft Metadaten und die Dokumentliste zu einem Vorgang (auch: Akte, Fall,
        Vorgangsakte) aus dem Enaio-Dokumenten-Management-System ab.

        Nutze dieses Tool, wenn nach einem konkreten Vorgang, einer Akte oder einem
        Aktenzeichen gefragt wird - auch wenn nur eine Vorgangs- oder Fallnummer im
        Text steht, ohne dass explizit das Wort "Aktenzeichen" fällt.

        Rückgabe enthält u.a. Titel, Kategorie, Sachbearbeiter sowie ein
        "documents"-Feld mit allen zugehörigen Dokumenten (inkl. Dokument-ID), die
        mit access_document_fulltext oder download_document abgerufen werden können.

        :param reference: Aktenzeichen des Vorgangs (Pflichtformat siehe oben).
        """

        await ctx.info("Suche nach Vorgangsinformationen in ENAIO")
        akte, record = await backend.get_aktenzeichen(reference)

        await ctx.info(f"Lade Liste aller Dokumente zum Vorgang {reference} ({akte})")
        record["documents"] = await backend.get_document_list(akte)

        return record


@mcp.tool(
        annotations=ToolAnnotations(
                title="Laufende Vorgänge auflisten",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
        ),
)
async def list_running_cases(
        user: Annotated[
                str,
                "Benutzerkürzel des Aktenverantwortlichen, z. B. 'gisch'. "
                "Groß-/Kleinschreibung spielt keine Rolle.",
        ],
        ctx: Context,
) -> dict:
        """
        Listet alle laufenden (offenen, noch nicht abgeschlossenen) Vorgänge auf,
        für die der angegebene Benutzer als Aktenverantwortlicher eingetragen ist.

        Nutze dieses Tool, wenn nach einer Übersicht gefragt wird - etwa "welche
        Vorgänge laufen bei mir", "meine offenen Akten" oder "woran arbeitet
        Person X gerade" - also immer dann, wenn noch kein konkretes Aktenzeichen
        bekannt ist.

        Zurückgegeben wird eine kompakte Liste ohne Akteninhalt. Zu jedem Treffer
        liefert 'reference_nr' das Aktenzeichen, mit dem sich über
        get_case_metadata Details und die Dokumentliste nachladen lassen.

        :param user: Benutzerkürzel des Aktenverantwortlichen.
        """

        await ctx.info(f"Suche laufende Vorgänge von {user} in ENAIO")
        cases = await backend.get_running_cases(user)

        return {
                "user": user,
                "status": RUNNING_CASE_STATUS,
                "count": len(cases),
                "cases": cases,
        }


@mcp.tool(
        annotations=ToolAnnotations(
                title="Dokument aus Vorlage erzeugen",
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=False,
                openWorldHint=False,
        ),
)
async def create_case_document(
        reference: Annotated[
                str,
                "Aktenzeichen / Vorgangsnummer des Vorgangs, z. B. 'DS.1.2-2024-1234', "
                "zu dem das Dokument erstellt wird.",
        ],
        document_type: Annotated[
                str,
                "Dokumententyp, der die zu verwendende Vorlage bestimmt, z. B. 'Vermerk' oder 'Brief'.",
        ],
        content: Annotated[
                List[dict],
                (
                        "Liste von Inhaltsbloecken (JSON-Array), die den Dokumentkoerper bilden. "
                        "Jeder Block ist ein Objekt mit dem Feld 'type'. Unterstuetzte Typen:\n"
                        '- heading:    {"type":"heading","text":"1. Ueberschrift","size":24}  (size optional, halbe Punkt)\n'
                        '- subheading: {"type":"subheading","text":"Zwischenueberschrift"}\n'
                        '- para:       {"type":"para","runs":[{"t":"Fett: ","b":true},{"t":"normaler Text."}],"jc":"both"}\n'
                        '              Kurzform ohne Formatierung: {"type":"para","text":"einfacher Absatz"}\n'
                        '- listitem:   {"type":"listitem","number":1,"text":"nummerierter Punkt"}  '
                        '(ohne "number" bzw. number=null -> Aufzaehlung)\n'
                        '- table:      {"type":"table","header":["Sp1","Sp2"],"rows":[["a","b"],["c","d"]]}\n'
                        "Run-Attribute (innerhalb von 'runs'): t (Text, Pflicht), b (fett), i (kursiv), "
                        "size (halbe Punkt, z.B. 24), color (Hex ohne #). "
                        "Beispiel: "
                        '[{"type":"heading","text":"1. Sachverhalt"},'
                        '{"type":"subheading","text":"Auflagen"},'
                        '{"type":"para","runs":[{"t":"Wichtig: ","b":true},{"t":"normaler Text."}]},'
                        '{"type":"listitem","number":1,"text":"Erster Punkt"},'
                        '{"type":"table","header":["A","B"],"rows":[["1","2"]]}]'
                ),
        ],
        ctx: Context,
        betreff: Annotated[
                Optional[str],
                "Optionaler Betreff; ersetzt die Betreffzeile der Vorlage.",
        ] = None,
        fields: Annotated[
                Optional[dict],
                (
                        "Optionale Zuordnung von Platzhaltern zu Ersatztexten (JSON-Objekt). "
                        "Schluessel ist der Platzhaltername OHNE eckige Klammern, z.B. 'Aktenzeichen' "
                        "fuer den Platzhalter [Aktenzeichen] in der Vorlage; Wert ist der einzusetzende "
                        "Text. Beispiel: {\"Aktenzeichen\":\"DS.1.2-2024-1234\",\"Bearbeiter\":\"Max Mustermann\"}. "
                        "Der Platzhalter [Datum] wird stets automatisch mit dem aktuellen Datum befuellt "
                        "und kann nicht ueberschrieben werden. [Aktenzeichen] wird - sofern nicht "
                        "angegeben - aus 'reference' uebernommen. Nicht angegebene Platzhalter bleiben "
                        "unveraendert im Dokument."
                ),
        ] = None,
) -> dict:
        """
        Erzeugt ein Word-Dokument (.docx) fuer einen Vorgang, indem eine zum
        Dokumententyp passende Hausvorlage mit den uebergebenen Inhalten befuellt wird.

        Anhand von document_type wird ueber eine Zuordnungsliste die passende
        .docx-Vorlage ausgewaehlt und mit den Bloecken aus content sowie - falls
        angegeben - dem betreff gefuellt. Briefkopf, Logo und Fusszeile der Vorlage
        bleiben erhalten.

        Das erzeugte Dokument wird lokal gespeichert und anschliessend ueber die
        Enaio-API in den zugehoerigen Vorgang (reference) hochgeladen. Ein
        RateLimiter begrenzt die Uploads auf UPLOAD_RATE_LIMIT_PER_MINUTE pro
        Minute; wird das Limit ueberschritten, wird der Upload mit HTTP 429
        abgelehnt (das lokal erzeugte Dokument bleibt erhalten).

        :param reference: Aktenzeichen / Vorgangsnummer.
        :param document_type: Dokumententyp (z. B. 'Vermerk', 'Brief').
        :param content: Liste von Inhaltsbloecken.
        :param betreff: Optionaler Betreff.
        :param fields: Optionale Zuordnung Platzhaltername -> Ersatztext; [Datum] immer
                aktuelles Datum, [Aktenzeichen] faellt auf reference zurueck.
        """

        template_name, template_path = _resolve_template(document_type)

        await ctx.info(
                f"Erzeuge {document_type}-Dokument fuer Vorgang {reference} aus Vorlage "
                f"{template_name}"
        )

        out_path, out_name = _output_path(document_type, betreff)

        # Platzhalter-Werte aufbereiten: [Aktenzeichen] faellt auf reference zurueck, wenn nicht
        # ausdruecklich angegeben. [Datum] setzt fill_document stets auf das aktuelle Datum.
        doc_fields = dict(fields or {})
        doc_fields.setdefault("Aktenzeichen", reference)

        written = _render_document(template_path, content, out_path, betreff, doc_fields)

        await ctx.info(f"Dokument lokal gespeichert unter {written}")

        # Rate-Limit pruefen, bevor tatsaechlich hochgeladen wird: ein belegter
        # Slot entspricht damit einem echten Upload-Versuch.
        await _enforce_upload_rate_limit()

        await ctx.info(f"Lade Dokument in Vorgang {reference} nach Enaio hoch")
        upload = await backend.upload_document(
                reference,
                written,
                document_type,
                betreff,
                out_name,
        )

        await _discard_temp_file(written, ctx)

        return {
                "reference_nr": reference,
                "document_type": document_type,
                "betreff": betreff,
                "template": template_name,
                "blocks": len(content),
                "stored_in_enaio": True,
                "enaio_object_id": upload.get("objectId"),
        }


@mcp.tool(
        annotations=ToolAnnotations(
                title="Dokumentinhalt als Text lesen",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
        ),
)
async def access_document_fulltext(
        document: Annotated[str, "Dokument-ID, z. B. aus dem 'documents'-Feld von get_case_metadata."],
        ctx: Context,
) -> str:
        """
        Liest den Volltext eines Dokuments aus einem Vorgang als Klartext, z. B. um
        Inhalte zu zitieren, zusammenzufassen oder im Gespräch zu durchsuchen.

        Verwende dieses Tool (nicht download_document), wenn der Inhalt gelesen,
        zitiert oder ausgewertet werden soll. Wenn stattdessen die Originaldatei
        (z. B. als Anhang oder zum Download) benötigt wird, nutze download_document.

        :param document: Dokument-ID.
        """

        return await _load_document_content(document, "text", ctx)


@mcp.tool(
        annotations=ToolAnnotations(
                title="Dokument als Datei herunterladen",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
        ),
)
async def download_document(
        document: Annotated[str, "Dokument-ID, z. B. aus dem 'documents'-Feld von get_case_metadata."],
        ctx: Context,
) -> str:
        """
        Lädt ein Dokument als Originaldatei herunter (Base64-kodierter Binärinhalt),
        z. B. um es weiterzuleiten oder als Anhang bereitzustellen.

        Verwende dieses Tool (nicht access_document_fulltext), wenn die Originaldatei
        selbst benötigt wird, nicht nur ihr Textinhalt.

        :param document: Dokument-ID.
        """

        content = await _load_document_content(document, "file", ctx)

        return base64.b64encode(content).decode("ascii")


@mcp.resource("document://{document}/fulltext")
async def resource_access_document_fulltext(document: str, ctx: Context) -> str:
        """
        Access documents fulltext. The document's content is provided as text representation.
        :param document: Dokument-ID
        """

        return await _load_document_content(document, "text", ctx)


@mcp.resource("document://{document}/file")
async def resource_download_document(document: str, ctx: Context) -> bytes:
        """
        Access document and download as file. The document's content is provided as binary representation.
        :param document: Dokument-ID
        """

        return await _load_document_content(document, "file", ctx)


if __name__ == "__main__":
    mcp.run(transport="stdio")
