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

url = os.environ.get('URL', 'DEFAULT_URL')
username = os.environ.get('USERNAME', 'DEFAULT_USERNAME')
password = os.environ.get('PASSWORD', 'DEFAULT_PASSWORD')

backend = EnaioBackend(url=url)
backend.setAuth(username, password)


# Verzeichnis mit den .docx-Hausvorlagen sowie Ausgabeverzeichnis fuer die
# erzeugten Dokumente. Beide sind ueber Umgebungsvariablen konfigurierbar.
ASSETS_DIR = Path(os.environ.get("ASSETS_DIR", Path(__file__).resolve().parent / "assets"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", Path(__file__).resolve().parent / "output"))

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
                "Blockliste mit den Dokumenteninhalten. Unterstuetzte Blocktypen: "
                "heading, subheading, para, listitem, table. Beispiel: "
                '[{"type":"heading","text":"1. Sachverhalt"},'
                '{"type":"para","runs":[{"t":"Wichtig: ","b":true},{"t":"Text."}]}].',
        ],
        ctx: Context,
        betreff: Annotated[
                Optional[str],
                "Optionaler Betreff; ersetzt die Betreffzeile der Vorlage.",
        ] = None,
) -> dict:
        """
        Erzeugt ein Word-Dokument (.docx) fuer einen Vorgang, indem eine zum
        Dokumententyp passende Hausvorlage mit den uebergebenen Inhalten befuellt wird.

        Anhand von document_type wird ueber eine Zuordnungsliste die passende
        .docx-Vorlage ausgewaehlt und mit den Bloecken aus content sowie - falls
        angegeben - dem betreff gefuellt. Briefkopf, Logo und Fusszeile der Vorlage
        bleiben erhalten.

        Hinweis: In diesem Schritt wird das erzeugte Dokument ausschliesslich lokal
        gespeichert. Das Hochladen in den Enaio-Vorgang ist noch nicht implementiert.

        :param reference: Aktenzeichen / Vorgangsnummer.
        :param document_type: Dokumententyp (z. B. 'Vermerk', 'Brief').
        :param content: Liste von Inhaltsbloecken.
        :param betreff: Optionaler Betreff.
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
        out_name = f"{_sanitize_filename(reference)}_{_sanitize_filename(document_type)}_{timestamp}.docx"
        out_path = OUTPUT_DIR / out_name

        try:
                written = vorlage.fill_document(
                        template_path,
                        content,
                        out_path,
                        betreff=betreff,
                        subject_placeholder=mapping.get("subject_placeholder"),
                )
        except (ValueError, FileNotFoundError) as e:
                raise HTTPException(status_code=422, detail=f"Fehler beim Fuellen der Vorlage: {e}")

        await ctx.info(f"Dokument lokal gespeichert unter {written}")

        # TODO: Enaio-Upload noch nicht implementiert. Hier wird spaeter das erzeugte
        # Dokument ueber die Enaio-API in den Vorgang (reference) hochgeladen, z. B.
        # via backend.uploadDocument(reference, written, document_type, betreff).

        return {
                "reference_nr": reference,
                "document_type": document_type,
                "betreff": betreff,
                "template": mapping["template"],
                "blocks": len(content),
                "local_path": str(written),
                "stored_in_enaio": False,
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
    mcp.run(transport="http", port=8000)