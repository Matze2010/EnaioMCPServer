import os
import json
import base64

from fastmcp import FastMCP, Context
from fastmcp.tools.tool import ToolAnnotations
from typing import Annotated, List
from EnaioBackend import EnaioBackend

url = os.environ.get('URL', 'DEFAULT_URL')
username = os.environ.get('USERNAME', 'DEFAULT_USERNAME')
password = os.environ.get('PASSWORD', 'DEFAULT_PASSWORD')

backend = EnaioBackend(url=url)
backend.setAuth(username, password)


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