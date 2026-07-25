# -*- coding: utf-8 -*-
"""
vorlage.py – Füllt eine Word-Hausvorlage (.docx) mit strukturierten Inhalten und
schreibt das fertige .docx.

Damit der Briefkopf (Logo, Kopffeld, Fußzeile, Schriftbild) garantiert erhalten
bleibt, wird die Vorlage NICHT nachgebaut, sondern direkt befüllt: Der erzeugte
Body ersetzt den Platzhalter-Absatz [Body] in word/document.xml und das ZIP wird
neu geschrieben. Reine Standardbibliothek, keine externen Abhängigkeiten.

Die Füll-Logik ist aus dem Skript fill_vorlage.py übernommen und in die Funktion
fill_document(...) gekapselt, damit sie ohne argparse/Dateizugriff aufrufbar ist.

Format der Blockliste (blocks): eine Liste von Blöcken. Unterstützte Typen:
  {"type":"heading","text":"1. ...","size":24}
  {"type":"subheading","text":"Auflagen (zwingend)"}
  {"type":"para","runs":[{"t":"Fett: ","b":true},{"t":"normaler Text."}], "jc":"both"}
  {"type":"para","text":"einfacher Absatz"}              # Kurzform ohne runs
  {"type":"listitem","number":1,"runs":[...]}            # number=null -> Aufzählung
  {"type":"listitem","text":"Aufzählungspunkt"}
  {"type":"table","header":["Sp1","Sp2","Sp3"],"rows":[["a","b","c"], ...]}
Run-Attribute: t (Text, Pflicht), b (fett), i (kursiv), size (halbe Punkt, z.B. 24),
color (Hex ohne #).

Zusätzlich werden Platzhalter in eckigen Klammern (z.B. [Datum], [Aktenzeichen]) in der
Vorlage durch übergebene Werte ersetzt (Parameter fields). Die Ersetzung greift über den
Dokumentkörper (word/document.xml) sowie Kopf-/Fußzeilen und Fuß-/Endnoten. Der Platzhalter
[Datum] wird stets automatisch mit dem aktuellen Datum (deutsche Langform) befüllt und
überschreibt einen etwaig übergebenen Wert. Nicht übergebene Platzhalter bleiben unverändert
im Dokument stehen.
"""
import re
import zipfile
from datetime import datetime
from pathlib import Path

_MONATE_DE = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]


def _aktuelles_datum_de():
    """Aktuelles Datum in deutscher Langform, z.B. '24. Juli 2026'."""
    now = datetime.now()
    return f"{now.day}. {_MONATE_DE[now.month - 1]} {now.year}"


def apply_placeholders(xml, replacements):
    """Ersetzt Platzhalter [key] durch den zugehörigen Wert (XML-escaped)."""
    for key, value in replacements.items():
        xml = xml.replace(f"[{key}]", esc(str(value)))
    return xml


def esc(t):
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def replace_subject(doc, placeholder, betreff):
    """Ersetzt die Betreffzeile placeholder durch betreff in word/document.xml.

    Word teilt Text haeufig ueber mehrere Runs/<w:t>-Elemente auf (z.B. durch
    Rechtschreibpruefung oder rsid-Aenderungsverfolgung), sodass "Brief" als
    "B" + "rief" in getrennten Runs vorliegt. Deshalb wird der Platzhalter nicht
    nur innerhalb eines einzelnen <w:t> gesucht, sondern run-uebergreifend:
    zwischen den einzelnen Zeichen sind optionale Run-/Text-Grenzen erlaubt.

    :returns: Tupel (neuer_xml, anzahl_ersetzungen). anzahl_ersetzungen=0, wenn
              der Platzhalter nicht gefunden wurde.
    """
    # Optionale Bruecke zwischen zwei Zeichen: Text-Element schliessen, ueber die
    # Run-Grenze springen (ohne dabei ein weiteres <w:t> zu ueberspringen) und ein
    # neues Text-Element oeffnen.
    bridge = r'(?:</w:t>(?:(?!<w:t[ >]).)*?<w:t(?: [^>]*)?>)?'
    core = bridge.join(re.escape(c) for c in placeholder)
    # g1: oeffnendes <w:t> des ersten Chunks, g2: evtl. vorangehender Text im selben
    # Run, g3: evtl. nachfolgender Text im letzten Run, g4: schliessendes </w:t>.
    pattern = re.compile(
        r'(<w:t(?: [^>]*)?>)'
        r'((?:(?!</w:t>).)*?)'
        + core +
        r'((?:(?!</w:t>).)*?)'
        r'(</w:t>)', re.DOTALL)
    return pattern.subn(
        lambda m: m.group(1) + m.group(2) + esc(betreff) + m.group(3) + m.group(4),
        doc, count=1)


def run_xml(r):
    rpr = []
    if r.get("b"): rpr.append("<w:b/><w:bCs/>")
    if r.get("i"): rpr.append("<w:i/><w:iCs/>")
    if r.get("color"): rpr.append(f'<w:color w:val="{r["color"]}"/>')
    if r.get("size"): rpr.append(f'<w:sz w:val="{r["size"]}"/><w:szCs w:val="{r["size"]}"/>')
    rprx = f"<w:rPr>{''.join(rpr)}</w:rPr>" if rpr else ""
    return f'<w:r>{rprx}<w:t xml:space="preserve">{esc(r.get("t",""))}</w:t></w:r>'


def runs_of(block):
    if "runs" in block:
        return block["runs"]
    return [{"t": block.get("text", "")}]


def para_xml(runs, jc="both", before=120, after=80, ind=None, line=276, pstyle=None):
    # <w:pStyle> muss laut OOXML-Schema das erste Kind von <w:pPr> sein.
    styx = f'<w:pStyle w:val="{pstyle}"/>' if pstyle else ""
    indx = f'<w:ind w:left="{ind[0]}" w:hanging="{ind[1]}"/>' if ind else ""
    ppr = (f'<w:pPr>{styx}<w:spacing w:before="{before}" w:after="{after}" '
           f'w:line="{line}" w:lineRule="auto"/>{indx}<w:jc w:val="{jc}"/></w:pPr>')
    return f'<w:p>{ppr}{"".join(run_xml(r) for r in runs)}</w:p>'


def heading_xml(text, size=24, before=240, after=100, pstyle=None):
    return para_xml([{"t": text, "b": True, "size": size}], jc="left",
                    before=before, after=after, pstyle=pstyle)


def cell_xml(w, text, header=False):
    shade = '<w:shd w:val="clear" w:color="auto" w:fill="D5E0EC"/>' if header else ""
    rpr = "<w:rPr><w:b/><w:bCs/></w:rPr>" if header else ""
    tcpr = (f'<w:tcPr><w:tcW w:w="{w}" w:type="dxa"/>{shade}'
            f'<w:tcMar><w:top w:w="40" w:type="dxa"/><w:left w:w="80" w:type="dxa"/>'
            f'<w:bottom w:w="40" w:type="dxa"/><w:right w:w="80" w:type="dxa"/></w:tcMar></w:tcPr>')
    p = (f'<w:p><w:pPr><w:spacing w:before="20" w:after="20" w:line="240" w:lineRule="auto"/>'
         f'<w:jc w:val="left"/></w:pPr><w:r>{rpr}<w:t xml:space="preserve">{esc(text)}</w:t></w:r></w:p>')
    return f'<w:tc>{tcpr}{p}</w:tc>'


def table_xml(header, rows):
    ncol = len(header)
    total = 9860
    base = total // ncol
    widths = [base] * (ncol - 1) + [total - base * (ncol - 1)]
    border = ('<w:tblBorders>' + "".join(
        f'<w:{s} w:val="single" w:sz="4" w:space="0" w:color="CCCCCC"/>'
        for s in ["top", "left", "bottom", "right", "insideH", "insideV"]) + '</w:tblBorders>')
    tblpr = f'<w:tblPr><w:tblW w:w="{total}" w:type="dxa"/>{border}<w:tblLook w:val="04A0"/></w:tblPr>'
    grid = "<w:tblGrid>" + "".join(f'<w:gridCol w:w="{w}"/>' for w in widths) + "</w:tblGrid>"

    def row(cells, hdr):
        return "<w:tr>" + "".join(cell_xml(widths[i], c, hdr) for i, c in enumerate(cells)) + "</w:tr>"

    body = row(header, True) + "".join(row(r, False) for r in rows)
    return f"<w:tbl>{tblpr}{grid}{body}</w:tbl>"


def build_body(blocks, pstyle=None):
    """Erzeugt den Body aus den Bloecken. pstyle (z.B. "Inhalt") wird als Absatz-
    Formatvorlage auf alle erzeugten Absaetze angewandt, damit die in der Vorlage
    vorgegebene Formatierung des [Body]-Platzhalter-Absatzes erhalten bleibt."""
    out = []
    for blk in blocks:
        t = blk.get("type")
        if t == "heading":
            out.append(heading_xml(blk["text"], size=blk.get("size", 24), pstyle=pstyle))
        elif t == "subheading":
            out.append(para_xml([{"t": blk["text"], "b": True, "size": 22}], jc="left",
                                before=160, after=60, pstyle=pstyle))
        elif t == "para":
            out.append(para_xml(runs_of(blk), jc=blk.get("jc", "both"),
                                before=blk.get("before", 120), after=blk.get("after", 80),
                                pstyle=pstyle))
        elif t == "listitem":
            runs = list(runs_of(blk))
            num = blk.get("number")
            prefix = {"t": (f"{num}.\t" if num is not None else "–\t")}
            out.append(para_xml([prefix] + runs, jc="both", before=40, after=40,
                                ind=(420, 420), pstyle=pstyle))
        elif t == "table":
            out.append(table_xml(blk["header"], blk["rows"]))
        else:
            raise ValueError(f"Unbekannter Blocktyp: {t}")
    return "".join(out)


def clean_settings(zin_names, read):
    """Entfernt einen toten attachedTemplate-Verweis, falls vorhanden."""
    patches = {}
    sp = "word/settings.xml"
    rp = "word/_rels/settings.xml.rels"
    if sp in zin_names:
        s = read(sp).decode("utf-8")
        if "attachedTemplate" in s:
            s = re.sub(r'<w:attachedTemplate[^>]*/>', '', s)
            patches[sp] = s.encode("utf-8")
    if rp in zin_names:
        r = read(rp).decode("utf-8")
        if "attachedTemplate" in r:
            r = re.sub(r'<Relationship\b[^>]*attachedTemplate[^>]*/>', '', r)
            patches[rp] = r.encode("utf-8")
    return patches


def fill_document(template_path, blocks, out_path, betreff=None, subject_placeholder=None,
                  fields=None):
    """
    Füllt die Word-Vorlage template_path mit den Inhalten aus blocks und schreibt das
    Ergebnis nach out_path. Optional wird die Betreffzeile der Vorlage ersetzt.

    :param template_path: Pfad zur .docx-Vorlage (Str oder Path).
    :param blocks:        Liste von Inhaltsblöcken (siehe Modul-Docstring).
    :param out_path:      Zielpfad der erzeugten .docx-Datei (Str oder Path).
    :param betreff:       Optionaler Betreff. Wird nur ersetzt, wenn zusätzlich
                          subject_placeholder gesetzt ist.
    :param subject_placeholder: Der in der Vorlage vorhandene Text der Betreffzeile,
                          der durch betreff ersetzt werden soll (z.B. "Vermerk").
    :param fields:        Optionale Zuordnung Platzhaltername -> Ersatztext. Schlüssel ohne
                          eckige Klammern (z.B. "Aktenzeichen" für den Platzhalter
                          [Aktenzeichen]). [Datum] wird stets automatisch mit dem aktuellen
                          Datum (deutsche Langform) befüllt und überschreibt eine Übergabe.
                          Nicht angegebene Platzhalter bleiben unverändert.
    :returns: Path des geschriebenen Dokuments.
    :raises FileNotFoundError: wenn die Vorlage nicht existiert.
    :raises ValueError: bei unerwartetem Vorlagenaufbau oder unbekanntem Blocktyp.
    """
    tpl = Path(template_path)
    if not tpl.exists():
        raise FileNotFoundError(f"Vorlage nicht gefunden: {tpl}")

    replacements = dict(fields or {})
    replacements["Datum"] = _aktuelles_datum_de()  # immer aktuell, überschreibt Übergabe

    with zipfile.ZipFile(tpl, "r") as zin:
        names = zin.namelist()
        infos = {i.filename: i for i in zin.infolist()}
        read = lambda n: zin.read(n)
        patches = clean_settings(names, read)
        doc = read("word/document.xml").decode("utf-8")
        if "<w:sectPr" not in doc:
            raise ValueError("Kein <w:sectPr> in document.xml gefunden - Vorlage unerwartet aufgebaut.")

        if betreff and subject_placeholder:
            doc, n = replace_subject(doc, subject_placeholder, betreff)
            if n == 0:
                raise ValueError(
                    f'Betreffzeile "{subject_placeholder}" nicht gefunden - Vorlage unerwartet aufgebaut.')

        # Den [Body]-Platzhalter durch den erzeugten Body ersetzen. Der Platzhalter steht
        # in einem eigenen Absatz (<w:p>...<w:t>[Body]</w:t>...</w:p>). Da der Body aus
        # Block-Elementen (<w:p>, <w:tbl>) besteht, muss der GESAMTE Platzhalter-Absatz
        # ersetzt werden - nicht nur der <w:t>-Text -, damit gueltiges OOXML entsteht.
        body_pattern = re.compile(
            r'<w:p\b[^>]*>(?:(?!</w:p>).)*?\[Body\](?:(?!</w:p>).)*?</w:p>', re.DOTALL)
        m_body = body_pattern.search(doc)
        if m_body is not None:
            # Die im Platzhalter-Absatz vorgegebene Absatz-Formatvorlage (z.B. "Inhalt")
            # muss erhalten bleiben und wird auf alle erzeugten Body-Absaetze uebertragen.
            m_style = re.search(r'<w:pStyle\s+w:val="([^"]+)"', m_body.group(0))
            body = build_body(blocks, pstyle=m_style.group(1) if m_style else None)
            doc = doc[:m_body.start()] + body + doc[m_body.end():]
        else:
            # Fallback: [Body] evtl. ohne umschliessenden Absatz oder gar nicht vorhanden.
            body = build_body(blocks)
            if "[Body]" in doc:
                doc = doc.replace("[Body]", body, 1)
            else:
                doc = doc.replace("<w:sectPr", body + "<w:sectPr", 1)
        doc = apply_placeholders(doc, replacements)
        patches["word/document.xml"] = doc.encode("utf-8")

        # Platzhalter auch in Kopf-/Fußzeilen und Fuß-/Endnoten ersetzen. document.xml wird
        # ausschließlich über die doc-Variable behandelt (Body-Einfügung bleibt erhalten).
        for n in names:
            if re.fullmatch(r"word/(header|footer|footnotes|endnotes)\d*\.xml", n):
                src = patches.get(n)
                text = src.decode("utf-8") if src is not None else read(n).decode("utf-8")
                patches[n] = apply_placeholders(text, replacements).encode("utf-8")

        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            for n in names:
                data = patches.get(n, zin.read(n))
                zi = zipfile.ZipInfo(n, date_time=infos[n].date_time)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = infos[n].external_attr
                zout.writestr(zi, data)

    return out
