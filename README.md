# Enaio MCP Server

MCP-Server ([Model Context Protocol](https://modelcontextprotocol.io/)), der
Vorgänge (Akten) und deren Dokumente aus dem Dokumenten-Management-System
**Enaio** für einen MCP-Client verfügbar macht. Ein Vorgang wird über sein
Aktenzeichen identifiziert (Format `DS.<Zahl>.<Zahl>-<Jahr>-<lfd. Nummer>`,
z. B. `DS.1.2-2024-1234`).

Der Server baut auf [FastMCP](https://gofastmcp.com/) auf und spricht die
Enaio-REST-API (`/api/dms/...`) wahlweise mit SessionID-Cookie oder Basic Auth
an. Standard ist die SessionID-Authentifizierung.

## Funktionsumfang

### Tools

Die sichtbaren Tool-Schemas haengen vom `AUTH_MODE` ab:

| AuthMode | Tools | Resources |
|----------|-------|-----------|
| `session` | Alle Tools verlangen den Pflichtparameter `SessionID`; der Server gibt ihn bei jedem Enaio-API-Aufruf als Cookie `JSESSIONID` weiter. | Nicht verfuegbar |
| `basic` | Dieselben Toolnamen sind ohne `SessionID` verfuegbar; der Server nutzt `USERNAME`/`PASSWORD` fuer Basic Auth. | Verfuegbar ohne `SessionID` in der URI |

Schlaegt die Authentifizierung im Session-Modus fehl, weist die Antwort darauf
hin, den Aufruf mit einer aktuellen SessionID zu wiederholen.

| Tool | Zweck |
|------|-------|
| `get_case_metadata` | Metadaten und Dokumentliste zu einem Aktenzeichen; liefert zusätzlich `dms_link` zum Öffnen im Web-Client |
| `list_running_cases` | Alle laufenden Vorgänge eines Aktenverantwortlichen (Benutzerkürzel), je Treffer mit `reference_nr` und `dms_link` |
| `access_document_fulltext` | Volltext eines Dokuments als Klartext — zum Lesen, Zitieren, Auswerten |
| `download_document` | Originaldatei eines Dokuments, Base64-kodiert |
| `create_case_document` | Erzeugt ein `.docx` aus einer Hausvorlage und legt es dauerhaft im Vorgang ab; liefert `edit_link` zum sofortigen Bearbeiten |

`create_case_document` schreibt in Enaio und ist entsprechend als
`destructiveHint=True` annotiert. Die Tool-Beschreibung enthält eine
ausführliche Zulässigkeitsprüfung: Der Aufruf ist nur nach einer
**ausdrücklichen Speicheranweisung** des Nutzers zulässig, nicht schon dann,
wenn ein Entwurf fertig ist oder abgenommen wurde.

### Resources

Resources sind nur in `AUTH_MODE=basic` sichtbar.

| URI | Inhalt |
|-----|--------|
| `document://{document}/fulltext` | Volltext des Dokuments (Text) |
| `document://{document}/file` | Originaldatei des Dokuments (binär) |

## Konfiguration

Der gesamte Betrieb wird über Umgebungsvariablen gesteuert; es gibt keine
Konfigurationsdatei.

| Variable | Standard | Bedeutung |
|----------|----------|-----------|
| `URL` | `DEFAULT_URL` (Platzhalter) | Basis-URL der Enaio-REST-API, z. B. `https://enaio.example`. Ohne diesen Wert schlägt jeder Enaio-Zugriff fehl. |
| `AUTH_MODE` | `session` | Backend-Authentifizierung: `session` sendet die Tool-`SessionID` als Cookie `JSESSIONID`; `basic` nutzt `USERNAME`/`PASSWORD`. Andere Werte verhindern den Start. |
| `USERNAME` | `DEFAULT_USERNAME` (Platzhalter) | Benutzername für die Basic-Auth-Anmeldung an der API, nur bei `AUTH_MODE=basic` |
| `PASSWORD` | `DEFAULT_PASSWORD` (Platzhalter) | Zugehöriges Passwort, nur bei `AUTH_MODE=basic` |
| `DMS_WEB_URL` | Wert von `URL` | Basis-URL des Enaio-Web-Clients (osweb) für die `dms_link`-Links auf Vorgänge |
| `OFFICE_WEB_URL` | Wert von `URL` | Basis-URL des Enaio-Office-Editors für die `edit_link`-Links auf neu erzeugte Dokumente |
| `ASSETS_DIR` | `./assets` | Verzeichnis mit den `.docx`-Hausvorlagen |
| `OUTPUT_DIR` | `./output` | Ausgabeverzeichnis für erzeugte Dokumente |
| `UPLOAD_RATE_LIMIT_PER_MINUTE` | `30` | Max. Enaio-Uploads pro rollierender Minute; `<= 0` deaktiviert die Begrenzung |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` oder `CRITICAL` (Groß-/Kleinschreibung egal); unbekannte Werte fallen still auf `INFO` zurück |

Zu den drei Web-URLs:

- `DMS_WEB_URL` und `OFFICE_WEB_URL` sind nur nötig, wenn Web-Client bzw.
  Office-Editor **nicht** auf demselben Host wie die REST-API liegen. Ohne eigene
  Angabe wird `URL` verwendet.
- Ist auch `URL` nicht gesetzt, bleibt der Platzhalter `DEFAULT_URL` stehen. In
  dem Fall entsteht kein kaputter Link: `dms_link` und `edit_link` fehlen dann
  einfach in der Antwort.

Die HTTP-Verbindung zur Enaio-API wird ohne Zertifikatsprüfung aufgebaut
(`httpx.AsyncClient(verify=False)` in `EnaioBackend.py`) — passend zu
Installationen mit interner CA.

## Installation und Start

Voraussetzung: Python 3.10 oder neuer.

```bash
pip install -r docker/requirements.txt
```

### Lokal über stdio

```bash
export URL=https://enaio.example
export AUTH_MODE=session
python EnaioMCP.py
```

Mit Basic Auth als Fallback:

```bash
export URL=https://enaio.example
export AUTH_MODE=basic
export USERNAME=... PASSWORD=...
python EnaioMCP.py
```

### Lokal über HTTP

```bash
fastmcp run EnaioMCP.py --host 0.0.0.0 --transport http --log-level INFO
```

### Docker

Das Image klont den Server aus GitHub (Branch `main`) und kopiert die Vorlagen
aus `assets/` hinein; gestartet wird der HTTP-Transport auf Port 8000.

Der Build-Kontext muss dabei sowohl `requirements.txt` als auch `assets/` auf
oberster Ebene enthalten — im Repository liegen die beiden eine Ebene
auseinander (`docker/requirements.txt` und `assets/`), sodass sie für den Build
erst zusammengelegt werden müssen:

```bash
mkdir -p build && cp docker/requirements.txt build/ && cp -r assets build/
docker build -f docker/Dockerfile -t enaio-mcp build
```

```bash
docker run -p 8000:8000 \
  -e URL=https://enaio.example \
  -e AUTH_MODE=session \
  -e OFFICE_WEB_URL=https://office.example \
  enaio-mcp
```

Mit Basic Auth:

```bash
docker run -p 8000:8000 \
  -e URL=https://enaio.example \
  -e AUTH_MODE=basic \
  -e USERNAME=... \
  -e PASSWORD=... \
  -e OFFICE_WEB_URL=https://office.example \
  enaio-mcp
```

## Vorlagen

`create_case_document` befüllt eine `.docx`-Hausvorlage, statt sie nachzubauen —
Briefkopf, Logo, Kopf- und Fußzeile bleiben dadurch erhalten. Die Vorlagen
selbst sind bewusst **nicht** im Repository versioniert und müssen in
`ASSETS_DIR` abgelegt werden; fehlt eine, antwortet der Server mit HTTP 404.

Aufbau der Vorlagen, unterstützte Platzhalter und die Zuordnung Dokumententyp →
Vorlagendatei sind in [`assets/README.md`](assets/README.md) beschrieben, die
Inhaltsblöcke (`heading`, `subheading`, `para`, `listitem`, `table`) in
`vorlage.py`.

## Benutzerkontext über `x-enaio-*`-Header

Angaben zum angemeldeten Benutzer kommen als HTTP-Header herein. Eine
FastMCP-Middleware (`middleware/enaio_headers.py`) liest bei jedem Tool-Aufruf
alle Header mit Präfix `x-enaio-` aus und stellt sie den Tools zur Verfügung. In
den Vorlagen steht jeder dieser Header als Platzhalter mit großem
Anfangsbuchstaben bereit — `x-enaio-mail` wird zu `[Mail]`, `x-enaio-name` zu
`[Name]`. Ein künftiger Header `x-enaio-<feld>` funktioniert ohne Code-Änderung
als `[Feld]`.

Beim stdio-Transport gibt es keine HTTP-Header; die betreffenden Platzhalter
bleiben dann unverändert im Dokument stehen.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Die Tests kommen ohne erreichbares Enaio aus: HTTP-Zugriffe laufen über einen
`httpx.MockTransport` (Fixture `make_backend` in `tests/conftest.py`).

## Projektstruktur

| Pfad | Inhalt |
|------|--------|
| `EnaioMCP.py` | MCP-Server: Tools, Resources, Konfiguration, Linkaufbau |
| `EnaioBackend.py` | HTTP-Zugriff auf die Enaio-REST-API (Suche, Dokumente, Upload) |
| `vorlage.py` | Befüllen der `.docx`-Vorlagen aus den Inhaltsblöcken |
| `middleware/` | `x-enaio-*`-Header-Middleware und Request-Logging |
| `rate_limiter.py` | Rollierendes Minutenlimit für Uploads |
| `logging_config.py` | Prozessweite Logging-Konfiguration (`LOG_LEVEL`) |
| `assets/` | Hausvorlagen (`.docx`, nicht versioniert) |
| `docker/` | Dockerfile und Laufzeit-Abhängigkeiten |
| `tests/` | Pytest-Suite |
