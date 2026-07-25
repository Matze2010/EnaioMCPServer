# Vorlagen (Word-Hausvorlagen)

In diesem Verzeichnis liegen die `.docx`-Hausvorlagen, aus denen der Endpunkt
`create_case_document` Dokumente erzeugt. Die Zuordnung von Dokumententyp zu
Vorlagendatei ist in `EnaioMCP.py` in `DOCUMENT_TEMPLATES` definiert.

## Erwartete Dateien

| Dokumententyp | Vorlagendatei          | Betreffzeile (Platzhalter) |
|---------------|------------------------|----------------------------|
| Vermerk       | `Vorlage_Vermerk.docx` | `Vermerk`                  |
| Brief         | `Vorlage_Brief.docx`   | `Brief`                    |

Die eigentlichen `.docx`-Dateien werden bewusst **nicht** im Repository
versioniert und müssen hier abgelegt werden. Fehlt eine Vorlage, liefert der
Endpunkt einen sprechenden Fehler (HTTP 404).

Das Vorlagenverzeichnis lässt sich über die Umgebungsvariable `ASSETS_DIR`
überschreiben (Standard: dieses Verzeichnis).

## Aufbau einer Vorlage

Die Vorlage wird **nicht** nachgebaut, sondern direkt befüllt, damit Briefkopf,
Logo, Kopf- und Fußzeile erhalten bleiben:

- Der erzeugte Inhalt wird direkt vor `<w:sectPr>` in `word/document.xml`
  eingefügt. Die Vorlage muss daher ein `<w:sectPr>`-Element enthalten (bei einer
  normalen, in Word gespeicherten `.docx` immer der Fall).
- Ist ein Betreff angegeben, wird der in der Tabelle genannte Platzhalter-Text der
  Betreffzeile (z. B. `Vermerk`) durch den übergebenen Betreff ersetzt. Der
  Platzhalter muss als eigener Textlauf in der Vorlage vorkommen.

Die unterstützten Inhaltsblöcke (heading, subheading, para, listitem, table) sind
in `vorlage.py` dokumentiert.

## Enaio-Upload und Rate-Limit

Nach dem lokalen Erzeugen lädt `create_case_document` das Dokument über den
Enaio-Endpunkt `POST /api/dms/objects` in den zugehörigen Vorgang (Aktenzeichen)
hoch (Objekttyp `OSTPL_AA_DOKUMENT`). Das lokal erzeugte Dokument bleibt als
Fallback erhalten; die Antwort enthält `stored_in_enaio: true` und die
`enaio_object_id`.

Ein RateLimiter begrenzt die Uploads auf eine konfigurierbare Anzahl pro Minute:

| Umgebungsvariable                 | Standard | Bedeutung                                             |
|-----------------------------------|----------|-------------------------------------------------------|
| `UPLOAD_RATE_LIMIT_PER_MINUTE`    | `30`     | Max. Uploads pro rollierender Minute; `<= 0` = unbegrenzt |

Wird das Limit überschritten, wird der Upload sofort mit **HTTP 429** abgelehnt
(inkl. `Retry-After`-Header); es wird nicht gewartet.
