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
        Allows clients or AI agents to retrieve a list of metadata associated with a specific case in the record management system. Each case is identified by a unique reference number, wich must be supplied in the request. The result is provided as JSON dictionary of metadata associated with this case.
        :param reference: Reference number of a specific case
        """

        await ctx.info("Suche nach Vorgangsinformationen in ENAIO")

        objectId, record = await backend.getAktenzeichen(reference)

        return record


@mcp.tool
async def list_case_documents(reference: Annotated[str, "case reference number"], ctx: Context) -> List:
        """
        Allows clients or AI agents to retrieve a list of existing documents filed in a specific case in the record management system and their metadata. The unqiue reference number of the case must be supplied. The result is provided as an array of JSON dictionaries for each document.
        :param reference: Reference number of a specific case
        """

        await ctx.info(f"Lade Liste aller Dokumente zum Vorgang {reference}")

        json = None
        akte = await backend.getAktenzeichen(reference)
        result = await backend.getDocumentList(akte[0])

        return result


@mcp.resource("document://{document_nr}/text")
async def download_document_text_content(document_nr: str, ctx: Context) -> str:
        """
        Allows clients or AI agents to download document contents. Each document is identified by a unique document number, wich must be supplied in the request. The document's content is provided as text extract.
        :param document_nr: Document number of a specific document
        """

        await ctx.info(f"Lade Textinhalt zum Dokument {document_nr}")

        document, json = await backend.getDocument(document_nr, "text")

        return document["content"]

@mcp.resource("document://{document_nr}/file")
async def download_document_file(document_nr: str, ctx: Context) -> str:
        """
        Allows clients or AI agents to download document contents. Each document is identified by a unique document number, wich must be supplied in the request. The document's content is provided as binary.
        :param document_nr: Document number of a specific document
        """

        await ctx.info(f"Lade Datei zum Dokument {document_nr}")

        document, json = await backend.getDocument(document_nr, "file")

        return document["content"]


if __name__ == "__main__":
    mcp.add_transform(ResourcesAsTools(mcp))
    mcp.run(transport="http", port=8000)