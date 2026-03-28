import os
import json

from fastmcp import FastMCP, Context
from fastmcp.server.transforms import ResourcesAsTools
from typing import Annotated, List
from EnaioBackend import EnaioBackend

url = os.environ.get('URL', 'DEFAULT_URL')
username = os.environ.get('USERNAME', 'DEFAULT_USERNAME')
password = os.environ.get('PASSWORD', 'DEFAULT_PASSWORD')

backend = EnaioBackend(url=url)
backend.setAuth(username, password)


mcp = FastMCP("Enaio MCP Server")


@mcp.tool
async def get_case_metadata(reference: Annotated[str, "case reference number"], ctx: Context) -> dict:
        """
        Lese die Metadaten zu einem laufenden Vorgang aus dem Vorgangsbearbeitungssystem.
        :param reference: Vorgangsnummer
        """

        await ctx.info("Suche nach Vorgangsinformationen in ENAIO")

        objectId, record = await backend.getAktenzeichen(reference)

        return record


@mcp.tool
async def list_case_documents(reference: Annotated[str, "case reference number"], ctx: Context) -> List:
        """
        Erstelle eine Liste aller Dokumente, die zu einem laufenden Vorgang gehören.
        :param reference: Vorgangsnummer
        """

        await ctx.info(f"Lade Liste aller Dokumente zum Vorgang {reference}")

        json = None
        akte = await backend.getAktenzeichen(reference)
        result = await backend.getDocumentList(akte[0])

        return result


@mcp.resource("document://{document_nr}/text")
async def download_document_text_content(document_nr: str, ctx: Context) -> str:
        """
        Lade den Inhalt eines Dokuments als reiner Text.
        :param document_nr: Dokumenten-Nr
        """

        await ctx.info(f"Lade Textinhalt zum Dokument {document_nr}")

        document, json = await backend.getDocument(document_nr, "text")

        return document["content"]

@mcp.resource("document://{document_nr}/file")
async def download_document_file(document_nr: str, ctx: Context) -> str:
        """
        Lade ein Dokument als Datei.
        :param document_nr: Dokumenten-Nr
        """

        await ctx.info(f"Lade Datei zum Dokument {document_nr}")

        document, json = await backend.getDocument(document_nr, "file")

        return document["content"]


mcp.add_transform(ResourcesAsTools(mcp))

if __name__ == "__main__":
    mcp.run(transport="http", port=8000)