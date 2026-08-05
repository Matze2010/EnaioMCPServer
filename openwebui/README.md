# Open WebUI Function: Tool Result Logger

`tool_result_logger.py` ist eine eigenstaendige [Open WebUI Function](https://docs.openwebui.com/features/plugin/functions/),
die zur Fehlersuche protokolliert, was Open WebUI nach einem Tool-Aufruf
tatsaechlich verarbeitet.

Die Function patcht zur Laufzeit `open_webui.utils.middleware.process_tool_result`
– die Funktion, die Open WebUI direkt nach jedem Tool-Aufruf zum Aufbereiten des
Ergebnisses ruft – und schreibt deren **Aufrufparameter als `WARNING`** ins Log.
Danach wird unveraendert an das Original delegiert; am Verhalten von Open WebUI
aendert sich nichts.

`WARNING` liegt ueber dem Open-WebUI-Default `INFO`, die Zeilen sind also ohne
Anpassung von `GLOBAL_LOG_LEVEL` sichtbar.

Die Datei ist bewusst **isoliert**: keine Importe aus dem Enaio-MCP-Server, nur
Standardbibliothek plus `pydantic`. Sie liegt hier nur zur Versionierung – im
Betrieb wird sie in Open WebUI eingefuegt.

## Installation

1. In Open WebUI **Admin Panel → Functions → `+`** oeffnen.
2. Den kompletten Inhalt von `tool_result_logger.py` einfuegen und speichern.
3. Die Function **global aktivieren** (Schalter in der Function-Liste). Ohne
   Aktivierung laedt Open WebUI das Modul nicht und der Patch wird nie gesetzt.
4. Ueber das Zahnrad die Valves einstellen, insbesondere `tools`.

Nach dem Laden erscheint einmalig eine Bestaetigungszeile im Log:

```
WARNING open_webui.tool_result_logger: [tool_result_logger] Patch aktiv auf open_webui.utils.middleware.process_tool_result
```

Bleibt sie aus, ist die Function nicht aktiviert. Erscheint stattdessen eine
`ERROR`-Zeile, nennt sie den Grund (Modul- oder Funktionsname nicht gefunden) –
in dem Fall hilft die Valve `target_function`.

## Valves

| Valve | Default | Bedeutung |
| --- | --- | --- |
| `priority` | `0` | Reihenfolge unter mehreren Filtern |
| `enabled` | `true` | Master-Schalter fuer das Logging |
| `tools` | `*` | **Komma-separierte Tool-Namen, fuer die geloggt wird.** `*` = alle, Glob-Muster erlaubt (`list_*`, `*document*`) |
| `log_tool_result` | `true` | Parameter `tool_result` mitloggen |
| `log_metadata` | `true` | Parameter `metadata` mitloggen |
| `log_user` | `true` | Parameter `user` mitloggen |
| `log_request` | `false` | Parameter `request` als `<Request POST /api/chat/completions>` mitloggen |
| `max_value_length` | `2000` | Kuerzung je Wert, `0` = unbegrenzt |
| `redact_keys` | `token,api_key,password,authorization,secret,cookie` | Schluessel in `metadata`/`user`, deren Werte durch `***` ersetzt werden |
| `logger_name` | `open_webui.tool_result_logger` | Ziel-Logger der WARNING-Zeilen |
| `target_function` | *(leer)* | Zu patchende Funktion; leer = automatisch (`process_tool_results`, sonst `process_tool_result`) |

### Tool-Namen des Enaio MCP Servers

Gueltige Werte fuer `tools` (aus `EnaioMCP.py`):

`get_case_metadata`, `list_running_cases`, `list_users`, `list_inbox`,
`get_document_fields`, `create_case_document`, `access_document_fulltext`,
`download_document`

Beispiel – nur Vorgangs- und Posteingangs-Tools protokollieren:

```
tools = get_case_metadata, list_inbox
```

## Beispiel-Ausgabe

```
WARNING open_webui.tool_result_logger: [tool_result_logger] process_tool_result(tool_function_name='get_case_metadata', tool_result='{\n  "aktenzeichen": "DS.1.2-2024-1234",\n  "betreff": "Beispielvorgang"\n}', tool_type='mcp', direct_tool=False, metadata={'chat_id': '…', 'message_id': '…', 'session_id': '…'}, user={'id': '…', 'name': '…', 'role': 'user'})
```

Logs ansehen:

```bash
docker logs -f <open-webui-container> | grep tool_result_logger
```

## Hinweise

- **`tool_result` enthaelt Fachdaten im Klartext.** In produktiven Umgebungen
  `tools` eng setzen oder `log_tool_result` abschalten.
- Die *Eingabe*-Argumente eines Tools erreichen `process_tool_result` nicht –
  geloggt werden die Parameter dieser Funktion, also Tool-Name, Ergebnis,
  Tool-Typ, `direct_tool`, `metadata` und `user`.
- Der Patch ist idempotent: erneutes Speichern der Function ersetzt den
  bestehenden Wrapper, statt einen weiteren darueber zu stapeln.
- Die Parameter werden ueber `inspect.signature(...).bind(...)` an ihre Namen
  gebunden. Ergaenzt eine kuenftige Open-WebUI-Version Parameter, tauchen diese
  automatisch mit im Log auf.
