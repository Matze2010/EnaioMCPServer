import json
import uuid
import httpx
import logging
import re
import unicodedata

from pathlib import Path
from typing import NamedTuple
from fastapi import HTTPException

# Objekttyp-ID der Vorgangsdokumente (OSTPL_AA_DOKUMENT), unter der neu erzeugte
# Dokumente in einen Vorgang eingehaengt werden.
UPLOAD_OBJECT_TYPE_ID = "262146"

# MIME-Type fuer die erzeugten Word-Dokumente (.docx).
DOCX_MIME_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

# Standardwert fuer handleDeletedDocuments in Suchanfragen.
EXCLUDE_DELETED = "DELETED_DOCUMENTS_EXCLUDE"

# Standard-Optionen einer Suchanfrage.
DEFAULT_SEARCH_OPTIONS = {"Rights": 0, "RegisterContext": 0}

SEARCH_PATH = "/api/dms/objects/search"


class ObjectType(NamedTuple):
    """Beschreibt einen in Enaio abgefragten Objekttyp.

    :ivar name:        Sprechender Typname in den Rueckgabedaten ("file", "mail", ...).
    :ivar table:       Enaio-Tabelle, aus der gelesen wird.
    :ivar id_field:    Feld, das die nach aussen sichtbare Dokument-ID traegt.
    :ivar title_field: Feld mit dem Titel/Betreff des Dokuments.
    """

    name: str
    table: str
    id_field: str
    title_field: str


# Alle Objekttypen, die als Dokumente eines Vorgangs gelten. Der Schluessel ist
# die system:objectTypeId, ueber die ein geladenes Objekt zugeordnet wird.
OBJECT_TYPES = {
    UPLOAD_OBJECT_TYPE_ID: ObjectType(
        "file", "OSTPL_AA_DOKUMENT", "AA_DOK_PENR", "Betreff"
    ),
    "393216": ObjectType("mail", "EMail", "system:objectId", "MAIL_SUBJECT"),
    "262144": ObjectType(
        "vermerk", "OSTPL_AA_AN", "system:objectId", "OSTPL_AA_AN_CONTACTMEDIA"
    ),
}


def standardize_text(text: str) -> str:
    # Convert text to lowercase
    text = text.lower()
    # replace carriage return newlines
    text = text.replace("\r\n", " ")
    text = text.replace("\r", "")
    text = text.replace("\n", " ")
    # Normalize unicode characters to ASCII
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("utf-8")
    # Remove extra whitespace
    text = re.sub(r"\W+", " ", text)
    # Optionally truncate content if it's very large
    text = " ".join(text.split()[:5000])
    return text


class EnaioDict(dict):
    def property(self, key):
        return self["properties"][key]["value"]


def encode_multipart(parts, boundary: str) -> bytes:
    """Setzt einen Multipart-Body aus vorbereiteten Parts zusammen.

    :param parts: Liste von ``(header_zeilen, payload)``. ``header_zeilen`` ist
        eine Liste von ``bytes`` ohne Zeilenumbruch, ``payload`` der Rohinhalt
        des Parts.
    :param boundary: Trennzeichenkette ohne fuehrende Bindestriche.
    :returns: Der vollstaendige Body inklusive Abschlussgrenze.
    """

    crlf = b"\r\n"
    dash = b"--"
    boundary_bytes = boundary.encode("ascii")

    chunks = []
    for headers, payload in parts:
        chunks.append(dash + boundary_bytes + crlf)
        for header in headers:
            chunks.append(header + crlf)
        chunks.append(crlf)
        chunks.append(payload)
        chunks.append(crlf)
    chunks.append(dash + boundary_bytes + dash + crlf)

    return b"".join(chunks)


class EnaioBackend:

    def __init__(self, url):

        self.backend_url = url

        self.session = httpx.AsyncClient(verify=False)

        self.logger = logging.getLogger(__name__)

    def set_auth(self, username: str, password: str):
        self.session.auth = httpx.BasicAuth(username=username, password=password)

    # ------------------------------------------------------------------
    # Gemeinsame Bausteine fuer alle API-Zugriffe
    # ------------------------------------------------------------------

    async def _request(self, method, path, *, context, raise_for_status=True, **kwargs):
        """Fuehrt einen Request gegen die Enaio-API aus und normiert die Fehler.

        Buendelt die Fehlerbehandlung, die andernfalls in jeder Methode
        wiederholt werden muesste:

        * ``httpx.RequestError``    -> HTTP 503 (Enaio nicht erreichbar)
        * ``httpx.HTTPStatusError`` -> HTTP 502 (unerwartete Antwort)
        * alles andere              -> HTTP 500

        :param context: Kurzbeschreibung des Vorgangs fuer die Logmeldung,
            z. B. ``"Aktenzeichen DS.1.2-2024-1"``.
        :param raise_for_status: Wenn ``False``, wird der Statuscode nicht
            geprueft und die Antwort unveraendert zurueckgegeben (fuer Aufrufer,
            die einzelne Statuscodes selbst behandeln).
        """

        try:
            response = await self.session.request(
                method, self.backend_url + path, **kwargs
            )
            if raise_for_status:
                response.raise_for_status()
            return response

        except httpx.RequestError as e:
            self.logger.error("Verbindungsfehler zur ENAIO API bei %s: %s", context, e)
            raise HTTPException(
                status_code=503, detail=f"Error connecting to ENAIO API: {e}"
            )
        except httpx.HTTPStatusError as e:
            self.logger.error("Unerwartete Antwort der ENAIO API bei %s: %s", context, e)
            raise HTTPException(
                status_code=502, detail=f"Unerwartete Antwort der ENAIO API: {e}"
            )
        except Exception as e:
            # Catch other potential errors during processing
            self.logger.exception("Interner Fehler bei %s", context)
            raise HTTPException(
                status_code=500, detail=f"An internal error occurred: {e}"
            )

    async def _search(
        self,
        statement,
        parameters,
        *,
        context,
        options=None,
        limit=None,
        handle_deleted=EXCLUDE_DELETED,
    ):
        """Fuehrt eine Suche ueber ``/api/dms/objects/search`` aus.

        :param statement: SQL-aehnliches Statement mit ``@name``-Parametern.
        :param parameters: Zuordnung Parametername -> Wert.
        :param context: Kurzbeschreibung fuer Logmeldungen.
        :param options: Abweichende Suchoptionen (Default: ``Rights``/``RegisterContext``).
        :param limit: Optionale Begrenzung der Treffermenge.
        :param handle_deleted: Umgang mit geloeschten Dokumenten; ``None`` laesst
            den Schluessel aus der Anfrage weg.
        :returns: Liste der Treffer als :class:`EnaioDict`.
        """

        query = {
            "statement": statement,
            "skipCount": 0,
            "options": options if options is not None else DEFAULT_SEARCH_OPTIONS,
            "parameters": parameters,
        }
        if handle_deleted is not None:
            query["handleDeletedDocuments"] = handle_deleted
        if limit is not None:
            query["limit"] = limit

        self.logger.debug("Suche in ENAIO: %s", context)
        response = await self._request(
            "POST",
            SEARCH_PATH,
            context=context,
            json={"query": query},
            headers={"accept": "application/json"},
        )

        try:
            return [EnaioDict(obj) for obj in response.json()["objects"]]
        except Exception as e:
            # Unerwartete Antwortstruktur (kein JSON, kein "objects"-Feld, ...).
            self.logger.exception("Unerwartete Antwort der ENAIO API bei %s", context)
            raise HTTPException(
                status_code=500, detail=f"An internal error occurred: {e}"
            )

    def _require_one(self, objects, kind, identifier):
        """Liefert den ersten Treffer oder wirft HTTP 404."""

        if not objects:
            self.logger.warning("%s '%s' nicht gefunden", kind, identifier)
            raise HTTPException(
                status_code=404, detail=f"{kind} '{identifier}' not found"
            )
        return objects[0]

    @staticmethod
    def _document_record(child, type_name, *, id_key, id_field, title_field):
        """Baut den einheitlichen Dokument-Datensatz aus einem Enaio-Objekt."""

        return {
            "type": type_name,
            id_key: child.property(id_field),
            "name": child.property(title_field),
            "creationDate": child.property("system:creationDate"),
            "lastModificationDate": child.property("system:lastModificationDate"),
        }

    async def _get_content(self, object_id, content_path):
        """Laedt einen Inhaltsstrom eines Objekts (Datei oder Rendition)."""

        return await self._request(
            "GET",
            f"/api/dms/objects/{object_id}/contents/{content_path}",
            context=f"Inhalt {content_path} von Objekt {object_id}",
            raise_for_status=False,
        )

    # ------------------------------------------------------------------
    # Fachliche Zugriffe
    # ------------------------------------------------------------------

    async def get_aktenzeichen(self, aktenzeichen):
        """Laedt die Metadaten eines Vorgangs.

        :returns: Tupel ``(objectId, record)``.
        """

        objects = await self._search(
            "SELECT system:objectId, Aktenbezeichnung, Kategorisierung, "
            "Aktenverantwortlicher, Aktenplaneintrag, Aktenzeichen, Akteninhalt "
            "FROM OSTPL_AA WHERE Aktenzeichen=@aktenzeichen",
            {"aktenzeichen": aktenzeichen},
            context=f"Aktenzeichen {aktenzeichen}",
        )

        akte = self._require_one(objects, "Aktenzeichen", aktenzeichen)
        record = {
            "reference_nr": akte.property("Aktenzeichen"),
            "title": akte.property("Aktenbezeichnung"),
            "category": akte.property("Kategorisierung"),
            "topics": akte.property("Aktenplaneintrag").split("|"),
            "sachbearbeiter": akte.property("Aktenverantwortlicher"),
        }

        self.logger.debug("Found Aktenzeichen %s", record)
        return (akte.property("system:objectId"), record)

    async def get_document_list(self, parent_object_id):
        """Sammelt alle Dokumente eines Vorgangs ueber alle Objekttypen hinweg."""

        documents = []

        for object_type in OBJECT_TYPES.values():
            context = f"Dokumentliste ({object_type.name}) zu {parent_object_id}"

            children = await self._search(
                "SELECT system:creationDate, system:lastModificationDate, "
                f"{object_type.id_field} AS documentIdentifier, "
                f"{object_type.title_field} AS documentTitle "
                f"FROM {object_type.table} WHERE system:SDSTA_ID IN (@objectIds)",
                {"objectIds": parent_object_id},
                context=context,
            )

            if not children:
                self.logger.debug("Keine Kinder vom Typ %s zu %s", object_type.name, parent_object_id)
                continue

            # Die Felder sind im SELECT einheitlich aliasiert, daher fuer alle
            # Objekttypen dieselben Spaltennamen.
            documents.extend(
                self._document_record(
                    child,
                    object_type.name,
                    id_key="id",
                    id_field="documentIdentifier",
                    title_field="documentTitle",
                )
                for child in children
            )

            self.logger.debug(
                "Children for ParentObjectId %s: %s", parent_object_id, children
            )

        self.logger.info(
            "Dokumentliste zu Vorgang %s: %d Dokument(e)", parent_object_id, len(documents)
        )
        return documents

    async def get_document(self, document_id, content_format):
        """Laedt ein einzelnes Dokument inklusive Inhalt.

        :param content_format: ``"file"`` fuer die Originaldatei, sonst die
            Text-Rendition.
        """

        objects = await self._search(
            "SELECT * FROM OSTPL_AA_DOKUMENT where AA_DOK_PENR=@objectId "
            "UNION SELECT * FROM OSTPL_AA_AN where system:objectId=@objectId "
            "UNION SELECT * FROM EMail where system:objectId=@objectId",
            {"objectId": document_id},
            context=f"Dokument {document_id}",
            options={
                "Rights": 0,
                "Baseparams": 1,
                "RegisterContext": 0,
                "FileInfo": 1,
            },
            limit=1,
            handle_deleted=None,
        )

        child = self._require_one(objects, "Document", document_id)
        self.logger.debug("Dokument: %s", child)

        object_type = OBJECT_TYPES[child.property("system:objectTypeId")]

        document = self._document_record(
            child,
            object_type.name,
            id_key="document_nr",
            id_field=object_type.id_field,
            title_field=object_type.title_field,
        )

        if object_type.name == "vermerk":
            document["content"] = child.property("OSTPL_AA_AN_NOTIZ")
        else:
            object_id = child.property("system:objectId")
            if content_format == "file":
                document["content"] = await self.get_file(object_id)
            else:
                document["content"] = await self.get_rendition(object_id)

        # Bewusst OHNE document["content"] loggen: der Inhalt kann Volltext
        # oder Binaerdaten (potenziell personenbezogen) enthalten.
        self.logger.info("Dokument %s geladen (Typ %s)", document_id, document["type"])

        return document

    async def get_file(self, document_id):
        response = await self._get_content(document_id, "file/1")
        return response.content

    async def get_rendition(self, document_id) -> str:
        response = await self._get_content(document_id, "renditions/text")
        if response.status_code == httpx.codes.OK:
            return standardize_text(response.text)

        self.logger.warning(
            "Keine Text-Rendition fuer Dokument %s (HTTP %s)",
            document_id,
            response.status_code,
        )
        return None

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

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

        cid_bytes = cid.encode("ascii")
        filename_header = filename.replace('"', "").encode("utf-8")

        parts = [
            # Part 1: Metadaten als JSON.
            (
                [
                    b'Content-Disposition: form-data; name="data"',
                    b"Content-Type: application/json;charset=UTF-8",
                ],
                json.dumps(data, ensure_ascii=False).encode("utf-8"),
            ),
            # Part 2: Dateiinhalt, referenziert ueber die cid.
            (
                [
                    b'Content-Disposition: form-data; name="'
                    + cid_bytes
                    + b'"; filename="'
                    + filename_header
                    + b'"',
                    b"Content-ID: " + cid_bytes,
                    b"Content-Type: " + DOCX_MIME_TYPE.encode("ascii"),
                ],
                file_bytes,
            ),
        ]

        boundary = uuid.uuid4().hex
        body = encode_multipart(parts, boundary)
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

    async def upload_document(self, reference, file_path, document_type, betreff, filename):
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
        parent_id, _ = await self.get_aktenzeichen(reference)

        file_bytes = Path(file_path).read_bytes()
        body, content_type = self._build_upload_payload(
            parent_id, file_bytes, betreff, filename
        )

        self.logger.debug(
            "Lade Dokument (%s) in Vorgang %s (Parent %s) hoch",
            document_type, reference, parent_id,
        )
        # Statuscodes werden unten selbst ausgewertet, da 422 fachlich bedeutsam ist.
        response = await self._request(
            "POST",
            "/api/dms/objects?minimalResponse=true",
            context=f"Upload in Vorgang {reference}",
            raise_for_status=False,
            content=body,
            headers={
                "accept": "application/json",
                "Content-Type": content_type,
            },
        )

        # Laut Spezifikation signalisiert 422 fehlgeschlagene Inserts.
        if response.status_code == 422:
            self.logger.warning("ENAIO hat den Upload abgelehnt (422) fuer Vorgang %s", reference)
            raise HTTPException(
                status_code=422,
                detail=f"Enaio hat den Upload abgelehnt (422): {response.text}",
            )

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            self.logger.error("Unerwartete Antwort der ENAIO API beim Upload in Vorgang %s: %s", reference, e)
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
            "Dokument in Vorgang %s hochgeladen (ObjectId %s)", reference, object_id
        )

        return {"objectId": object_id, "reference_nr": reference}
