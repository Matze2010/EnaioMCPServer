import os
import json
import uuid
import requests
import httpx
import logging
import urllib3
import re
import unicodedata
import tempfile

from pathlib import Path
from datetime import datetime
from pydantic import BaseModel, Field
from fastapi import HTTPException

AKTENZEICHEN_REGEX = "DS\\.[1-9]\\.[1-9]-(202[2-6])-(\\d|[1-9]\\d{1,5})"
DOCUMENT_REGEX = "(202[2-6])-(\\d|[1-9]\\d{1,6})|(\\d{1,12})"

# Objekttyp-ID der Vorgangsdokumente (OSTPL_AA_DOKUMENT), unter der neu erzeugte
# Dokumente in einen Vorgang eingehaengt werden. Entspricht dem Eintrag "262146"
# in EnaioBackend.settings.
UPLOAD_OBJECT_TYPE_ID = "262146"

# MIME-Type fuer die erzeugten Word-Dokumente (.docx).
DOCX_MIME_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

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
                        "id": document_nr,
                        "name": childDict.property("documentTitle"),
                        "creationDate": childDict.property("system:creationDate"),
                        "lastModificationDate": childDict.property(
                            "system:lastModificationDate"
                        )
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

    def _build_upload_payload(self, parent_id, file_bytes, betreff, filename):
        """Baut den Multipart-Body fuer POST /api/dms/objects.

        Erzeugt genau zwei Parts gemaess der Enaio-DMS-REST-Spezifikation:

        * ``name="data"`` (application/json) mit den Objekt-Metadaten und einem
          ``contentStreams``-Eintrag, der ueber ``cid`` auf den Inhalts-Part
          verweist.
        * ``name="<cid>"`` mit dem Binaerinhalt (zusaetzlich ``Content-ID``-Header).

        Der Multipart wird bewusst manuell zusammengesetzt: Der ``data``-Part
        muss ``application/json`` sein (nicht text/plain) und der Inhalts-Part
        benoetigt einen ``Content-ID``-Header, was ueber httpx' ``files=`` nicht
        zuverlaessig steuerbar ist. Diese Kapselung ist die zentrale Stelle, an
        der die Request-Form bei Bedarf angepasst werden kann.

        :returns: Tupel ``(body_bytes, content_type_header)``.
        """

        cid = "cid_document"

        data = {
            "objects": [
                {
                    "properties": {
                        "system:objectTypeId": {"value": UPLOAD_OBJECT_TYPE_ID},
                        "system:parentId": {"value": str(parent_id)},
                        "Betreff": {"value": betreff or filename},
                    },
                    "contentStreams": [
                        {
                            "mimeType": DOCX_MIME_TYPE,
                            "fileName": filename,
                            "cid": cid,
                        }
                    ],
                }
            ]
        }

        boundary = uuid.uuid4().hex
        crlf = b"\r\n"
        dash = b"--"
        boundary_bytes = boundary.encode("ascii")

        parts = []

        # Part 1: Metadaten als JSON.
        parts.append(dash + boundary_bytes + crlf)
        parts.append(b'Content-Disposition: form-data; name="data"' + crlf)
        parts.append(b"Content-Type: application/json;charset=UTF-8" + crlf)
        parts.append(crlf)
        parts.append(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        parts.append(crlf)

        # Part 2: Dateiinhalt, referenziert ueber die cid.
        cid_bytes = cid.encode("ascii")
        filename_header = filename.replace('"', "")
        parts.append(dash + boundary_bytes + crlf)
        parts.append(
            b'Content-Disposition: form-data; name="'
            + cid_bytes
            + b'"; filename="'
            + filename_header.encode("utf-8")
            + b'"'
            + crlf
        )
        parts.append(b"Content-ID: " + cid_bytes + crlf)
        parts.append(b"Content-Type: " + DOCX_MIME_TYPE.encode("ascii") + crlf)
        parts.append(crlf)
        parts.append(file_bytes)
        parts.append(crlf)

        # Abschlussgrenze.
        parts.append(dash + boundary_bytes + dash + crlf)

        body = b"".join(parts)
        content_type = f"multipart/form-data; boundary={boundary}"
        return body, content_type

    @staticmethod
    def _extract_object_id(data):
        """Liest die system:objectId des ersten Objekts aus der Upload-Antwort."""
        try:
            props = data["objects"][0]["properties"]
            return props["system:objectId"]["value"]
        except (KeyError, IndexError, TypeError):
            return None

    async def uploadDocument(self, reference, file_path, document_type, betreff, filename):
        """Laedt eine Datei als neues Dokument in den Vorgang hoch.

        Das Dokument wird als OSTPL_AA_DOKUMENT-Objekt unter dem ueber
        ``reference`` (Aktenzeichen) ermittelten Vorgang angelegt.

        :param reference: Aktenzeichen des Ziel-Vorgangs.
        :param file_path: Pfad zur hochzuladenden Datei (.docx).
        :param document_type: Dokumententyp (aktuell nur informativ/Logging).
        :param betreff: Betreff/Titel des Dokuments (Fallback: Dateiname).
        :param filename: Anzuzeigender Dateiname in Enaio.
        :returns: ``{"objectId": <id>, "reference_nr": <reference>}``.
        """

        # Elternobjekt (Vorgang) ermitteln; wirft 404, wenn nicht vorhanden.
        parent_id, _ = await self.getAktenzeichen(reference)

        file_bytes = Path(file_path).read_bytes()
        body, content_type = self._build_upload_payload(
            parent_id, file_bytes, betreff, filename
        )

        try:
            self.logger.info(
                f"Lade Dokument ({document_type}) in Vorgang {reference} "
                f"(Parent {parent_id}) hoch"
            )
            response = await self.session.post(
                self.backendUrl + "/api/dms/objects?minimalResponse=true",
                content=body,
                headers={
                    "accept": "application/json",
                    "Content-Type": content_type,
                },
            )
        except httpx.RequestError as e:
            raise HTTPException(
                status_code=503, detail=f"Error connecting to ENAIO API: {e}"
            )

        # Laut Spezifikation signalisiert 422 fehlgeschlagene Inserts.
        if response.status_code == 422:
            raise HTTPException(
                status_code=422,
                detail=f"Enaio hat den Upload abgelehnt (422): {response.text}",
            )

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise HTTPException(
                status_code=502,
                detail=f"Unerwartete Antwort der ENAIO API: {e}",
            )

        try:
            data = response.json()
        except ValueError:
            data = {}

        object_id = self._extract_object_id(data)

        self.logger.info(
            f"Dokument in Vorgang {reference} hochgeladen (ObjectId {object_id})"
        )

        return {"objectId": object_id, "reference_nr": reference}