import os
import re
import json
import base64

from datetime import datetime
from pathlib import Path

from fastmcp import FastMCP, Context
from fastmcp.tools.tool import ToolAnnotations
from typing import Annotated, List, Optional
from fastapi import HTTPException

import vorlage
from EnaioBackend import EnaioBackend
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
backend.setAuth(username, password)


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

# Zuordnungsliste: Dokumententyp -> zugehoerige Vorlage und der in der Vorlage
# vorhandene Text der Betreffzeile (subject_placeholder), der bei Angabe eines
# Betreffs ersetzt wird. Der Lookup erfolgt case-insensitive ueber den Schluessel.
DOCUMENT_TEMPLATES = {
        "vermerk": {"template": "Vorlage_Vermerk.docx", "subject_placeholder": "Vermerk"},
        "brief": {"template": "Vorlage_Brief.docx", "subject_placeholder": "Brief"},
}


def _sanitize_filename(text: str) -> str:
        """Ersetzt fuer Dateinamen problematische Zeichen durch Unterstriche."""
        return re.sub(r"[^A-Za-z0-9._-]+", "_", (text or "").strip()).strip("._") or "dokument"


mcp = FastMCP(
        "Enaio MCP Server",
        instructions=(
                "Dieser Server gibt Zugriff auf Vorgänge (Akten) und deren Dokumente im "
                "Dokumenten-Management-System Enaio. Ein Vorgang wird über sein Aktenzeichen "
                "identifiziert, z. B. 'DS.1.2-2024-1234'. Wird ein Vorgang, eine Akte, ein Fall "
                "oder ein Aktenzeichen erwähnt, ist get_case_metadata das passende Tool. "
                "Dokumente werden über ihre Dokument-ID referenziert, die man aus dem "
                "'documents'-Feld der get_case_metadata-Antwort erhält."
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
        akte, record = await backend.getAktenzeichen(reference)

        await ctx.info(f"Lade Liste aller Dokumente zum Vorgang {reference} ({akte})")
        documents = await backend.getDocumentList(akte)
        record["documents"] = documents

        return record


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

        template_path = ASSETS_DIR / mapping["template"]
        if not template_path.exists():
                raise HTTPException(
                        status_code=404,
                        detail=(
                                f"Vorlage fuer Dokumententyp '{document_type}' nicht gefunden: "
                                f"{template_path}."
                        ),
                )

        await ctx.info(
                f"Erzeuge {document_type}-Dokument fuer Vorgang {reference} aus Vorlage "
                f"{mapping['template']}"
        )

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = f"_{_sanitize_filename(betreff)}" if betreff else ""
        out_name = f"{timestamp}_{_sanitize_filename(document_type)}{suffix}.docx"
        out_path = OUTPUT_DIR / out_name

        # Platzhalter-Werte aufbereiten: [Aktenzeichen] faellt auf reference zurueck, wenn nicht
        # ausdruecklich angegeben. [Datum] setzt fill_document stets auf das aktuelle Datum.
        doc_fields = dict(fields or {})
        doc_fields.setdefault("Aktenzeichen", reference)

        try:
                written = vorlage.fill_document(
                        template_path,
                        content,
                        out_path,
                        betreff=betreff,
                        subject_placeholder=mapping.get("subject_placeholder"),
                        fields=doc_fields,
                )
        except (ValueError, FileNotFoundError) as e:
                raise HTTPException(status_code=422, detail=f"Fehler beim Fuellen der Vorlage: {e}")

        await ctx.info(f"Dokument lokal gespeichert unter {written}")

        # Rate-Limit pruefen, bevor tatsaechlich hochgeladen wird: ein belegter
        # Slot entspricht damit einem echten Upload-Versuch. Bei Ueberschreitung
        # sofortige Ablehnung mit HTTP 429 (kein Warten).
        try:
                await upload_limiter.acquire()
        except RateLimitExceeded as e:
                raise HTTPException(
                        status_code=429,
                        detail=str(e),
                        headers={"Retry-After": str(e.retry_after)},
                )

        await ctx.info(f"Lade Dokument in Vorgang {reference} nach Enaio hoch")
        upload = await backend.uploadDocument(
                reference,
                written,
                document_type,
                betreff,
                out_name,
        )

        try:
                Path(written).unlink()
        except OSError as e:
                await ctx.info(f"Warnung: temporaere Datei {written} konnte nicht geloescht werden: {e}")

        return {
                "reference_nr": reference,
                "document_type": document_type,
                "betreff": betreff,
                "template": mapping["template"],
                "blocks": len(content),
                "stored_in_enaio": True,
                "enaio_object_id": upload.get("objectId"),
        }


# @mcp.tool
# async def list_case_documents(reference: Annotated[str, "case reference number"], ctx: Context) -> List:
#         """
#         Erstelle eine Liste aller Dokumente, die zu einem laufenden Vorgang gehören.
#         :param reference: Vorgangsnummer
#         """

#         await ctx.info(f"Lade Liste aller Dokumente zum Vorgang {reference}")

#         json = None
#         akte, record = await backend.getAktenzeichen(reference)
#         result = await backend.getDocumentList(akte[0])

#         return {"reference_nr": reference, "documents": result }


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

        await ctx.info(f"Lade Textinhalt zum Dokument {document}")

        document, json = await backend.getDocument(document, "text")

        return document["content"]


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

        await ctx.info(f"Lade Datei zum Dokument {document}")

        document, json = await backend.getDocument(document, "file")

        return base64.b64encode(document["content"])



@mcp.resource("document://{document}/fulltext")
async def resource_access_document_fulltext(document: str, ctx: Context) -> str:
        """
        Access documents fulltext. The document's content is provided as text representation.
        :param document: Dokument-ID
        """

        await ctx.info(f"Lade Textinhalt zum Dokument {document}")

        document, json = await backend.getDocument(document, "text")

        return document["content"]

@mcp.resource("document://{document}/file")
async def resource_download_document(document: str, ctx: Context) -> str:
        """
        Access document and download as file. The document's content is provided as binary representation.
        :param document_nr: Dokument-ID
        """

        await ctx.info(f"Lade Datei zum Dokument {document}")

        document, json = await backend.getDocument(document, "file")

        return document["content"]

if __name__ == "__main__":
    mcp.run(transport="stdio")