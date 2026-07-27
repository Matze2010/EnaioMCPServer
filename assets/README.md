# Vorlagen (Word-Hausvorlagen)

In diesem Verzeichnis liegen die `.docx`-Hausvorlagen, aus denen der Endpunkt
`create_case_document` Dokumente erzeugt. Die Zuordnung von Dokumententyp zu
Vorlagendatei ist in `EnaioMCP.py` in `DOCUMENT_TEMPLATES` definiert.

## Erwartete Dateien

| Dokumententyp | Vorlagendatei          | Betreffzeile (Platzhalter) |
|---------------|------------------------|----------------------------|
| Vermerk       | `Vorlage_Vermerk.docx` | `[Betreff]`                |
| Brief         | `Vorlage_Brief.docx`   | `[Betreff]`                |

Die eigentlichen `.docx`-Dateien werden bewusst **nicht** im Repository
versioniert und müssen hier abgelegt werden. Fehlt eine Vorlage, liefert der
Endpunkt einen sprechenden Fehler (HTTP 404).

Das Vorlagenverzeichnis lässt sich über die Umgebungsvariable `ASSETS_DIR`
überschreiben (Standard: dieses Verzeichnis).

## Aufbau einer Vorlage

Die Vorlage wird **nicht** nachgebaut, sondern direkt befüllt, damit Briefkopf,
Logo, Kopf- und Fußzeile erhalten bleiben:

- Der erzeugte Inhalt ersetzt den Platzhalter-Absatz `[Body]` in
  `word/document.xml`; dessen Absatz-Formatvorlage (z. B. `Inhalt`) wird auf den
  eingefügten Inhalt übertragen. Die Vorlage muss ein `<w:sectPr>`-Element
  enthalten (bei einer normalen, in Word gespeicherten `.docx` immer der Fall).
- Die Betreffzeile wird über den Platzhalter `[Betreff]` gesetzt: Ist ein Betreff
  angegeben, wird `[Betreff]` durch den übergebenen Text ersetzt; ohne Betreff wird
  der Platzhalter entfernt. Die Ersetzung funktioniert auch, wenn Word den
  Platzhalter intern über mehrere Textläufe aufgeteilt hat.

- Weitere Platzhalter in eckigen Klammern (z. B. `[Aktenzeichen]`, `[Datum]`)
  werden über den Parameter `fields` befüllt; nicht übergebene Platzhalter
  bleiben unverändert stehen.

Die unterstützten Inhaltsblöcke (heading, subheading, para, listitem, table) sind
in `vorlage.py` dokumentiert. Das Tool nimmt sie im Parameter `content` als
JSON-String entgegen, also als String, der das JSON-Array der Blöcke enthält.

## Manuell befüllbare `fields`

Das Tool `get_document_fields` gibt je Dokumenttyp zurück, welche Platzhalter
sinnvoll vor dem Aufruf von `create_case_document` im Parameter `fields`
befüllt werden können. `fields` wird dabei als JSON-String übergeben, also als
String, der ein JSON-Objekt enthält (z. B.
`"{\"Adressat\":\"Ministerium für Bildung\",\"Ort\":\"Musterstadt\"}"`).

| Dokumententyp | Platzhalter | Erwarteter Inhalt |
|---------------|-------------|-------------------|
| Brief         | `Adressat`  | Name bzw. Bezeichnung der adressierten Person, Stelle oder Organisation. |
| Brief         | `Anschrift` | Anschrift der adressierten Person, Stelle oder Organisation. |
| Brief         | `PLZ`       | Postleitzahl der adressierten Person, Stelle oder Organisation. |
| Brief         | `Ort`       | Ort der adressierten Person, Stelle oder Organisation. |
| Brief         | `Bearbeiter` | Name des Verfassers / Bearbeiters des Dokuments. |
| Brief         | `Durchwahl` | Durchwahl des Verfassers / Bearbeiters des Dokuments. |
| Brief         | `Email`     | E-Mail-Adresse des Verfassers / Bearbeiters des Dokuments. |
| Vermerk       | —           | Keine manuell zu erfragenden optionalen `fields`-Platzhalter. |

Nur bekannte Angaben befüllen; fehlende Angaben werden nicht ergänzt.

Technische Platzhalter wie `[Body]` und `[Betreff]` sowie automatisch befüllte
Platzhalter wie `[Datum]`, `[Aktenzeichen]`, `[Name]` oder `[Mail]` erscheinen
nicht in der Rückgabe von `get_document_fields`.

## Automatisch befüllte Platzhalter zum Benutzer

Die Identität des Aufrufers kommt über HTTP-Header vom Typ `x-enaio-*` herein
(siehe `middleware/enaio_headers.py`). Jeder dieser Header steht in der Vorlage
als Platzhalter zur Verfügung — der Feldname hinter dem Präfix, mit großem
Anfangsbuchstaben:

| HTTP-Header        | Platzhalter  |
|--------------------|--------------|
| `x-enaio-mail`     | `[Mail]`     |
| `x-enaio-name`     | `[Name]`     |
| `x-enaio-username` | `[Username]` |

Ein künftiger Header `x-enaio-<feld>` steht ohne Code-Änderung als `[Feld]`
bereit. Vorrang hat immer ein ausdrücklich in `fields` übergebener Wert; fehlt
der Header (z. B. beim stdio-Transport), bleibt der Platzhalter unverändert im
Dokument stehen.

## Enaio-Upload und Rate-Limit

Nach dem lokalen Erzeugen lädt `create_case_document` das Dokument über den
Enaio-Endpunkt `POST /api/dms/objects` in den zugehörigen Vorgang (Aktenzeichen)
hoch (Objekttyp `OSTPL_AA_DOKUMENT`). Nach erfolgreichem Upload wird die lokal
erzeugte Datei wieder gelöscht; die Antwort enthält `stored_in_enaio: true` und
die `enaio_object_id`. Scheitert der Upload (oder greift das Rate-Limit), bleibt
die Datei im Ausgabeverzeichnis (`OUTPUT_DIR`) liegen.

Ein RateLimiter begrenzt die Uploads auf eine konfigurierbare Anzahl pro Minute:

| Umgebungsvariable                 | Standard | Bedeutung                                             |
|-----------------------------------|----------|-------------------------------------------------------|
| `UPLOAD_RATE_LIMIT_PER_MINUTE`    | `30`     | Max. Uploads pro rollierender Minute; `<= 0` = unbegrenzt |

Wird das Limit überschritten, wird der Upload sofort mit **HTTP 429** abgelehnt
(inkl. `Retry-After`-Header); es wird nicht gewartet.
