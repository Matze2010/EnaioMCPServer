import json
import uuid
import httpx
import logging
import re
import unicodedata

from datetime import datetime
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

# Aktenstatus, der einen noch nicht abgeschlossenen Vorgang kennzeichnet.
RUNNING_CASE_STATUS = "laufend"

# Aktentyp, auf den die Vorgangssuchen eingeschraenkt sind.
STANDARD_CASE_TYPE = "Standardakte"

SEARCH_PATH = "/api/dms/objects/search"

# Endpunkt der Organisationsverwaltung, der alle angelegten Benutzer liefert.
# Er liegt nicht unter demselben Praefix wie die uebrigen Aufrufe und traegt das
# /osrest daher selbst im Pfad.
USERS_PATH = "/osrest/api/organization/users"

# Endpunkt der laufenden Workflow-Aktivitaeten des angemeldeten Nutzers. Er liegt
# wie der Organization-Endpunkt ausserhalb des Praefixes der DMS-Aufrufe und
# traegt das /osrest deshalb selbst im Pfad.
WORKFLOWS_RUNNING_PATH = "/osrest/api/workflows/running"

# WorkflowId des Posteingangs-Workflows. Der Endpunkt liefert die Aktivitaeten
# aller Workflows (Ad-hoc-Umlaeufe, Schlusszeichnung, ...); Posteingaenge sind
# ausschliesslich die Aktivitaeten dieses einen Workflows.
INBOX_WORKFLOW_ID = "3E41FBD0FE084633947F36C60856D510"

# Werte des Feldes "locked", die einen nicht gesperrten Benutzer kennzeichnen.
# Enaio liefert "0"/"1" als String; Zahlen und true/false werden mit abgedeckt.
UNLOCKED_VALUES = {"0", "false"}

AUTH_MODE_BASIC = "basic"
AUTH_MODE_SESSION = "session"
AUTH_MODES = {AUTH_MODE_BASIC, AUTH_MODE_SESSION}
SESSION_AUTH_FAILED_MESSAGE = (
    "Die Authentifizierung an der Enaio-API ist fehlgeschlagen. "
    "Bitte wiederholen Sie den Aufruf mit einer aktuellen SessionID."
)


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

    def __init__(self, url, auth_mode=AUTH_MODE_SESSION):

        self.backend_url = url

        if auth_mode not in AUTH_MODES:
            raise ValueError(
                f"Ungueltiger AUTH_MODE '{auth_mode}'. Erlaubt sind: basic, session."
            )
        self.auth_mode = auth_mode
        self._basic_auth = None

        self.session = httpx.AsyncClient(verify=False)

        self.logger = logging.getLogger(__name__)

    def set_auth(self, username: str, password: str):
        self._basic_auth = httpx.BasicAuth(username=username, password=password)

    def _request_kwargs(self, kwargs, session_id=None):
        """Ergaenzt die Request-Argumente um die konfigurierte Authentifizierung."""

        request_kwargs = dict(kwargs)
        if self.auth_mode == AUTH_MODE_BASIC:
            if self._basic_auth is not None:
                request_kwargs.setdefault("auth", self._basic_auth)
            return request_kwargs
        if session_id is None:
            return request_kwargs

        headers = dict(request_kwargs.get("headers") or {})
        cookie_key = next((key for key in headers if key.lower() == "cookie"), "Cookie")
        session_cookie = f"JSESSIONID={session_id}"
        if headers.get(cookie_key):
            headers[cookie_key] = f"{headers[cookie_key]}; {session_cookie}"
        else:
            headers[cookie_key] = session_cookie
        request_kwargs["headers"] = headers
        return request_kwargs

    # ------------------------------------------------------------------
    # Gemeinsame Bausteine fuer alle API-Zugriffe
    # ------------------------------------------------------------------

    async def _request(
        self,
        method,
        path,
        *,
        context,
        session_id=None,
        raise_for_status=True,
        **kwargs,
    ):
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
            request_kwargs = self._request_kwargs(kwargs, session_id)
            response = await self.session.request(
                method, self.backend_url + path, **request_kwargs
            )
            if (
                self.auth_mode == AUTH_MODE_SESSION
                and response.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN)
            ):
                self.logger.warning(
                    "Authentifizierung an der ENAIO API bei %s fehlgeschlagen (HTTP %s)",
                    context,
                    response.status_code,
                )
                raise HTTPException(
                    status_code=401,
                    detail=SESSION_AUTH_FAILED_MESSAGE,
                )
            if raise_for_status:
                response.raise_for_status()
            return response

        except HTTPException:
            raise
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
        session_id=None,
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
            session_id=session_id,
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
    def _is_sensitive(value):
        """Wertet das Feld ``Sensibel`` eines Vorgangs aus.

        Enaio liefert den Wert als String (``"0"``/``"1"``). Nur der Wert 0 gilt
        als nicht sensibel; leere oder fehlende Werte werden bewusst als sensibel
        behandelt.
        """

        return str(value).strip() != "0"

    @classmethod
    def _case_record(cls, akte):
        """Baut die gemeinsamen Vorgangsfelder aus einem OSTPL_AA-Objekt."""

        return {
            "reference_nr": akte.property("Aktenzeichen"),
            "title": akte.property("Aktenbezeichnung"),
            "category": akte.property("Kategorisierung"),
            "creationDate": akte.property("Erstelldatum"),
            "topics": akte.property("Aktenplaneintrag").split("|"),
            "restricted": cls._is_sensitive(akte.property("Sensibel")),
        }

    @staticmethod
    def _user_record(entry, email):
        """Baut den schlanken Nutzerdatensatz aus einem Organization-Eintrag."""

        return {
            "name": entry.get("name"),
            "fullname": entry.get("fullname"),
            "email": email,
            "groups": entry.get("groups") or [],
            "guid": entry.get("guid"),
            "wfguid": entry.get("wfguid"),
        }

    @staticmethod
    def _epoch_ms_to_iso(value):
        """Wandelt einen Zeitstempel in Millisekunden in einen ISO-8601-String.

        Der Workflow-Endpunkt liefert Zeitstempel als Millisekunden seit Epoch.
        Fehlende oder unbrauchbare Werte ergeben ``None``, damit ein einzelner
        kaputter Eintrag nicht die ganze Liste kippt.
        """

        try:
            return datetime.fromtimestamp(int(value) / 1000).isoformat(
                timespec="seconds"
            )
        except (TypeError, ValueError, OSError, OverflowError):
            return None

    @classmethod
    def _inbox_record(cls, entry):
        """Baut den schlanken Posteingangs-Datensatz aus einer Workflow-Aktivitaet."""

        return {
            "id": entry.get("id"),
            "name": entry.get("processName"),
            "activity": entry.get("activityName"),
            "creationDate": cls._epoch_ms_to_iso(entry.get("creationTime")),
            # Kennung des im Posteingang liegenden Dokuments. Sie heisst bewusst
            # document_id, weil sie unveraendert als Dokument-ID in get_document
            # weiterverwendet wird (access_document_fulltext, download_document).
            "document_id": entry.get("objectId"),
        }

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

    async def _caseIsAccessible(self, parent_object_id, session_id=None):
        """Prueft, ob die Inhalte eines Vorgangs gelesen werden duerfen.

        Laedt das Objekt ueber ``GET /api/dms/objects/{id}`` und akzeptiert es nur
        dann, wenn die Antwort genau dieses Objekt enthaelt und dessen Feld
        ``Sensibel`` den Wert 0 traegt. Jede andere Situation - kein Treffer,
        anderer Statuscode, unerwartete Antwortstruktur, fehlendes oder leeres
        ``Sensibel`` - gilt als nicht zugaenglich.

        :returns: ``True``, wenn der Vorgang gelesen werden darf, sonst ``False``.
        """

        context = f"Zugriffspruefung Vorgang {parent_object_id}"
        response = await self._request(
            "GET",
            f"/api/dms/objects/{parent_object_id}",
            context=context,
            session_id=session_id,
            raise_for_status=False,
            headers={"accept": "application/json"},
        )

        if response.status_code != httpx.codes.OK:
            self.logger.warning(
                "Vorgang %s nicht lesbar (HTTP %s)",
                parent_object_id,
                response.status_code,
            )
            return False

        try:
            objects = [EnaioDict(obj) for obj in response.json()["objects"]]
        except Exception:
            # Unerwartete Antwortstruktur (kein JSON, kein "objects"-Feld, ...).
            self.logger.exception("Unerwartete Antwort der ENAIO API bei %s", context)
            return False

        wanted = str(parent_object_id).strip()
        for obj in objects:
            try:
                if str(obj.property("system:objectId")).strip() != wanted:
                    continue
                sensibel = obj.property("Sensibel")
            except (KeyError, TypeError):
                continue
            return not self._is_sensitive(sensibel)

        self.logger.warning(
            "Vorgang %s in der Antwort der ENAIO API nicht enthalten", parent_object_id
        )
        return False

    async def _get_content(self, object_id, content_path, session_id=None):
        """Laedt einen Inhaltsstrom eines Objekts (Datei oder Rendition)."""

        return await self._request(
            "GET",
            f"/api/dms/objects/{object_id}/contents/{content_path}",
            context=f"Inhalt {content_path} von Objekt {object_id}",
            session_id=session_id,
            raise_for_status=False,
        )

    # ------------------------------------------------------------------
    # Fachliche Zugriffe
    # ------------------------------------------------------------------

    async def get_aktenzeichen(self, aktenzeichen, session_id=None):
        """Laedt die Metadaten eines Vorgangs.

        :returns: Tupel ``(objectId, record)``.
        """

        objects = await self._search(
            "SELECT system:objectId, Erstelldatum, Aktenbezeichnung, "
            "Kategorisierung, Aktenverantwortlicher, Aktenplaneintrag, "
            "Aktenzeichen, Aktentyp, Akteninhalt, Sensibel "
            "FROM OSTPL_AA "
            "WHERE Aktenzeichen=@aktenzeichen AND Aktentyp=@aktentyp",
            {"aktenzeichen": aktenzeichen, "aktentyp": STANDARD_CASE_TYPE},
            context=f"Aktenzeichen {aktenzeichen}",
            session_id=session_id,
        )

        akte = self._require_one(objects, "Aktenzeichen", aktenzeichen)
        record = self._case_record(akte)
        record["sachbearbeiter"] = akte.property("Aktenverantwortlicher")

        self.logger.debug("Found Aktenzeichen %s", record)
        return (akte.property("system:objectId"), record)

    async def get_running_cases(self, user, session_id=None):
        """Listet alle laufenden Vorgaenge eines Aktenverantwortlichen auf.

        Gesucht wird ueber die Bedingung ``Aktenverantwortlicher=@user AND
        Aktenstatus=@status AND Aktentyp=@aktentyp``; der Status ist fest auf
        ``laufend``, der Aktentyp fest auf ``Standardakte`` gesetzt. Der
        ``Akteninhalt`` wird bewusst nicht mitgelesen, damit die Liste auch bei
        vielen Treffern kompakt bleibt - Details liefert
        :meth:`get_aktenzeichen`.

        :param user: Benutzerkuerzel des Aktenverantwortlichen (z. B. ``"gisch"``).
            Enaio vergleicht ohne Beachtung der Gross-/Kleinschreibung.
        :returns: Liste der Vorgaenge; leer, wenn es keine Treffer gibt.
        """

        objects = await self._search(
            "SELECT system:objectId, Erstelldatum, Aktenzeichen, "
            "Aktenbezeichnung, Kategorisierung, Aktenplaneintrag, Aktenstatus, "
            "Aktentyp, Sensibel "
            "FROM OSTPL_AA "
            "WHERE Aktenverantwortlicher=@user AND Aktenstatus=@status "
            "AND Aktentyp=@aktentyp",
            {
                "user": user,
                "status": RUNNING_CASE_STATUS,
                "aktentyp": STANDARD_CASE_TYPE,
            },
            context=f"Laufende Vorgaenge von {user}",
            session_id=session_id,
        )

        cases = []
        for akte in objects:
            case = self._case_record(akte)
            case["status"] = akte.property("Aktenstatus")
            case["object_id"] = akte.property("system:objectId")
            cases.append(case)

        self.logger.info("Laufende Vorgaenge von %s: %d Treffer", user, len(cases))
        return cases

    async def get_users(self, session_id=None):
        """Listet die nutzbaren Bearbeiter/Benutzer der Organisation auf.

        Gelesen wird ``/osrest/api/organization/users``. Aus der Rohliste fallen alle
        Eintraege heraus, die keine eMail-Adresse tragen oder gesperrt sind
        (``locked`` ungleich ``0``) - das sind in der Praxis technische Konten,
        Admin-Zweitkonten und stillgelegte Sammelpostfaecher.

        :returns: Liste der Benutzer, aufsteigend nach ``name`` sortiert; leer,
            wenn kein Eintrag die Bedingungen erfuellt.
        """

        context = "Nutzerliste der Organisation"

        response = await self._request(
            "GET",
            USERS_PATH,
            context=context,
            session_id=session_id,
            headers={"accept": "application/json"},
        )

        try:
            entries = response.json()
        except Exception as e:
            self.logger.exception("Unerwartete Antwort der ENAIO API bei %s", context)
            raise HTTPException(
                status_code=500, detail=f"An internal error occurred: {e}"
            )

        if not isinstance(entries, list):
            self.logger.error(
                "Unerwartete Antwort der ENAIO API bei %s: %s statt Liste",
                context,
                type(entries).__name__,
            )
            raise HTTPException(
                status_code=500,
                detail="Unerwartete Antwort der ENAIO API: Nutzerliste ist keine Liste",
            )

        users = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue

            email = str(entry.get("email") or "").strip()
            if not email:
                continue

            locked = str(entry.get("locked") or "0").strip().lower()
            if locked not in UNLOCKED_VALUES:
                continue

            users.append(self._user_record(entry, email))

        users.sort(key=lambda user: (user["name"] or "").upper())

        self.logger.info(
            "Nutzerliste: %d von %d Eintraegen nutzbar", len(users), len(entries)
        )
        return users

    async def get_inbox(self, session_id=None):
        """Listet die offenen Posteingaenge des angemeldeten Nutzers auf.

        Gelesen wird ``/osrest/api/workflows/running?verbose=true``. Der Endpunkt
        liefert alle laufenden Workflow-Aktivitaeten des angemeldeten Nutzers.
        Uebrig bleiben davon nur die Aktivitaeten des Posteingangs-Workflows
        (``workflowId`` gleich :data:`INBOX_WORKFLOW_ID`), die noch nicht gelesen
        wurden (``read`` nicht gesetzt).

        :returns: Liste der Posteingaenge, neueste zuerst; leer, wenn kein
            Eintrag die Bedingungen erfuellt.
        """

        context = "Posteingang des angemeldeten Nutzers"

        response = await self._request(
            "GET",
            WORKFLOWS_RUNNING_PATH,
            context=context,
            session_id=session_id,
            params={"verbose": "true"},
            headers={"accept": "application/json"},
        )

        try:
            entries = response.json()
        except Exception as e:
            self.logger.exception("Unerwartete Antwort der ENAIO API bei %s", context)
            raise HTTPException(
                status_code=500, detail=f"An internal error occurred: {e}"
            )

        if not isinstance(entries, list):
            self.logger.error(
                "Unerwartete Antwort der ENAIO API bei %s: %s statt Liste",
                context,
                type(entries).__name__,
            )
            raise HTTPException(
                status_code=500,
                detail="Unerwartete Antwort der ENAIO API: Posteingang ist keine Liste",
            )

        inbox = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue

            # Bereits gelesene Eintraege sind erledigt und gehoeren nicht in die
            # Uebersicht dessen, was noch offen ist.
            if entry.get("read"):
                continue

            workflow_id = str(entry.get("workflowId") or "").strip().upper()
            if workflow_id != INBOX_WORKFLOW_ID:
                continue

            inbox.append(self._inbox_record(entry))

        # Neueste Posteingaenge zuerst; Eintraege ohne Zeitstempel ans Ende.
        inbox.sort(key=lambda item: item["creationDate"] or "", reverse=True)

        self.logger.info(
            "Posteingang: %d von %d Eintraegen offen", len(inbox), len(entries)
        )
        return inbox

    async def get_document_list(self, parent_object_id, session_id=None):
        """Sammelt alle Dokumente eines Vorgangs ueber alle Objekttypen hinweg.

        Sensible Vorgaenge werden vorab ausgeschlossen; fuer sie wird keine
        Dokumentliste ausgegeben.
        """

        if not await self._caseIsAccessible(parent_object_id, session_id=session_id):
            self.logger.warning(
                "Zugriff auf Vorgang %s verweigert (sensibel oder nicht lesbar)",
                parent_object_id,
            )
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Zugriff auf die Dokumente des Vorgangs '{parent_object_id}' "
                    "ist nicht gestattet."
                ),
            )

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
                session_id=session_id,
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

    async def get_document(self, document_id, content_format, session_id=None):
        """Laedt ein einzelnes Dokument inklusive Inhalt.

        :param content_format: ``"file"`` fuer die Originaldatei, sonst die
            Text-Rendition.
        """

        # Die uebergebene Kennung kann zweierlei sein: die Fachnummer
        # AA_DOK_PENR, wie sie get_case_metadata ueber get_document_list
        # ausgibt, oder eine system:objectId, wie sie list_inbox aus dem
        # Workflow-Endpunkt liefert. Bei EMail und OSTPL_AA_AN ist die
        # system:objectId ohnehin die nach aussen sichtbare Kennung; nur bei
        # OSTPL_AA_DOKUMENT fallen beide auseinander. Dort wurde frueher allein
        # die Fachnummer geprueft, weshalb Posteingaenge in HTTP 404 liefen.
        # Deshalb vergleicht dieser Zweig beide Spalten.
        objects = await self._search(
            "SELECT * FROM OSTPL_AA_DOKUMENT "
            "where (AA_DOK_PENR=@objectId OR system:objectId=@objectId) "
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
            session_id=session_id,
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
                document["content"] = await self.get_file(object_id, session_id=session_id)
            else:
                document["content"] = await self.get_rendition(object_id, session_id=session_id)

        # Bewusst OHNE document["content"] loggen: der Inhalt kann Volltext
        # oder Binaerdaten (potenziell personenbezogen) enthalten.
        self.logger.info("Dokument %s geladen (Typ %s)", document_id, document["type"])

        return document

    async def get_file(self, document_id, session_id=None):
        response = await self._get_content(document_id, "file/1", session_id=session_id)
        return response.content

    async def get_rendition(self, document_id, session_id=None) -> str:
        response = await self._get_content(document_id, "renditions/text", session_id=session_id)
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

    async def upload_document(
        self, reference, file_path, document_type, betreff, filename, session_id=None
    ):
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
        parent_id, _ = await self.get_aktenzeichen(reference, session_id=session_id)

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
            session_id=session_id,
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
