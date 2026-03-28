import os
import requests
import httpx
import logging
import urllib3
import re
import unicodedata
import tempfile

from datetime import datetime
from pydantic import BaseModel, Field
from fastapi import HTTPException

AKTENZEICHEN_REGEX = "DS\\.[1-9]\\.[1-9]-(202[2-6])-(\\d|[1-9]\\d{1,5})"
DOCUMENT_REGEX = "(202[2-6])-(\\d|[1-9]\\d{1,6})|(\\d{1,12})"

def standardize_text(text: str) -> str:
    # Convert text to lowercase
    text = text.lower()
    # replace carriage return newlines
    text = text.replace("\r\n", " ")
    text = text.replace("\r", "")
    text = text.replace("\n", " ")
    # Normalize unicode characters to ASCII
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("utf-8")
    # Remove punctuation
    # text = re.sub(r'[^ws]', '', text)
    # Remove extra whitespace
    text = re.sub("\W+", " ", text)
    # Optionally truncate content if it's very large
    text = " ".join(text.split()[:5000])
    return text

class EnaioDict(dict):
    def property(self, key):
        return self["properties"][key]["value"]


class EnaioBackend:

    def __init__(self, url):

        urllib3.disable_warnings()

        self.backendUrl = url

        self.session = httpx.AsyncClient(verify=False)

        self.logger = logging.getLogger(__name__)

        self.settings = {
            "262146": {
                "type": "file",
                "table": "OSTPL_AA_DOKUMENT",
                "fields": ["AA_DOK_PENR", "Betreff"],
            },
            "393216": {
                "type": "mail",
                "table": "EMail",
                "fields": ["system:objectId", "MAIL_SUBJECT"],
            },
            "262144": {
                "type": "vermerk",
                "table": "OSTPL_AA_AN",
                "fields": ["system:objectId", "OSTPL_AA_AN_CONTACTMEDIA"],
            },
        }

    def setAuth(self, username: str, password: str):
        self.session.auth = httpx.BasicAuth(username=username, password=password)

    async def getAktenzeichen(self, aktenzeichen):
        data = None

        folder_query_params = {
            "query": {
                "statement": "SELECT system:objectId, Aktenbezeichnung, Kategorisierung, Aktenverantwortlicher, Aktenplaneintrag, Aktenzeichen, Akteninhalt FROM OSTPL_AA WHERE Aktenzeichen=@aktenzeichen",
                "skipCount": 0,
                "handleDeletedDocuments": "DELETED_DOCUMENTS_EXCLUDE",
                "options": {"Rights": 0, "RegisterContext": 0},
                "parameters": {"aktenzeichen": aktenzeichen},
            }
        }

        try:
            self.logger.info(f"Getting Aktenzeichen {aktenzeichen}")
            response = await self.session.post(
                self.backendUrl + "/api/dms/objects/search",
                json=folder_query_params,
                headers={"accept": "application/json"},
            )
            response.raise_for_status()
            data = response.json()

        except requests.exceptions.RequestException as e:
            raise HTTPException(
                status_code=503, detail=f"Error connecting to ENAIO API: {e}"
            )
        except Exception as e:
            # Catch other potential errors during processing
            raise HTTPException(
                status_code=500, detail=f"An internal error occurred: {e}"
            )

        if len(data["objects"]) == 0:
            raise HTTPException(
                status_code=404, detail=f"Aktenzeichen '{aktenzeichen}' not found"
            )

        akteJSON = EnaioDict(data["objects"][0])
        record = {
            "reference_nr": akteJSON.property("Aktenzeichen"),
            "title": akteJSON.property("Aktenbezeichnung"),
            "category": akteJSON.property("Kategorisierung"),
            "topics": akteJSON.property("Aktenplaneintrag").split("|"),
            "sachbearbeiter": akteJSON.property("Aktenverantwortlicher"),
        }

        self.logger.debug("Found Aktenzeichen %s", record)
        return (akteJSON.property("system:objectId"), record)

    async def getDocumentList(self, parentObjectId):

        documents = []

        for key, config in self.settings.items():

            fieldDocIdentifier = config["fields"][0]
            fieldDocTitle = config["fields"][1]
            fieldTable = config["table"]
            docType = config["type"]

            children_query_params = {
                "query": {
                    "statement": f"SELECT system:creationDate, system:lastModificationDate, {fieldDocIdentifier} AS documentIdentifier, {fieldDocTitle} AS documentTitle FROM {fieldTable} WHERE system:SDSTA_ID IN (@objectIds)",
                    "skipCount": 0,
                    "handleDeletedDocuments": "DELETED_DOCUMENTS_EXCLUDE",
                    "options": {"Rights": 0, "RegisterContext": 0},
                    "parameters": {"objectIds": parentObjectId},
                }
            }

            try:
                self.logger.info(
                    f"Getting documentlist({docType}) of ParentObjectId {parentObjectId}"
                )
                response = await self.session.post(
                    self.backendUrl + "/api/dms/objects/search",
                    json=children_query_params,
                    headers={"accept": "application/json"},
                )
                response.raise_for_status()
                data = response.json()

            except requests.exceptions.RequestException as e:
                raise HTTPException(
                    status_code=503, detail=f"Error connecting to ENAIO API: {e}"
                )
            except Exception as e:
                # Catch other potential errors during processing
                raise HTTPException(
                    status_code=500, detail=f"An internal error occurred: {e}"
                )

            if len(data["objects"]) == 0:
                self.logger.info(
                    f"No children of type {docType} for ParentObjectId {parentObjectId}"
                )
                continue

            children = data["objects"]

            for child in children:
                childDict = EnaioDict(child)
                document_nr = childDict.property("documentIdentifier")

                documents.append(
                    {
                        "type": config["type"],
                        "document_nr": document_nr,
                        "name": childDict.property("documentTitle"),
                        "creationDate": childDict.property("system:creationDate"),
                        "lastModificationDate": childDict.property(
                            "system:lastModificationDate"
                        ),
                        "resource": f"document://{document_nr}/text"
                    }
                )

            self.logger.debug(
                "Children for ParentObjectId %s: %s", parentObjectId, children
            )

        return documents

    async def getDocument(self, documentId, format):

        ### system:objectId, AA_DOK_PENR, Betreff
        union_query_params = {
            "query": {
                "statement": "SELECT * FROM OSTPL_AA_DOKUMENT where AA_DOK_PENR=@objectId UNION SELECT * FROM OSTPL_AA_AN where system:objectId=@objectId UNION SELECT * FROM EMail where system:objectId=@objectId",
                "skipCount": 0,
                "limit": 1,
                "options": {
                    "Rights": 0,
                    "Baseparams": 1,
                    "RegisterContext": 0,
                    "FileInfo": 1,
                },
                "parameters": {"objectId": documentId},
            }
        }

        try:
            self.logger.info(f"Getting document {documentId}")
            response = await self.session.post(
                self.backendUrl + "/api/dms/objects/search",
                json=union_query_params,
                headers={"accept": "application/json"},
            )
            response.raise_for_status()  # Raise an exception for bad status codes (4xx or 5xx)
            data = response.json()

        except requests.exceptions.RequestException as e:
            raise HTTPException(
                status_code=503, detail=f"Error connecting to ENAIO API: {e}"
            )
        except Exception as e:
            # Catch other potential errors during processing
            raise HTTPException(
                status_code=500, detail=f"An internal error occurred: {e}"
            )

        if len(data["objects"]) == 0:
            raise HTTPException(
                status_code=404, detail=f"Document '{documentId}' not found"
            )

        child = EnaioDict(data["objects"][0])
        self.logger.debug("Dokument: %s", child)

        config = self.settings[child.property("system:objectTypeId")]

        document = {
            "type": config["type"],
            "document_nr": child.property(config["fields"][0]),
            "name": child.property(config["fields"][1]),
            "creationDate": child.property("system:creationDate"),
            "lastModificationDate": child.property("system:lastModificationDate"),
        }

        if config["type"] == "vermerk":
            document["content"] = child.property("OSTPL_AA_AN_NOTIZ")
        else:
            objectId = child.property("system:objectId")
            if format == "file":
                document["content"] = await self.getFile(objectId)
            else:
                document["content"] = await self.getRendition(objectId)

        self.logger.info("Content: %s", document)

        return (document, child)

    async def getFile(self, documentId):
        response = await self.session.get(
            self.backendUrl + f"/api/dms/objects/{documentId}/contents/file/1"
        )

        return response.content

    async def getRendition(self, documentId) -> str:

        response = await self.session.get(
            self.backendUrl + f"/api/dms/objects/{documentId}/contents/renditions/text"
        )
        if response.status_code == requests.codes.ok:
            return standardize_text(response.text)

        return None