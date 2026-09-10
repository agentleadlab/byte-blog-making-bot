"""The rebuttal as a .docx, with the attachments embedded where they belong.

Skip Scott's rebuttal is four pages: a fact table, a summary, five numbered
proofs each with its screenshot underneath, an addendum of the cardholder's
own texts, a proof summary and a demand. This builds that, from what
`rebuttal` decided it says.

A .docx rather than a PDF because it is submitted after somebody has read it
- a wrong date or a name spelled two ways is worth catching, and a document
you cannot edit is one that goes out wrong.
"""

from __future__ import annotations

import io
from pathlib import Path

from . import rebuttal

# Wide enough to read a screenshot of a phone conversation, narrow enough to
# stay inside a letter page's margins.
PICTURE_INCHES = 6.0

# What each proof's images are captioned, when nothing better was worked out.
CAPTIONS = {
    "contract": "Signed agreement",
    "invoice": "Invoice, marked paid",
    "texts": "Message from the cardholder",
    "discord": "Support channel",
    "sheet": "Delivered lead sheet",
    "sale": "Posted sale from the delivered leads",
    "other": "Supporting document",
}


class DocError(RuntimeError):
    """python-docx isn't installed, or the file could not be written."""


def _docx():
    try:
        import docx
        from docx.shared import Inches, Pt, RGBColor
    except ImportError as exc:  # pragma: no cover - depends on the machine
        raise DocError(
            "Building a .docx needs python-docx. On the Mac:\n"
            "`cd ~/Desktop/byte-blog-making-bot && "
            ".venv/bin/pip install python-docx`"
        ) from exc
    return docx, Inches, Pt, RGBColor


def build(
    dispute: rebuttal.Dispute,
    written: dict,
    found: rebuttal.Gathered,
    exhibits: list,
    *,
    into: Path,
) -> Path:
    """Write the file and return where it went.

    `written` is what Claude wrote, keyed by the evidence name it came from,
    plus "summary". A proof with nothing written and nothing attached is left
    out of the document entirely rather than printed empty - a heading with no
    evidence under it reads as evidence that does not exist.
    """
    docx, Inches, Pt, RGBColor = _docx()

    doc = docx.Document()
    _title(doc, dispute, Pt, RGBColor)
    _facts(doc, dispute)

    holes = rebuttal.what_is_missing(found, exhibits)
    if holes:
        _holes(doc, holes, RGBColor)

    if written.get("summary"):
        doc.add_heading("SUMMARY", level=1)
        doc.add_paragraph(str(written["summary"]).strip())
        waited = rebuttal.waited_line(dispute)
        if waited:
            doc.add_paragraph(waited.replace("**", ""))

    by_kind: dict[str, list] = {}
    for one in exhibits:
        by_kind.setdefault(one.kind, []).append(one)

    said = 0
    for number, heading in rebuttal.PROOFS:
        name = _evidence_for(number)
        body = str(written.get(name) or "").strip()
        pictures = [
            one for one in by_kind.get(_exhibit_for(number), []) if one.is_image()
        ]
        if not body and not pictures:
            continue
        said += 1
        doc.add_heading(f"PROOF #{said} — {heading}", level=1)
        if body:
            for chunk in body.split("\n\n"):
                if chunk.strip():
                    doc.add_paragraph(chunk.strip())
        for picture in pictures:
            _picture(doc, picture, Inches, Pt)

    leftover = [
        one for one in by_kind.get("other", []) if one.is_image()
    ]
    if leftover:
        doc.add_heading("FURTHER SUPPORTING MATERIAL", level=1)
        for picture in leftover:
            _picture(doc, picture, Inches, Pt)

    if found.timeline:
        doc.add_heading("TIMELINE", level=1)
        for when, what in found.timeline:
            doc.add_paragraph(f"{when}    {what}", style="List Bullet")

    doc.add_heading("FINAL DEMAND", level=1)
    doc.add_paragraph(rebuttal.demand(dispute))
    doc.add_paragraph()
    doc.add_paragraph("SUBMITTED BY")
    doc.add_paragraph(f"{dispute.dba}")

    into.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(into))
    return into


def _title(doc, dispute, Pt, RGBColor) -> None:
    heading = doc.add_heading("CHARGEBACK REBUTTAL", level=0)
    for run in heading.runs:
        run.font.color.rgb = RGBColor(0x11, 0x11, 0x11)
    line = doc.add_paragraph()
    run = line.add_run(
        f"MID: {dispute.mid}  |  DBA: {dispute.dba}" if dispute.mid
        else f"DBA: {dispute.dba}"
    )
    run.bold = True
    run.font.size = Pt(10)


def _facts(doc, dispute) -> None:
    rows = rebuttal.header(dispute)
    if not rows:
        return
    table = doc.add_table(rows=0, cols=2)
    table.style = "Light Grid Accent 1"
    for label, value in rows:
        cells = table.add_row().cells
        cells[0].text = label
        cells[1].text = str(value)
        for run in cells[0].paragraphs[0].runs:
            run.bold = True
    doc.add_paragraph()


def _holes(doc, holes, RGBColor) -> None:
    """What is still needed, at the top where somebody reads it.

    Removed before this goes anywhere near an acquirer - and saying so on the
    page is what makes sure it is.
    """
    heading = doc.add_heading("BEFORE YOU SEND THIS — still needed", level=1)
    for run in heading.runs:
        run.font.color.rgb = RGBColor(0xB0, 0x00, 0x00)
    for hole in holes:
        doc.add_paragraph(hole, style="List Bullet")
    note = doc.add_paragraph()
    run = note.add_run("Delete this section before submitting.")
    run.italic = True
    doc.add_paragraph()


def _picture(doc, exhibit, Inches, Pt) -> None:
    try:
        doc.add_picture(io.BytesIO(exhibit.data), width=Inches(PICTURE_INCHES))
    except Exception:
        # A screenshot Word will not take is not worth losing the document
        # over. Named instead, so somebody can drop it in by hand.
        line = doc.add_paragraph()
        run = line.add_run(f"[couldn't embed {exhibit.name} — attach it by hand]")
        run.italic = True
        return
    caption = doc.add_paragraph()
    run = caption.add_run(
        exhibit.caption or CAPTIONS.get(exhibit.kind, CAPTIONS["other"])
    )
    run.italic = True
    run.font.size = Pt(9)


def _evidence_for(number: int) -> str:
    return {
        1: "contract", 2: "invoice", 3: "delivery",
        4: "sheet", 5: "activity", 6: "texts",
    }.get(number, "")


def _exhibit_for(number: int) -> str:
    return {
        1: "contract", 2: "invoice", 3: "discord",
        4: "sheet", 5: "sale", 6: "texts",
    }.get(number, "other")
