"""Tests fuer das Befuellen der Word-Vorlage (vorlage.py).

Gearbeitet wird mit einer synthetischen Minimalvorlage: ein .docx ist ein ZIP mit
XML-Teilen, sodass sich Body-Einfuegung, Platzhalter-Ersetzung und das Erhalten
der uebrigen Teile ohne echte Hausvorlage pruefen lassen.
"""

import zipfile

import pytest

import vorlage


DOCUMENT_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body>"
    '<w:p><w:pPr><w:pStyle w:val="Betreffzeile"/></w:pPr>'
    '<w:r><w:t>[Betreff]</w:t></w:r></w:p>'
    '<w:p><w:pPr><w:pStyle w:val="Inhalt"/></w:pPr>'
    '<w:r><w:t>[Body]</w:t></w:r></w:p>'
    '<w:p><w:r><w:t>Aktenzeichen: [Aktenzeichen]</w:t></w:r></w:p>'
    '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>'
    "</w:body></w:document>"
)

HEADER_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:p><w:r><w:t>Stand: [Datum] / [Aktenzeichen]</w:t></w:r></w:p>'
    "</w:hdr>"
)

SETTINGS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:attachedTemplate r:id="rId1" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>'
    "</w:settings>"
)


@pytest.fixture
def template(tmp_path):
    """Minimale, aber strukturell gueltige .docx-Vorlage."""
    path = tmp_path / "Vorlage_Test.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", DOCUMENT_XML)
        z.writestr("word/header1.xml", HEADER_XML)
        z.writestr("word/settings.xml", SETTINGS_XML)
        z.writestr("media/logo.png", b"\x89PNG-nicht-echt")
    return path


def _fill(template, tmp_path, blocks, **kwargs):
    """Fuellt die Vorlage und liest das Ergebnis-ZIP vollstaendig ein.

    :returns: Tupel ``(pfad, {name: bytes}, document_xml)``.
    """
    out = vorlage.fill_document(template, blocks, tmp_path / "out" / "ergebnis.docx", **kwargs)
    with zipfile.ZipFile(out) as z:
        parts = {name: z.read(name) for name in z.namelist()}
    return out, parts, parts["word/document.xml"].decode("utf-8")


def test_body_replaces_placeholder_and_inherits_pstyle(template, tmp_path):
    blocks = [
        {"type": "heading", "text": "1. Sachverhalt"},
        {"type": "para", "text": "Ein Absatz."},
        {"type": "listitem", "number": 1, "text": "Erster Punkt"},
        {"type": "table", "header": ["A", "B"], "rows": [["1", "2"]]},
    ]

    _, _, doc = _fill(template, tmp_path, blocks, betreff="Mein Betreff")

    assert "[Body]" not in doc
    assert "1. Sachverhalt" in doc
    assert "Ein Absatz." in doc
    assert "<w:tbl>" in doc
    # Die Formatvorlage des Platzhalter-Absatzes wird uebernommen.
    assert doc.count('<w:pStyle w:val="Inhalt"/>') >= 3
    # Der Body steht vor dem Abschnittsende.
    assert doc.index("1. Sachverhalt") < doc.index("<w:sectPr")


def test_betreff_replaces_placeholder(template, tmp_path):
    _, _, doc = _fill(template, tmp_path, [], betreff="Anhörung")

    assert "[Betreff]" not in doc
    assert "Anhörung" in doc


def test_without_betreff_placeholder_is_removed(template, tmp_path):
    _, _, doc = _fill(template, tmp_path, [])

    assert "[Betreff]" not in doc


def test_missing_subject_placeholder_with_betreff_raises(template, tmp_path):
    with pytest.raises(ValueError, match="Betreff-Platzhalter"):
        vorlage.fill_document(
            template, [], tmp_path / "out.docx",
            betreff="X", subject_placeholder="[GibtEsNicht]",
        )


def test_fields_replace_placeholders_in_body_and_header(template, tmp_path):
    _, parts, doc = _fill(
        template, tmp_path, [], fields={"Aktenzeichen": "DS.1.2-2024-1234"}
    )

    assert "DS.1.2-2024-1234" in doc
    header = parts["word/header1.xml"].decode("utf-8")
    assert "DS.1.2-2024-1234" in header
    # [Datum] wird immer automatisch gesetzt.
    assert "[Datum]" not in header


def test_datum_cannot_be_overridden(template, tmp_path):
    _, parts, _ = _fill(template, tmp_path, [], fields={"Datum": "01.01.1999"})

    header = parts["word/header1.xml"].decode("utf-8")
    assert "01.01.1999" not in header
    assert vorlage._aktuelles_datum_de() in header


def test_other_parts_are_preserved_and_attached_template_removed(template, tmp_path):
    _, parts, _ = _fill(template, tmp_path, [])

    # Nicht angefasste Teile bleiben unveraendert erhalten.
    assert parts["media/logo.png"] == b"\x89PNG-nicht-echt"
    assert set(parts) == {
        "[Content_Types].xml",
        "word/document.xml",
        "word/header1.xml",
        "word/settings.xml",
        "media/logo.png",
    }
    # Der tote attachedTemplate-Verweis wird entfernt.
    assert "attachedTemplate" not in parts["word/settings.xml"].decode("utf-8")


def test_xml_special_characters_are_escaped(template, tmp_path):
    _, _, doc = _fill(
        template, tmp_path,
        [{"type": "para", "text": "Rechte & Pflichten <wichtig>"}],
        betreff="A & B",
    )

    assert "Rechte &amp; Pflichten &lt;wichtig&gt;" in doc
    assert "A &amp; B" in doc


def test_unknown_block_type_raises(template, tmp_path):
    with pytest.raises(ValueError, match="Unbekannter Blocktyp"):
        vorlage.fill_document(template, [{"type": "quatsch"}], tmp_path / "out.docx")


def test_missing_template_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        vorlage.fill_document(tmp_path / "fehlt.docx", [], tmp_path / "out.docx")


def test_output_directory_is_created(template, tmp_path):
    out_path = tmp_path / "tief" / "verschachtelt" / "ergebnis.docx"

    written = vorlage.fill_document(template, [], out_path)

    assert written == out_path
    assert out_path.exists()
