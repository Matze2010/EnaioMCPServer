import os
import re
import base64
import json

from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

from fastmcp import FastMCP, Context
from fastmcp.tools.tool import ToolAnnotations
from pydantic import BeforeValidator, Field
from typing import Annotated, Optional
from fastapi import HTTPException

import vorlage
from EnaioBackend import (
        AUTH_MODES,
        AUTH_MODE_BASIC,
        AUTH_MODE_SESSION,
        EnaioBackend,
        RUNNING_CASE_STATUS,
        UPLOAD_OBJECT_TYPE_ID,
)
from rate_limiter import RateLimiter, RateLimitExceeded
from logging_config import configure_logging
from middleware import (
        EnaioHeaderMiddleware,
        EnaioSessionIDMiddleware,
        RequestHeaderLoggingMiddleware,
        SESSION_ID_DESCRIPTION,
        enaio_placeholder_fields,
        get_enaio_headers,
)

load_dotenv()

# Logging prozessweit konfigurieren (Level ueber LOG_LEVEL, Default INFO).
# Auf Modulebene, damit sowohl "fastmcp run ..." als auch "python EnaioMCP.py"
# die Konfiguration erhalten.
configure_logging()

url = os.environ.get('URL', 'DEFAULT_URL')
username = os.environ.get('USERNAME', 'DEFAULT_USERNAME')
password = os.environ.get('PASSWORD', 'DEFAULT_PASSWORD')
AUTH_MODE = os.environ.get("AUTH_MODE", AUTH_MODE_SESSION).strip().lower()
if AUTH_MODE not in AUTH_MODES:
        allowed = ", ".join(sorted(AUTH_MODES))
        raise RuntimeError(f"Ungueltiger AUTH_MODE '{AUTH_MODE}'. Erlaubt sind: {allowed}.")
AUTH_TAG_BASIC = f"auth:{AUTH_MODE_BASIC}"
AUTH_TAG_SESSION = f"auth:{AUTH_MODE_SESSION}"

backend = EnaioBackend(url=url, auth_mode=AUTH_MODE)
if AUTH_MODE == AUTH_MODE_BASIC:
        backend.set_auth(username, password)

# Basis-URL des Enaio-Web-Clients (osweb) fuer anklickbare Links auf Vorgaenge.
# Ohne eigene Konfiguration wird die API-Basis-URL verwendet, da Web-Client und
# REST-API in der Regel auf demselben Host liegen.
DMS_WEB_URL = os.environ.get('DMS_WEB_URL', url)

# Basis-URL des Enaio-Office-Editors fuer Links, mit denen ein erzeugtes
# Word-Dokument direkt zur Bearbeitung geoeffnet werden kann. Ueber die
# Umgebungsvariable OFFICE_WEB_URL konfigurierbar. Ohne eigene Konfiguration
# wird die API-Basis-URL verwendet, da Office-Editor und REST-API in der Regel
# auf demselben Host liegen.
OFFICE_WEB_URL = os.environ.get('OFFICE_WEB_URL', url)


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

# Zuordnungsliste: Dokumententyp -> zugehoerige Vorlage. Die Betreffzeile wird in den
# Vorlagen einheitlich ueber den Platzhalter [Betreff] gesetzt (siehe vorlage.py). Der
# Lookup erfolgt case-insensitive ueber den Schluessel.
DOCUMENT_TEMPLATES = {
        "vermerk": {
                "document_type": "Vermerk",
                "template": "Vorlage_Vermerk.docx",
                "fields": [],
        },
        "brief": {
                "document_type": "Brief",
                "template": "Vorlage_Brief.docx",
                "fields": [
                        {
                                "name": "Adressat",
                                "description": (
                                        "Name bzw. Bezeichnung der adressierten Person, "
                                        "Stelle oder Organisation."
                                ),
                        },
                        {
                                "name": "Anschrift",
                                "description": (
                                        "Anschrift der adressierten Person, "
                                        "Stelle oder Organisation."
                                ),
                        },
{
                                "name": "PLZ",
                                "description": (
                                        "Postleitzahl der adressierten Person, "
                                        "Stelle oder Organisation."
                                ),
                        },
                        {
                                "name": "Ort",
                                "description": (
                                        "Ort der adressierten Person, "
                                        "Stelle oder Organisation."
                                ),
                        },
                        {
                                "name": "Bearbeiter",
                                "description": (
                                        "Nachname / Familienname des Verfassers / Bearbeiters des Dokuments."
                                ),
                        },
                        {
                                "name": "Durchwahl",
                                "description": (
                                        "Durchwahl des Verfassers / Bearbeiters des Dokuments."
                                ),
                        },
                        {
                                "name": "Email",
                                "description": (
                                        "E-Mail-Adresse des Verfassers / Bearbeiters des Dokuments."
                                ),
                        },
                ],
        },
}

CREATE_CASE_DOCUMENT_BETREFF_DESCRIPTION = (
        "Optionaler Betreff; ersetzt die Betreffzeile der Vorlage."
)

CREATE_CASE_DOCUMENT_CONTENT_DESCRIPTION = (
        "Muss als JSON-String uebergeben werden, also als ein String, der ein "
        "JSON-Array von Objekten enthaelt - nicht als echtes JSON-Array. Baue die "
        "Blockliste auf und uebergib sie serialisiert (json.dumps(...)). Richtig: "
        '{"content":"[{\\"type\\":\\"para\\",\\"text\\":\\"Text\\"}]"}. Falsch: '
        '{"content":[{"type":"para","text":"Text"}]}. '
        "Betreff und Aktenzeichen duerfen im Dokumentinhalt nicht wiederholt "
        "werden, weil sie bereits ueber die Parameter betreff und reference "
        "bzw. die Vorlage gesetzt werden. "
        "Das JSON-Array enthaelt die Inhaltsbloecke, die den Dokumentkoerper bilden. "
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
        "Beispiel fuer den zu uebergebenden String: "
        '"[{\\"type\\":\\"heading\\",\\"text\\":\\"1. Sachverhalt\\"},'
        '{\\"type\\":\\"subheading\\",\\"text\\":\\"Auflagen\\"},'
        '{\\"type\\":\\"para\\",\\"runs\\":[{\\"t\\":\\"Wichtig: \\",\\"b\\":true},'
        '{\\"t\\":\\"normaler Text.\\"}]},'
        '{\\"type\\":\\"listitem\\",\\"number\\":1,\\"text\\":\\"Erster Punkt\\"},'
        '{\\"type\\":\\"table\\",\\"header\\":[\\"A\\",\\"B\\"],'
        '\\"rows\\":[[\\"1\\",\\"2\\"]]}]"'
)

CREATE_CASE_DOCUMENT_FIELDS_DESCRIPTION = (
        "Optionale Zuordnung von Platzhaltern zu Ersatztexten. Muss als JSON-String "
        "uebergeben werden, also als ein String, der ein JSON-Objekt enthaelt - nicht "
        "als echtes JSON-Objekt. Baue die Zuordnung auf und uebergib sie serialisiert "
        "(json.dumps(...)). Richtig: "
        "\"{\\\"Adressat\\\":\\\"Ministerium für Bildung\\\","
        "\\\"PLZ\\\":\\\"12345\\\",\\\"Ort\\\":\\\"Musterstadt\\\"}\". Falsch: "
        "{\"Adressat\":\"Ministerium für Bildung\","
        "\"PLZ\":\"12345\",\"Ort\":\"Musterstadt\"}. "
        "Schluessel ist der Platzhaltername OHNE eckige Klammern, z.B. 'Aktenzeichen' "
        "fuer den Platzhalter [Aktenzeichen] in der Vorlage; Wert ist der einzusetzende "
        "Text. Beispiel fuer den zu uebergebenden String: "
        "\"{\\\"Aktenzeichen\\\":\\\"DS.1.2-2024-1234\\\","
        "\\\"Bearbeiter\\\":\\\"Max Mustermann\\\","
        "\\\"Adressat\\\":\\\"Ministerium für Bildung\\\","
        "\\\"PLZ\\\":\\\"12345\\\",\\\"Ort\\\":\\\"Musterstadt\\\","
        "\\\"Ansprechpartner\\\":\\\"Erika Mustermann\\\","
        "\\\"Abteilung\\\":\\\"Bauamt\\\","
        "\\\"Anschrift\\\":\\\"Musterstrasse 1\\\"}\". "
        "Haeufig genutzte Platzhalter sind z.B. "
        "[Adressat], [PLZ], [Ort], [Ansprechpartner], [Abteilung] und [Anschrift]. "
        "Pflicht vor create_case_document: Rufe zuerst get_document_fields mit "
        "dem geplanten document_type auf. Befuelle fields anschliessend anhand "
        "der dort zurueckgegebenen relevanten Platzhalter. Ohne vorherigen "
        "get_document_fields-Aufruf darf create_case_document nicht aufgerufen werden. "
        "Beim Erstellen von Entwuerfen fuer Briefe oder sonstige Schreiben sind "
        "sämtliche bekannten Angaben zum Adressaten zu uebergeben. Dies umfasst "
        "insbesondere Name, Bearbeiter, Organisation, Abteilung, Anschrift, "
        "Postleitzahl und Ort. Es duerfen ausschließlich die vom Nutzer "
        "bereitgestellten oder anderweitig verfuegbaren Angaben uebermittelt "
        "werden; fehlende Angaben sind nicht zu ergänzen und nicht zu fingieren. "
        "Der Platzhalter [Datum] wird stets automatisch mit dem aktuellen Datum befuellt "
        "und kann nicht ueberschrieben werden. [Aktenzeichen] wird - sofern nicht "
        "angegeben - aus 'reference' uebernommen. Die Angaben zum aufrufenden Benutzer "
        "([Mail], [Name], [Username]) werden - sofern nicht angegeben - automatisch aus "
        "den x-enaio-Headern des Aufrufs uebernommen und muessen nicht erfragt werden. "
        "Nicht angegebene Platzhalter bleiben unveraendert im Dokument."
)


def _parse_json_dict_string(value):
        """Akzeptiert JSON-Objekte auch dann, wenn ein Client sie als String sendet."""

        if value is None or isinstance(value, dict):
                return value

        if not isinstance(value, str):
                return value

        text = value.strip()
        if not text:
                return None

        try:
                parsed = json.loads(text)
        except json.JSONDecodeError as exc:
                raise ValueError("fields muss ein gueltiges JSON-Objekt sein.") from exc

        if not isinstance(parsed, dict):
                raise ValueError("fields muss ein JSON-Objekt sein.")

        return parsed


def _parse_json_list_string(value):
        """Akzeptiert JSON-Arrays auch dann, wenn ein Client sie als String sendet."""

        if isinstance(value, list):
                return value

        if not isinstance(value, str):
                return value

        text = value.strip()
        if not text:
                raise ValueError("content muss ein gueltiges JSON-Array sein.")

        try:
                parsed = json.loads(text)
        except json.JSONDecodeError as exc:
                raise ValueError("content muss ein gueltiges JSON-Array sein.") from exc

        if not isinstance(parsed, list):
                raise ValueError("content muss ein JSON-Array sein.")

        return parsed


def _as_json_array_string(value):
        """Nimmt content als JSON-String entgegen; echte Listen werden serialisiert."""

        if isinstance(value, list):
                return json.dumps(value, ensure_ascii=False)

        if not isinstance(value, str):
                raise ValueError(
                        "content muss als JSON-String uebergeben werden, der ein JSON-Array enthaelt."
                )

        # Frueh pruefen, damit der Client den Fehler schon bei der Parametervalidierung
        # sieht und nicht erst mitten in der Dokumenterzeugung.
        _parse_json_list_string(value)
        return value


def _as_json_dict_string(value):
        """Nimmt fields als JSON-String entgegen; echte Objekte werden serialisiert."""

        if value is None:
                return None

        if isinstance(value, dict):
                return json.dumps(value, ensure_ascii=False)

        if not isinstance(value, str):
                raise ValueError(
                        "fields muss als JSON-String uebergeben werden, der ein JSON-Objekt enthaelt."
                )

        if not value.strip():
                return None

        _parse_json_dict_string(value)
        return value


CreateCaseDocumentContent = Annotated[
        str,
        BeforeValidator(_as_json_array_string),
        Field(description=CREATE_CASE_DOCUMENT_CONTENT_DESCRIPTION),
]


CreateCaseDocumentFields = Annotated[
        Optional[str],
        BeforeValidator(_as_json_dict_string),
        Field(description=CREATE_CASE_DOCUMENT_FIELDS_DESCRIPTION),
]


def _sanitize_filename(text: str) -> str:
        """Ersetzt fuer Dateinamen problematische Zeichen durch Unterstriche."""
        return re.sub(r"[^A-Za-z0-9._-]+", "_", (text or "").strip()).strip("._") or "dokument"


def _dms_link(object_id) -> Optional[str]:
        """Baut den Link auf einen Vorgang im Enaio-Web-Client (osweb).

        Der ``state``-Parameter ist ein Zeitstempel in Millisekunden und wird beim
        Aufbau des Links jeweils neu erzeugt.

        :param object_id: ObjectID des Vorgangs (``system:objectId``).
        :returns: Link oder ``None``, wenn ObjectID oder Basis-URL fehlen.
        """

        base = (DMS_WEB_URL or "").rstrip("/")
        if not object_id or not base or base == "DEFAULT_URL":
                return None

        state = int(datetime.now().timestamp() * 1000)
        return (
                f"{base}/osweb/#/folder/{object_id}/0"
                f"?state={state}&currentId={object_id}&currentTypeId=0"
        )


def _office_edit_link(object_id) -> Optional[str]:
        """Baut den Link, ueber den ein Dokument im Enaio-Office-Editor bearbeitet wird.

        Die Objekttyp-ID ist die der Vorgangsdokumente (UPLOAD_OBJECT_TYPE_ID), unter
        der create_case_document neue Dokumente ablegt.

        :param object_id: ObjectID des Dokuments (``system:objectId``).
        :returns: Link oder ``None``, wenn ObjectID oder Basis-URL fehlen.
        """

        base = (OFFICE_WEB_URL or "").rstrip("/")
        if not object_id or not base or base == "DEFAULT_URL":
                return None

        return f"{base}/office/desktop/edit/edit/{UPLOAD_OBJECT_TYPE_ID}/{object_id}"


def _resolve_template(document_type: str):
        """Ermittelt die Vorlagendatei zu einem Dokumententyp.

        :returns: Tupel ``(vorlagenname, pfad)``.
        :raises HTTPException: 400 bei unbekanntem Typ, 404 bei fehlender Datei.
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

        template_name = mapping["template"]
        template_path = ASSETS_DIR / template_name
        if not template_path.exists():
                raise HTTPException(
                        status_code=404,
                        detail=(
                                f"Vorlage fuer Dokumententyp '{document_type}' nicht gefunden: "
                                f"{template_path}."
                        ),
                )

        return template_name, template_path


def _document_field_metadata(document_type: str) -> dict:
        """Liefert die manuell befuellbaren fields-Platzhalter eines Dokumententyps."""

        template_name, _ = _resolve_template(document_type)
        type_key = (document_type or "").strip().lower()
        mapping = DOCUMENT_TEMPLATES[type_key]

        return {
                "document_type": mapping["document_type"],
                "template": template_name,
                "fields": [dict(field) for field in mapping.get("fields", [])],
        }


def _output_path(document_type: str, betreff: Optional[str]):
        """Baut Zielpfad und Dateinamen des zu erzeugenden Dokuments.

        Schema: ``<Zeitstempel>_<Dokumententyp>[_<Betreff>].docx``.

        :returns: Tupel ``(pfad, dateiname)``.
        """

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = f"_{_sanitize_filename(betreff)}" if betreff else ""
        out_name = f"{timestamp}_{_sanitize_filename(document_type)}{suffix}.docx"
        return OUTPUT_DIR / out_name, out_name


async def _document_fields(reference: str, fields: Optional[dict], ctx: Context) -> dict:
        """Stellt die Platzhalterwerte fuer die Vorlage zusammen.

        Basis sind die uebergebenen ``fields``. Ergaenzt werden das Aktenzeichen
        aus ``reference`` sowie die Angaben zum aufrufenden Benutzer aus den
        x-enaio-Headern ([Mail], [Name], [Username], ...). Beides nur per
        ``setdefault``: ein ausdruecklich uebergebener Wert bleibt stehen.

        :param reference: Aktenzeichen des Vorgangs.
        :param fields: Optional uebergebene Platzhalterwerte.
        :param ctx: Context des Aufrufs (liefert die x-enaio-Header).
        :returns: Zuordnung Platzhaltername -> Ersatztext.
        """

        doc_fields = dict(fields or {})
        doc_fields.setdefault("Aktenzeichen", reference)

        enaio = await get_enaio_headers(ctx)
        for key, value in enaio_placeholder_fields(enaio).items():
                doc_fields.setdefault(key, value)

        return doc_fields


def _render_document(template_path, content, out_path, betreff, fields):
        """Fuellt die Vorlage und meldet Vorlagenfehler als HTTP 422."""

        try:
                return vorlage.fill_document(
                        template_path,
                        content,
                        out_path,
                        betreff=betreff,
                        fields=fields,
                )
        except (ValueError, FileNotFoundError) as e:
                raise HTTPException(status_code=422, detail=f"Fehler beim Fuellen der Vorlage: {e}")


async def _enforce_upload_rate_limit():
        """Belegt einen Upload-Slot oder lehnt mit HTTP 429 ab (kein Warten)."""

        try:
                await upload_limiter.acquire()
        except RateLimitExceeded as e:
                raise HTTPException(
                        status_code=429,
                        detail=str(e),
                        headers={"Retry-After": str(e.retry_after)},
                )


async def _discard_temp_file(path, ctx: Context):
        """Loescht die lokal erzeugte Datei; ein Fehlschlag ist nicht kritisch."""

        try:
                Path(path).unlink()
        except OSError as e:
                await ctx.info(f"Warnung: temporaere Datei {path} konnte nicht geloescht werden: {e}")


async def _load_document_content(
        document_id: str, content_format: str, session_id: str | None, ctx: Context
):
        """Laedt den Inhalt eines Dokuments in der gewuenschten Repraesentation.

        :param content_format: ``"file"`` fuer die Originaldatei (bytes), sonst
            die Text-Rendition (str).
        """

        what = "Datei" if content_format == "file" else "Textinhalt"
        await ctx.info(f"Lade {what} zum Dokument {document_id}")

        document = await backend.get_document(document_id, content_format, session_id=session_id)
        return document["content"]


AUTH_INSTRUCTIONS = (
        "Alle Tools mit Enaio-API-Zugriff setzen voraus, dass der "
        "Caller den Parameter SessionID mit einer Enaio SessionID uebergibt. "
        "Das Tool get_document_fields ist davon ausgenommen. "
        if AUTH_MODE == AUTH_MODE_SESSION
        else
        ""
)

mcp = FastMCP(
        "Enaio MCP Server",
        instructions=(
                "Dieser Server gibt Zugriff auf Vorgänge (Akten) und deren Dokumente im "
                "Dokumenten-Management-System Enaio. "
                f"{AUTH_INSTRUCTIONS}"
                "Ein Vorgang wird über sein Aktenzeichen "
                "identifiziert, z. B. 'DS.1.2-2024-1234'. Wird ein Vorgang, eine Akte, ein Fall "
                "oder ein Aktenzeichen erwähnt, ist get_case_metadata das passende Tool. "
                "Dokumente werden über ihre Dokument-ID referenziert, die man aus dem "
                "'documents'-Feld der get_case_metadata-Antwort erhält. "
                "Wird nach allen offenen bzw. laufenden Vorgängen einer Person gefragt "
                "('welche Vorgänge laufen bei mir', 'offene Akten von ...'), ist "
                "list_running_cases das passende Tool. "
                "Wird nach den vorhandenen Bearbeitern bzw. Nutzern gefragt oder ist "
                "das für list_running_cases benötigte Benutzerkürzel einer Person "
                "unbekannt, liefert list_users die Liste der Nutzer mit ihren "
                "Kürzeln, Namen und eMail-Adressen. "
                "Wird nach der eingegangenen Post gefragt ('was liegt in meinem "
                "Posteingang', 'habe ich neue Post', 'was muss ich noch bearbeiten'), "
                "ist list_inbox das passende Tool; es liefert die noch nicht "
                "gelesenen Posteingänge des angemeldeten Nutzers. "
                "Für create_case_document gilt eine Sonderregel: Dieses Tool speichert ein "
                "Dokument dauerhaft in Enaio. Standardzustand ist „kein Aufruf“. Es darf "
                "ausschließlich nach einer ausdrücklichen Speicheranweisung des Nutzers "
                "(„Speichern.“, „In Enaio speichern.“, „Zur Akte hinzufügen.“ o. Ä.) aufgerufen "
                "werden. Zusätzlich muss unmittelbar vor create_case_document immer "
                "get_document_fields mit dem geplanten document_type aufgerufen werden; "
                "ohne diesen vorherigen Aufruf darf create_case_document nicht aufgerufen "
                "werden. Arbeitsaufträge zum Inhalt („erstelle ...“, „formuliere ...“, "
                "„überarbeite ...“, „erstelle die Endfassung“) sowie Zustimmung zu einem Entwurf "
                "(„das passt“, „sieht gut aus“) sind keine Speicheranweisung; der Entwurf wird "
                "dann nur im Chat ausgegeben. Im Zweifel: kein Aufruf. Die vollständige "
                "Zulässigkeitsprüfung steht in der Beschreibung des Tools und ist vor jedem "
                "Aufruf durchzuführen."
        ),
)

# Stellt die x-enaio-Header des eingehenden HTTP-Requests allen Tools und
# Resources ueber get_enaio_headers(ctx) als Dict zur Verfuegung.
mcp.add_middleware(EnaioHeaderMiddleware())

# Erzwingt im Session-Modus fuer jeden sichtbaren Tool-Aufruf eine nicht-leere
# Enaio SessionID. Im Basic-Modus existiert dieser Aufrufparameter nicht.
if AUTH_MODE == AUTH_MODE_SESSION:
        mcp.add_middleware(EnaioSessionIDMiddleware())

# Protokolliert bei jedem Tool-Aufruf die Header des eingehenden HTTP-Requests.
# mcp.add_middleware(RequestHeaderLoggingMiddleware())


@mcp.tool(
        name="get_case_metadata",
        version=AUTH_MODE_SESSION,
        tags={AUTH_TAG_SESSION},
        annotations=ToolAnnotations(
                title="Vorgang / Akte abrufen",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
        ),
)
async def get_case_metadata_session(
        reference: Annotated[
                str,
                "Aktenzeichen des Vorgangs, z. B. 'DS.1.2-2024-1234' "
                "(Format: DS.<Zahl>.<Zahl>-<Jahr>-<laufende Nummer>).",
        ],
        SessionID: Annotated[str, SESSION_ID_DESCRIPTION],
        ctx: Context,
) -> dict:
        """
        Ruft Metadaten und die Dokumentliste zu einem Vorgang (auch: Akte, Fall,
        Vorgangsakte) aus dem Enaio-Dokumenten-Management-System ab.

        Nutze dieses Tool, wenn nach einem konkreten Vorgang, einer Akte oder einem
        Aktenzeichen gefragt wird - auch wenn nur eine Vorgangs- oder Fallnummer im
        Text steht, ohne dass explizit das Wort "Aktenzeichen" fällt.

        Rückgabe enthält u.a. Titel, Kategorie, Sachbearbeiter, das Erstelldatum
        des Vorgangs ('creationDate') sowie ein
        "documents"-Feld mit allen zugehörigen Dokumenten (inkl. Dokument-ID), die
        mit access_document_fulltext oder download_document abgerufen werden können.
        Zusätzlich liefert "dms_link" einen direkten Link, mit dem der Vorgang im
        Enaio-Web-Client geöffnet werden kann; dieser Link sollte in der Antwort
        mit angegeben werden.
        """

        await ctx.info("Suche nach Vorgangsinformationen in ENAIO")
        akte, record = await backend.get_aktenzeichen(reference, session_id=SessionID)

        await ctx.info(f"Lade Liste aller Dokumente zum Vorgang {reference} ({akte})")
        record["documents"] = await backend.get_document_list(akte, session_id=SessionID)

        record["object_id"] = akte
        link = _dms_link(akte)
        if link:
                record["dms_link"] = link

        return record


@mcp.tool(
        name="get_case_metadata",
        version=AUTH_MODE_BASIC,
        tags={AUTH_TAG_BASIC},
        description=get_case_metadata_session.__doc__,
        annotations=ToolAnnotations(
                title="Vorgang / Akte abrufen",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
        ),
)
async def get_case_metadata_basic(
        reference: Annotated[
                str,
                "Aktenzeichen des Vorgangs, z. B. 'DS.1.2-2024-1234' "
                "(Format: DS.<Zahl>.<Zahl>-<Jahr>-<laufende Nummer>).",
        ],
        ctx: Context,
) -> dict:
        return await get_case_metadata_session(reference, None, ctx)


@mcp.tool(
        name="list_running_cases",
        version=AUTH_MODE_SESSION,
        tags={AUTH_TAG_SESSION},
        annotations=ToolAnnotations(
                title="Laufende Vorgänge auflisten",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
        ),
)
async def list_running_cases_session(
        user: Annotated[
                str,
                "Benutzerkürzel des Aktenverantwortlichen, z. B. 'gisch'. "
                "Groß-/Kleinschreibung spielt keine Rolle.",
        ],
        SessionID: Annotated[str, SESSION_ID_DESCRIPTION],
        ctx: Context,
) -> dict:
        """
        Listet alle laufenden (offenen, noch nicht abgeschlossenen) Vorgänge auf,
        für die der angegebene Benutzer als Aktenverantwortlicher eingetragen ist.

        Nutze dieses Tool, wenn nach einer Übersicht gefragt wird - etwa "welche
        Vorgänge laufen bei mir", "meine offenen Akten" oder "woran arbeitet
        Person X gerade" - also immer dann, wenn noch kein konkretes Aktenzeichen
        bekannt ist.

        Zurückgegeben wird eine kompakte Liste ohne Akteninhalt. Zu jedem Treffer
        liefert 'reference_nr' das Aktenzeichen, mit dem sich über
        get_case_metadata Details und die Dokumentliste nachladen lassen,
        'creationDate' das Erstelldatum des Vorgangs sowie
        'dms_link' einen direkten Link, mit dem der Vorgang im Enaio-Web-Client
        geöffnet werden kann; dieser Link sollte in der Antwort mit angegeben
        werden.
        """

        await ctx.info(f"Suche laufende Vorgänge von {user} in ENAIO")
        cases = await backend.get_running_cases(user, session_id=SessionID)

        for case in cases:
                link = _dms_link(case.get("object_id"))
                if link:
                        case["dms_link"] = link

        return {
                "user": user,
                "status": RUNNING_CASE_STATUS,
                "count": len(cases),
                "cases": cases,
        }


@mcp.tool(
        name="list_running_cases",
        version=AUTH_MODE_BASIC,
        tags={AUTH_TAG_BASIC},
        description=list_running_cases_session.__doc__,
        annotations=ToolAnnotations(
                title="Laufende Vorgänge auflisten",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
        ),
)
async def list_running_cases_basic(
        user: Annotated[
                str,
                "Benutzerkürzel des Aktenverantwortlichen, z. B. 'gisch'. "
                "Groß-/Kleinschreibung spielt keine Rolle.",
        ],
        ctx: Context,
) -> dict:
        return await list_running_cases_session(user, None, ctx)


@mcp.tool(
        name="list_users",
        version=AUTH_MODE_SESSION,
        tags={AUTH_TAG_SESSION},
        annotations=ToolAnnotations(
                title="Bearbeiter / Nutzer auflisten",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
        ),
)
async def list_users_session(
        SessionID: Annotated[str, SESSION_ID_DESCRIPTION],
        ctx: Context,
) -> dict:
        """
        Listet alle Bearbeiter (Nutzer, Sachbearbeiter, Kolleginnen und Kollegen)
        auf, die im Vorgangsbearbeitungssystem Enaio angelegt sind.

        Nutze dieses Tool, wenn nach den vorhandenen Personen gefragt wird - etwa
        "wer arbeitet hier", "welche Bearbeiter gibt es", "wie ist die
        eMail-Adresse von ..." - oder wenn für list_running_cases das
        Benutzerkürzel einer Person gebraucht wird, aber nicht bekannt ist.

        Zu jedem Eintrag liefert 'name' das Benutzerkürzel (z. B. 'GISCH'), das
        bei list_running_cases unverändert als Parameter 'user' eingesetzt werden
        kann, dazu 'fullname' den Nachnamen bzw. angezeigten Namen, 'email' die
        dienstliche eMail-Adresse, 'groups' die Gruppen- und
        Referatszugehörigkeiten sowie 'guid' und 'wfguid' die technischen
        Kennungen des Benutzers.

        Die Liste ist nach 'name' sortiert und bewusst gefiltert: gesperrte Konten
        und technische Konten ohne eMail-Adresse (Dienst-, Administrations- und
        Sammelkonten) sind nicht enthalten. Sie enthält also nur Konten, hinter
        denen eine erreichbare Person steht.
        """

        await ctx.info("Lade Liste der Bearbeiter/Nutzer aus ENAIO")
        users = await backend.get_users(session_id=SessionID)

        return {
                "count": len(users),
                "users": users,
        }


@mcp.tool(
        name="list_users",
        version=AUTH_MODE_BASIC,
        tags={AUTH_TAG_BASIC},
        description=list_users_session.__doc__,
        annotations=ToolAnnotations(
                title="Bearbeiter / Nutzer auflisten",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
        ),
)
async def list_users_basic(
        ctx: Context,
) -> dict:
        return await list_users_session(None, ctx)


@mcp.tool(
        name="list_inbox",
        version=AUTH_MODE_SESSION,
        tags={AUTH_TAG_SESSION},
        annotations=ToolAnnotations(
                title="Posteingang auflisten",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
        ),
)
async def list_inbox_session(
        SessionID: Annotated[str, SESSION_ID_DESCRIPTION],
        ctx: Context,
) -> dict:
        """
        Listet die offenen Posteingänge des angemeldeten Nutzers auf - also die
        Post, die im Vorgangsbearbeitungssystem Enaio zur Bearbeitung im Postkorb
        liegt und noch nicht gelesen wurde.

        Nutze dieses Tool, wenn nach der eingegangenen Post gefragt wird - etwa
        "was liegt in meinem Posteingang", "habe ich neue Post", "was muss ich
        noch bearbeiten" oder "was ist reingekommen". Geht es dagegen um die
        eigenen laufenden Akten einer Person, ist list_running_cases das
        passende Tool.

        Zu jedem Eintrag liefert 'name' die Bezeichnung des Posteingangs
        (z. B. 'Posteingang 24298'), 'activity' den anstehenden Arbeitsschritt
        (z. B. 'Bearbeiten', 'Kenntnisnahme') und 'creationDate' den Zeitpunkt
        des Eingangs. 'process_id', 'activity_id' und 'object_id' sind
        technische Kennungen des Eintrags.

        Die Liste ist nach Eingangszeitpunkt sortiert (neueste zuerst) und
        bewusst gefiltert: Bereits gelesene Einträge und Aktivitäten anderer
        Workflows (Ad-hoc-Umläufe, Schlusszeichnung, ...) sind nicht enthalten.
        Sie bezieht sich immer auf den angemeldeten Nutzer und lässt sich nicht
        auf eine andere Person umstellen.
        """

        await ctx.info("Lade Posteingang aus ENAIO")
        inbox = await backend.get_inbox(session_id=SessionID)

        return {
                "count": len(inbox),
                "inbox": inbox,
        }


@mcp.tool(
        name="list_inbox",
        version=AUTH_MODE_BASIC,
        tags={AUTH_TAG_BASIC},
        description=list_inbox_session.__doc__,
        annotations=ToolAnnotations(
                title="Posteingang auflisten",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
        ),
)
async def list_inbox_basic(
        ctx: Context,
) -> dict:
        return await list_inbox_session(None, ctx)


@mcp.tool(
        name="get_document_fields",
        version=AUTH_MODE,
        tags={AUTH_TAG_SESSION if AUTH_MODE == AUTH_MODE_SESSION else AUTH_TAG_BASIC},
        annotations=ToolAnnotations(
                title="Optionale Dokumentfelder abrufen",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
        ),
)
async def get_document_fields(
        document_type: Annotated[
                str,
                "Dokumententyp, dessen manuell befuellbare fields-Platzhalter "
                "abgerufen werden, z. B. 'Vermerk' oder 'Brief'.",
        ],
) -> dict:
        """
        Gibt fuer einen Dokumententyp zurueck, welche Platzhalter optional ueber
        den Parameter fields von create_case_document befuellt werden koennen.

        Die Rueckgabe enthaelt je Feld den Platzhalternamen ohne eckige Klammern
        sowie eine Beschreibung des erwarteten Inhalts. Technische Platzhalter
        wie [Body] und [Betreff] sowie automatisch befuellte Platzhalter wie
        [Datum], [Aktenzeichen] und Angaben aus x-enaio-Headern werden nicht
        gelistet.
        """

        return _document_field_metadata(document_type)


# destructiveHint=True: Der Aufruf legt ein Dokument dauerhaft im Enaio-Vorgang ab und ist
# nicht zurueckzunehmen. Clients, die auf diesen Hinweis reagieren, holen dann vor dem
# Aufruf eine ausdrueckliche Bestaetigung des Nutzers ein - zusaetzlich zur Aufrufregel in
# der Tool-Beschreibung.
@mcp.tool(
        name="create_case_document",
        version=AUTH_MODE_SESSION,
        tags={AUTH_TAG_SESSION},
        annotations=ToolAnnotations(
                title="Dokument erzeugen und in Enaio speichern",
                readOnlyHint=False,
                destructiveHint=True,
                idempotentHint=False,
                openWorldHint=False,
        ),
)
async def create_case_document_session(
        reference: Annotated[
                str,
                "Aktenzeichen / Vorgangsnummer des Vorgangs, z. B. 'DS.1.2-2024-1234', "
                "zu dem das Dokument erstellt wird.",
        ],
        document_type: Annotated[
                str,
                "Dokumententyp, der die zu verwendende Vorlage bestimmt, z. B. 'Vermerk' oder 'Brief'.",
        ],
        content: CreateCaseDocumentContent,
        SessionID: Annotated[str, SESSION_ID_DESCRIPTION],
        ctx: Context,
        betreff: Annotated[
                Optional[str],
                Field(description=CREATE_CASE_DOCUMENT_BETREFF_DESCRIPTION),
        ] = None,
        fields: CreateCaseDocumentFields = None,
) -> dict:
        """
        Erzeugt ein Word-Dokument (.docx) für einen Vorgang, indem eine zum
        Dokumententyp passende Hausvorlage mit den übergebenen Inhalten befüllt wird,
        und speichert es anschließend dauerhaft im Enaio-Vorgang.

        KERNREGEL — STANDARDZUSTAND: VERBOTEN

        Der Aufruf dieses Tools ist grundsätzlich untersagt. Zulässig ist er
        ausschließlich nach einer ausdrücklichen Speicheranweisung des Nutzers. Die
        vollständige Zulässigkeitsprüfung steht am Ende dieser Beschreibung und ist
        vor jedem beabsichtigten Aufruf durchzuführen. Im Zweifel: kein Aufruf.

        Anhand von document_type wird über eine Zuordnungsliste die passende
        .docx-Vorlage ausgewählt und mit den Blöcken aus content sowie - falls
        angegeben - dem betreff gefüllt. Briefkopf, Logo und Fußzeile der Vorlage
        bleiben erhalten.

        PARAMETERFORMAT

        content und fields werden als JSON-String übergeben, nicht als echtes
        JSON-Array bzw. JSON-Objekt. Erzeuge die Struktur und serialisiere sie
        (json.dumps), bevor du sie übergibst.

        content = "[{\\"type\\":\\"para\\",\\"text\\":\\"Absatztext\\"}]"
        fields  = "{\\"Adressat\\":\\"Ministerium für Bildung\\",\\"Ort\\":\\"Musterstadt\\"}"

        OPTIONALE PARAMETER

        betreff ist ein optionaler Betreff und ersetzt die Betreffzeile der
        Vorlage.

        fields ist ein optionaler JSON-String, der ein JSON-Objekt mit
        Platzhalterwerten enthält. Die Schlüssel werden ohne eckige Klammern
        angegeben, z. B. "Adressat", "PLZ", "Ort", "Ansprechpartner",
        "Abteilung" oder "Anschrift".

        Das erzeugte Dokument wird lokal gespeichert und anschließend über die
        Enaio-API in den zugehörigen Vorgang (reference) hochgeladen. Ein
        RateLimiter begrenzt die Uploads auf UPLOAD_RATE_LIMIT_PER_MINUTE pro
        Minute; wird das Limit überschritten, wird der Upload mit HTTP 429
        abgelehnt (das lokal erzeugte Dokument bleibt erhalten).

        Die Rückgabe enthält mit "edit_link" einen direkten Link, mit dem das neu
        erzeugte Word-Dokument sofort zur Bearbeitung geöffnet werden kann; dieser
        Link sollte in der Antwort mit angegeben werden.

        ================================================================
        SICHERHEITSREGEL FÜR DIE VERWENDUNG DIESES TOOLS
        ================================================================

        SICHERHEITSREGEL

        Standardzustand (Default): VERBOTEN.

        Der Aufruf des Tools create_case_document ist grundsätzlich untersagt.

        Eine Verwendung ist ausschließlich zulässig, wenn alle nachfolgenden
        Voraussetzungen erfüllt sind.

        VERBINDLICHE VORPRÜFUNG

        Vor jedem beabsichtigten Aufruf von create_case_document musst du die
        nachfolgende Zulässigkeitsprüfung vollständig durchführen.

        Ergibt die Prüfung, dass auch nur eine Voraussetzung nicht erfüllt ist oder
        Zweifel bestehen, ist der Toolaufruf unzulässig und muss unterbleiben.

        Es gibt keine Ausnahmen.

        Insbesondere dürfen weder Eigeninitiative noch Wahrscheinlichkeitsannahmen
        oder übliche Arbeitsabläufe die nachfolgenden Voraussetzungen ersetzen.

        Unmittelbar vor dem Aufruf von create_case_document muss get_document_fields 
        mit dem geplanten document_type aufgerufen werden. Die Rueckgabe von
        get_document_fields ist zu verwenden, um die fuer diesen document_type
        relevanten Platzhalter zu ermitteln und fields zu befuellen. Ohne diesen
        vorherigen get_document_fields-Aufruf darf create_case_document nicht
        aufgerufen werden.

        ZULÄSSIGKEITSPRÜFUNG

        Vor jedem Toolaufruf beantworte intern die folgenden Fragen in dieser
        Reihenfolge:

        Frage 1
        Hat der Nutzer ausdrücklich verlangt, dass das fertige Dokument in Enaio
        gespeichert oder zur Akte hinzugefügt werden soll?
        - Ja     -> weiter mit Frage 2.
        - Nein   -> Tool darf nicht verwendet werden.
        - Unklar -> Tool darf nicht verwendet werden.

        Frage 2
        Bezieht sich diese Aufforderung eindeutig auf das Speichern des bereits
        erstellten Dokuments und nicht lediglich auf dessen Erstellung oder
        Überarbeitung?
        - Ja     -> weiter mit Frage 3.
        - Nein   -> Tool darf nicht verwendet werden.
        - Unklar -> Tool darf nicht verwendet werden.

        Frage 3
        Ist das Dokument inhaltlich fertig oder hat der Nutzer ausdrücklich
        angeordnet, den aktuellen Stand zu speichern?
        - Ja                -> Tool darf verwendet werden.
        - Nein oder unklar  -> Tool darf nicht verwendet werden.

        WAS ALS AUSDRÜCKLICHE SPEICHERANWEISUNG GILT

        Eine ausdrückliche Speicheranweisung liegt beispielsweise bei folgenden
        Formulierungen vor:
        - „Speichern.“
        - „Bitte speichern.“
        - „Jetzt speichern.“
        - „In Enaio speichern.“
        - „Im Vorgang speichern.“
        - „Zur Akte hinzufügen.“
        - „In der Akte ablegen.“
        - „Zum Vorgang hinzufügen.“
        - „Nach Enaio übernehmen.“
        - „Diesen Entwurf jetzt speichern.“

        Nur vergleichbar eindeutige Formulierungen berechtigen zur Verwendung des
        Tools.

        WAS AUSDRÜCKLICH KEINE SPEICHERANWEISUNG IST

        Die folgenden oder vergleichbare Formulierungen beziehen sich ausschließlich
        auf die Erstellung oder Bearbeitung des Inhalts und dürfen niemals den Aufruf
        von create_case_document auslösen:
        - Erstelle ...
        - Erzeuge ...
        - Entwerfe ...
        - Formuliere ...
        - Schreibe ...
        - Verfasse ...
        - Generiere ...
        - Überarbeite ...
        - Verbessere ...
        - Passe an ...
        - Ergänze ...
        - Kürze ...
        - Erweitere ...
        - Optimiere ...
        - Prüfe ...
        - Bewerte ...
        - Analysiere ...
        - Fasse zusammen ...
        - Zeige ...
        - Gib aus ...
        - Erstelle einen Bescheid ...
        - Erstelle einen Vermerk ...
        - Erstelle ein Schreiben ...
        - Erstelle eine Stellungnahme ...

        Auch folgende Formulierungen stellen keine Speicheranweisung dar:
        - „Das passt.“
        - „Einverstanden.“
        - „Sieht gut aus.“
        - „Finalisiere den Entwurf.“
        - „Erstelle die Endfassung.“
        - „Fertige die endgültige Version an.“
        - „Das ist die finale Version.“

        Diese Aussagen beziehen sich ausschließlich auf den Inhalt des Dokuments und
        nicht auf dessen Speicherung.

        VERBOT IMPLIZITER ANNAHMEN

        Es ist untersagt,
        - aus dem Gesprächskontext,
        - aus früheren Gesprächen,
        - aus üblichen Arbeitsabläufen,
        - aus vermuteten Absichten des Nutzers,
        - aus Gewohnheit,
        - aus dem Umstand, dass ein Dokument fertig erscheint,
        - aus dem Wunsch nach einer Endfassung oder
        - aus eigener Initiative
        auf einen Speicherwunsch zu schließen.

        Ein Speicherwunsch darf ausschließlich aus einer ausdrücklichen sprachlichen
        Anweisung des Nutzers abgeleitet werden.

        VERHALTEN BEI UNSICHERHEIT

        Besteht auch nur der geringste Zweifel, ob der Nutzer eine Speicherung
        wünscht, gilt ausnahmslos:
        - create_case_document wird nicht aufgerufen.
        - Der Entwurf wird ausschließlich im Chat ausgegeben.
        - Es wird auf eine ausdrückliche Speicheranweisung gewartet.

        VORRANGREGEL

        Diese Regeln haben Vorrang vor allen anderen allgemeinen Anweisungen zur
        Werkzeugverwendung. Insbesondere gilt:
        - Ein fertiger Entwurf ist nicht automatisch zu speichern.
        - Ein vom Nutzer akzeptierter Entwurf ist nicht automatisch zu speichern.
        - Eine Endfassung ist nicht automatisch zu speichern.
        - Ein Dokument darf niemals proaktiv oder vorsorglich gespeichert werden.
        - Die Verwendung von create_case_document ist ausschließlich nach einer
          ausdrücklichen Speicheranweisung zulässig.

        FAIL-CLOSED-REGEL

        Im Zweifel gilt immer die sicherste Alternative:
        - Zweifel -> kein Toolaufruf.
        - Keine ausdrückliche Speicheranweisung -> kein Toolaufruf.
        - Ausdrückliche Speicheranweisung -> Toolaufruf zulässig.

        Das Verhalten ist grundsätzlich fail closed: Ist die Zulässigkeit des
        Toolaufrufs nicht zweifelsfrei gegeben, ist der Toolaufruf verboten.

        MERKSATZ

        Standard: Kein Toolaufruf.
        Ausnahme: Nur nach einer ausdrücklichen Anweisung des Nutzers, das Dokument
        in Enaio zu speichern.
        """

        # content und fields kommen als JSON-String herein; die Parse-Helfer reichen
        # bereits fertige Listen/Dicts unveraendert durch (direkte Python-Aufrufe).
        content_blocks = _parse_json_list_string(content)
        field_values = _parse_json_dict_string(fields)

        template_name, template_path = _resolve_template(document_type)

        await ctx.info(
                f"Erzeuge {document_type}-Dokument fuer Vorgang {reference} aus Vorlage "
                f"{template_name}"
        )

        out_path, out_name = _output_path(document_type, betreff)

        # Platzhalter-Werte aufbereiten: [Aktenzeichen] faellt auf reference zurueck und die
        # Benutzerangaben ([Mail], [Name], ...) auf die x-enaio-Header, jeweils nur wenn nicht
        # ausdruecklich angegeben. [Datum] setzt fill_document stets auf das aktuelle Datum.
        doc_fields = await _document_fields(reference, field_values, ctx)

        written = _render_document(template_path, content_blocks, out_path, betreff, doc_fields)

        await ctx.info(f"Dokument lokal gespeichert unter {written}")

        # Rate-Limit pruefen, bevor tatsaechlich hochgeladen wird: ein belegter
        # Slot entspricht damit einem echten Upload-Versuch.
        await _enforce_upload_rate_limit()

        await ctx.info(f"Lade Dokument in Vorgang {reference} nach Enaio hoch")
        upload = await backend.upload_document(
                reference,
                written,
                document_type,
                betreff,
                out_name,
                session_id=SessionID,
        )

        await _discard_temp_file(written, ctx)

        object_id = upload.get("objectId")
        result = {
                "reference_nr": reference,
                "document_type": document_type,
                "betreff": betreff,
                "template": template_name,
                "blocks": len(content_blocks),
                "stored_in_enaio": True,
                "enaio_object_id": object_id,
        }

        # Link zum direkten Bearbeiten; faellt weg, wenn Enaio keine ObjectID liefert.
        edit_link = _office_edit_link(object_id)
        if edit_link:
                result["edit_link"] = edit_link

        return result


@mcp.tool(
        name="create_case_document",
        version=AUTH_MODE_BASIC,
        tags={AUTH_TAG_BASIC},
        description=create_case_document_session.__doc__.split("\n        :param", 1)[0],
        annotations=ToolAnnotations(
                title="Dokument erzeugen und in Enaio speichern",
                readOnlyHint=False,
                destructiveHint=True,
                idempotentHint=False,
                openWorldHint=False,
        ),
)
async def create_case_document_basic(
        reference: Annotated[
                str,
                "Aktenzeichen / Vorgangsnummer des Vorgangs, z. B. 'DS.1.2-2024-1234', "
                "zu dem das Dokument erstellt wird.",
        ],
        document_type: Annotated[
                str,
                "Dokumententyp, der die zu verwendende Vorlage bestimmt, z. B. 'Vermerk' oder 'Brief'.",
        ],
        content: CreateCaseDocumentContent,
        ctx: Context,
        betreff: Annotated[
                Optional[str],
                Field(description=CREATE_CASE_DOCUMENT_BETREFF_DESCRIPTION),
        ] = None,
        fields: CreateCaseDocumentFields = None,
) -> dict:
        return await create_case_document_session(
                reference,
                document_type,
                content,
                None,
                ctx,
                betreff=betreff,
                fields=fields,
        )


@mcp.tool(
        name="access_document_fulltext",
        version=AUTH_MODE_SESSION,
        tags={AUTH_TAG_SESSION},
        annotations=ToolAnnotations(
                title="Dokumentinhalt als Text lesen",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
        ),
)
async def access_document_fulltext_session(
        document: Annotated[str, "Dokument-ID, z. B. aus dem 'documents'-Feld von get_case_metadata."],
        SessionID: Annotated[str, SESSION_ID_DESCRIPTION],
        ctx: Context,
) -> str:
        """
        Liest den Volltext eines Dokuments aus einem Vorgang als Klartext, z. B. um
        Inhalte zu zitieren, zusammenzufassen oder im Gespräch zu durchsuchen.

        Verwende dieses Tool (nicht download_document), wenn der Inhalt gelesen,
        zitiert oder ausgewertet werden soll. Wenn stattdessen die Originaldatei
        (z. B. als Anhang oder zum Download) benötigt wird, nutze download_document.
        """

        return await _load_document_content(document, "text", SessionID, ctx)


@mcp.tool(
        name="access_document_fulltext",
        version=AUTH_MODE_BASIC,
        tags={AUTH_TAG_BASIC},
        description=access_document_fulltext_session.__doc__,
        annotations=ToolAnnotations(
                title="Dokumentinhalt als Text lesen",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
        ),
)
async def access_document_fulltext_basic(
        document: Annotated[str, "Dokument-ID, z. B. aus dem 'documents'-Feld von get_case_metadata."],
        ctx: Context,
) -> str:
        return await access_document_fulltext_session(document, None, ctx)


@mcp.tool(
        name="download_document",
        version=AUTH_MODE_SESSION,
        tags={AUTH_TAG_SESSION},
        annotations=ToolAnnotations(
                title="Dokument als Datei herunterladen",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
        ),
)
async def download_document_session(
        document: Annotated[str, "Dokument-ID, z. B. aus dem 'documents'-Feld von get_case_metadata."],
        SessionID: Annotated[str, SESSION_ID_DESCRIPTION],
        ctx: Context,
) -> str:
        """
        Lädt ein Dokument als Originaldatei herunter (Base64-kodierter Binärinhalt),
        z. B. um es weiterzuleiten oder als Anhang bereitzustellen.

        Verwende dieses Tool (nicht access_document_fulltext), wenn die Originaldatei
        selbst benötigt wird, nicht nur ihr Textinhalt.
        """

        content = await _load_document_content(document, "file", SessionID, ctx)

        return base64.b64encode(content).decode("ascii")


@mcp.tool(
        name="download_document",
        version=AUTH_MODE_BASIC,
        tags={AUTH_TAG_BASIC},
        description=download_document_session.__doc__,
        annotations=ToolAnnotations(
                title="Dokument als Datei herunterladen",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
        ),
)
async def download_document_basic(
        document: Annotated[str, "Dokument-ID, z. B. aus dem 'documents'-Feld von get_case_metadata."],
        ctx: Context,
) -> str:
        return await download_document_session(document, None, ctx)


@mcp.resource("document://{document}/fulltext", tags={AUTH_TAG_BASIC})
async def resource_access_document_fulltext(document: str, ctx: Context) -> str:
        """
        Access documents fulltext. The document's content is provided as text representation.
        :param document: Dokument-ID
        """

        return await _load_document_content(document, "text", None, ctx)


@mcp.resource("document://{document}/file", tags={AUTH_TAG_BASIC})
async def resource_download_document(document: str, ctx: Context) -> bytes:
        """
        Access document and download as file. The document's content is provided as binary representation.
        :param document: Dokument-ID
        """

        return await _load_document_content(document, "file", None, ctx)


mcp.enable(
        tags={f"auth:{AUTH_MODE}"},
        components={"tool", "resource", "template"},
        only=True,
)


if __name__ == "__main__":
    mcp.run(transport="stdio")
