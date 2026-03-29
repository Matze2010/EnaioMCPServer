import os
import json
import base64

from fastmcp import FastMCP, Context
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


@mcp.tool
async def access_document_fulltext(document: Annotated[str, "ID of the document"], ctx: Context) -> str:
        """
        Access documents fulltext. The document's content is provided as text representation.
        :param document: Dokument-ID
        """

        await ctx.info(f"Lade Textinhalt zum Dokument {document}")

        document, json = await backend.getDocument(document, "text")

        return document["content"]


@mcp.tool
async def download_document(document: Annotated[str, "ID of the document"], ctx: Context) -> str:
        """
        Access document and download as file. The document's content is provided as binary representation.
        :param document_nr: Dokument-ID
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