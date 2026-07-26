import os
import re
import base64

from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

from fastmcp import FastMCP, Context
from fastmcp.tools.tool import ToolAnnotations
from typing import Annotated, List, Optional
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
        "vermerk": {"template": "Vorlage_Vermerk.docx"},
        "brief": {"template": "Vorlage_Brief.docx"},
}


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
        "Alle Tools setzen voraus, dass der "
        "Caller den Parameter SessionID mit einer Enaio SessionID uebergibt. "
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
                "Für create_case_document gilt eine Sonderregel: Dieses Tool speichert ein "
                "Dokument dauerhaft in Enaio. Standardzustand ist „kein Aufruf“. Es darf "
                "ausschließlich nach einer ausdrücklichen Speicheranweisung des Nutzers "
                "(„Speichern.“, „In Enaio speichern.“, „Zur Akte hinzufügen.“ o. Ä.) aufgerufen "
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

        Rückgabe enthält u.a. Titel, Kategorie, Sachbearbeiter sowie ein
        "documents"-Feld mit allen zugehörigen Dokumenten (inkl. Dokument-ID), die
        mit access_document_fulltext oder download_document abgerufen werden können.
        Zusätzlich liefert "dms_link" einen direkten Link, mit dem der Vorgang im
        Enaio-Web-Client geöffnet werden kann; dieser Link sollte in der Antwort
        mit angegeben werden.

        :param reference: Aktenzeichen des Vorgangs (Pflichtformat siehe oben).
        :param SessionID: Enaio SessionID des aufrufenden Clients.
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
        get_case_metadata Details und die Dokumentliste nachladen lassen, sowie
        'dms_link' einen direkten Link, mit dem der Vorgang im Enaio-Web-Client
        geöffnet werden kann; dieser Link sollte in der Antwort mit angegeben
        werden.

        :param user: Benutzerkürzel des Aktenverantwortlichen.
        :param SessionID: Enaio SessionID des aufrufenden Clients.
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
        content: Annotated[
                List[dict],
                (
                        "Liste von Inhaltsbloecken (JSON-Array), die den Dokumentkoerper bilden. "
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
                        "Beispiel: "
                        '[{"type":"heading","text":"1. Sachverhalt"},'
                        '{"type":"subheading","text":"Auflagen"},'
                        '{"type":"para","runs":[{"t":"Wichtig: ","b":true},{"t":"normaler Text."}]},'
                        '{"type":"listitem","number":1,"text":"Erster Punkt"},'
                        '{"type":"table","header":["A","B"],"rows":[["1","2"]]}]'
                ),
        ],
        SessionID: Annotated[str, SESSION_ID_DESCRIPTION],
        ctx: Context,
        betreff: Annotated[
                Optional[str],
                "Optionaler Betreff; ersetzt die Betreffzeile der Vorlage.",
        ] = None,
        fields: Annotated[
                Optional[dict],
                (
                        "Optionale Zuordnung von Platzhaltern zu Ersatztexten (JSON-Objekt). "
                        "Schluessel ist der Platzhaltername OHNE eckige Klammern, z.B. 'Aktenzeichen' "
                        "fuer den Platzhalter [Aktenzeichen] in der Vorlage; Wert ist der einzusetzende "
                        "Text. Beispiel: {\"Aktenzeichen\":\"DS.1.2-2024-1234\","
                        "\"Bearbeiter\":\"Max Mustermann\",\"Adressat\":\"Ministerium für Bildung\","
                        "\"PLZ\":\"12345\",\"Ort\":\"Musterstadt\",\"Ansprechpartner\":"
                        "\"Erika Mustermann\",\"Abteilung\":\"Bauamt\",\"Anschrift\":"
                        "\"Musterstrasse 1\"}. Haeufig genutzte Platzhalter sind z.B. "
                        "[Adressat], [PLZ], [Ort], [Ansprechpartner], [Abteilung] und [Anschrift]. "
                        "Der Platzhalter [Datum] wird stets automatisch mit dem aktuellen Datum befuellt "
                        "und kann nicht ueberschrieben werden. [Aktenzeichen] wird - sofern nicht "
                        "angegeben - aus 'reference' uebernommen. Die Angaben zum aufrufenden Benutzer "
                        "([Mail], [Name], [Username]) werden - sofern nicht angegeben - automatisch aus "
                        "den x-enaio-Headern des Aufrufs uebernommen und muessen nicht erfragt werden. "
                        "Nicht angegebene Platzhalter bleiben unveraendert im Dokument."
                ),
        ] = None,
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

        :param reference: Aktenzeichen / Vorgangsnummer.
        :param document_type: Dokumententyp (z. B. 'Vermerk', 'Brief').
        :param content: Liste von Inhaltsblöcken.
        :param SessionID: Enaio SessionID des aufrufenden Clients.
        :param betreff: Optionaler Betreff.
        :param fields: Optionale Zuordnung Platzhaltername -> Ersatztext; [Datum] immer
                aktuelles Datum, [Aktenzeichen] fällt auf reference zurück, [Mail], [Name]
                und [Username] auf die x-enaio-Header des Aufrufs.
        """

        template_name, template_path = _resolve_template(document_type)

        await ctx.info(
                f"Erzeuge {document_type}-Dokument fuer Vorgang {reference} aus Vorlage "
                f"{template_name}"
        )

        out_path, out_name = _output_path(document_type, betreff)

        # Platzhalter-Werte aufbereiten: [Aktenzeichen] faellt auf reference zurueck und die
        # Benutzerangaben ([Mail], [Name], ...) auf die x-enaio-Header, jeweils nur wenn nicht
        # ausdruecklich angegeben. [Datum] setzt fill_document stets auf das aktuelle Datum.
        doc_fields = await _document_fields(reference, fields, ctx)

        written = _render_document(template_path, content, out_path, betreff, doc_fields)

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
                "blocks": len(content),
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
        description=create_case_document_session.__doc__,
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
        content: Annotated[
                List[dict],
                (
                        "Liste von Inhaltsbloecken (JSON-Array), die den Dokumentkoerper bilden. "
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
                        "Beispiel: "
                        '[{"type":"heading","text":"1. Sachverhalt"},'
                        '{"type":"subheading","text":"Auflagen"},'
                        '{"type":"para","runs":[{"t":"Wichtig: ","b":true},{"t":"normaler Text."}]},'
                        '{"type":"listitem","number":1,"text":"Erster Punkt"},'
                        '{"type":"table","header":["A","B"],"rows":[["1","2"]]}]'
                ),
        ],
        ctx: Context,
        betreff: Annotated[
                Optional[str],
                "Optionaler Betreff; ersetzt die Betreffzeile der Vorlage.",
        ] = None,
        fields: Annotated[
                Optional[dict],
                (
                        "Optionale Zuordnung von Platzhaltern zu Ersatztexten (JSON-Objekt). "
                        "Schluessel ist der Platzhaltername OHNE eckige Klammern, z.B. 'Aktenzeichen' "
                        "fuer den Platzhalter [Aktenzeichen] in der Vorlage; Wert ist der einzusetzende "
                        "Text. Beispiel: {\"Aktenzeichen\":\"DS.1.2-2024-1234\","
                        "\"Bearbeiter\":\"Max Mustermann\",\"Adressat\":\"Ministerium für Bildung\","
                        "\"PLZ\":\"12345\",\"Ort\":\"Musterstadt\",\"Ansprechpartner\":"
                        "\"Erika Mustermann\",\"Abteilung\":\"Bauamt\",\"Anschrift\":"
                        "\"Musterstrasse 1\"}. Haeufig genutzte Platzhalter sind z.B. "
                        "[Adressat], [PLZ], [Ort], [Ansprechpartner], [Abteilung] und [Anschrift]. "
                        "Der Platzhalter [Datum] wird stets automatisch mit dem aktuellen Datum befuellt "
                        "und kann nicht ueberschrieben werden. [Aktenzeichen] wird - sofern nicht "
                        "angegeben - aus 'reference' uebernommen. Die Angaben zum aufrufenden Benutzer "
                        "([Mail], [Name], [Username]) werden - sofern nicht angegeben - automatisch aus "
                        "den x-enaio-Headern des Aufrufs uebernommen und muessen nicht erfragt werden. "
                        "Nicht angegebene Platzhalter bleiben unveraendert im Dokument."
                ),
        ] = None,
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

        :param document: Dokument-ID.
        :param SessionID: Enaio SessionID des aufrufenden Clients.
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

        :param document: Dokument-ID.
        :param SessionID: Enaio SessionID des aufrufenden Clients.
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
