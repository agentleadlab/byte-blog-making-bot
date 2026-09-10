"""The rebuttal as a .docx, with the attachments embedded where they belong.

Skip Scott's rebuttal is four pages: a fact table, a summary, five numbered
proofs each with its screenshot underneath, an addendum of the cardholder's
own texts, a proof summary and a demand. This builds that, from what
`rebuttal` decided it says.

It is read by somebody at an acquirer who has a queue of these and no reason
to be generous, so it is set like a document rather than left in Word's
defaults: black headings, a plain fact table, one typeface, and the evidence
captioned under it. A rebuttal that looks thrown together invites being read
as one.

A .docx rather than a PDF because it is submitted after somebody has read it
- a wrong date or a name spelled two ways is worth catching, and a document
you cannot edit is one that goes out wrong.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from . import rebuttal

# Wide enough to read a screenshot of a phone conversation, and inside the
# margins below with room to spare.
PICTURE_INCHES = 5.6

# One typeface throughout. Word's default changes between versions and
# between machines, and a document that renders differently on the sender's
# screen and the reader's is one nobody can proofread.
FACE = "Calibri"
INK = (0x1A, 0x1A, 0x1A)
QUIET = (0x66, 0x66, 0x66)
WARN = (0xA6, 0x1B, 0x1B)
RULE = "BFBFBF"

# What each proof's images are captioned, when the reading did not work one out.
CAPTIONS = {
    "contract": "Signed agreement",
    "invoice": "Invoice, marked paid",
    "texts": "Message from the cardholder",
    "discord": "Support channel",
    "sheet": "Delivered lead sheet",
    "sale": "Posted sale from the delivered leads",
    "other": "Supporting document",
}

# A proof whose evidence is only pictures still needs a sentence over them,
# or the section is a heading and a pile of screenshots.
OVER_THE_PICTURES = {
    "contract": "The executed agreement is reproduced below.",
    "invoice": "The invoice for this order is reproduced below.",
    "discord": "The support channel for this client is reproduced below.",
    "sheet": "The delivered lead sheet is reproduced below.",
    "sale": "The sale the cardholder posted is reproduced below.",
    "texts": (
        "The cardholder's own messages are reproduced below, in the order they "
        "were sent."
    ),
}

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*")
_BULLET = re.compile(r"^\s{0,3}[-*•]\s+")
_NUMBERED = re.compile(r"^\s{0,3}\d+[.)]\s+")
# "1. The service was clearly described before purchase (Exhibits A and B)" is
# an argument's heading; "1. He asked for a comparison, and got one." is a
# sentence that happens to be numbered. A heading does not end in a full stop.
# The labels the writing is asked for. Markers rather than shapes: the first
# version guessed a heading from "a numbered line with no full stop", and the
# writing came back numbered *with* full stops, so every argument in the
# document rendered as a bullet and it had no structure at all.
_ARGUMENT = re.compile(r"^\s{0,3}ARGUMENT\s*[:.\-—]\s*(.+?)\s*$", re.IGNORECASE)
_SECTION = re.compile(
    r"^\s{0,3}(SUMMARY|CONCLUSION|TIMELINE|BACKGROUND)\s*[:.]?\s*$", re.IGNORECASE
)


class DocError(RuntimeError):
    """python-docx isn't installed, or the file could not be written."""


def _docx():
    try:
        import docx
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Inches, Pt, RGBColor
    except ImportError as exc:  # pragma: no cover - depends on the machine
        raise DocError(
            "Building a .docx needs python-docx. On the Mac:\n"
            "`cd ~/Desktop/byte-blog-making-bot && "
            ".venv/bin/pip install python-docx`"
        ) from exc
    return docx, Inches, Pt, RGBColor, WD_ALIGN_PARAGRAPH


def build(
    dispute: rebuttal.Dispute,
    written: dict,
    found: rebuttal.Gathered,
    exhibits: list,
    *,
    into: Path,
) -> Path:
    """Write the file and return where it went.

    Argument first, evidence at the back. The person reading it wants to know
    what we say happened and why, and to be able to check any of it - so the
    numbered arguments cite Exhibit A and Exhibit A is where they said it is,
    one screenshot to a page with what it says in English underneath.
    """
    docx, Inches, Pt, RGBColor, ALIGN = _docx()

    doc = docx.Document()
    _set_up(doc, Pt, RGBColor, Inches)
    _title(doc, dispute, Pt, RGBColor, ALIGN)
    _facts(doc, dispute, Pt, RGBColor)

    holes = rebuttal.what_is_missing(found, exhibits)
    if holes:
        _holes(doc, holes, Pt, RGBColor)

    body = str(written.get("body") or written.get("summary") or "").strip()
    messages = str(written.get("messages") or "").strip()

    # The messages go under the summary rather than after the conclusion:
    # they are what the arguments below are about, and an acquirer who reads
    # only the first page should be reading them.
    opening, arguments = _split_at_first_argument(body)
    counted = [0]
    if opening:
        _prose(doc, opening, Pt, RGBColor, counted)
    if messages:
        _heading(doc, "Key Messages", Pt, RGBColor)
        _message_table(doc, messages, Pt, RGBColor, Inches)
    if arguments:
        _prose(doc, arguments, Pt, RGBColor, counted)

    # Only when nothing was written. The number is in the prompt, so a
    # written rebuttal makes the argument itself and better - repeating it
    # underneath the conclusion reads like a note somebody forgot to move.
    if not body:
        waited = rebuttal.waited_line(dispute)
        if waited:
            _prose(doc, waited, Pt, RGBColor)

    if found.timeline:
        _heading(doc, "Timeline", Pt, RGBColor)
        for when, what in found.timeline:
            line = doc.add_paragraph(style="List Bullet")
            _ink(line.add_run(f"{when}   "), Pt, RGBColor, bold=True)
            _ink(line.add_run(what), Pt, RGBColor)

    groups = rebuttal.exhibit_groups(exhibits)
    if groups:
        _exhibits(doc, groups, Pt, RGBColor, Inches, ALIGN, docx)

    into.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(into))
    return into


def _exhibits(doc, groups, Pt, RGBColor, Inches, ALIGN, docx) -> None:
    """The evidence, at the back, lettered and one image to a page.

    With what it says in English under it. The cardholder's WhatsApp is in
    Spanish and the person deciding this dispute will not be reading Spanish -
    a screenshot they cannot read is a screenshot that proves nothing.
    """
    from docx.enum.text import WD_BREAK

    for letter, what, group in groups:
        doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        line = doc.add_paragraph()
        line.paragraph_format.space_after = Pt(3)
        _ink(line.add_run(f"EXHIBIT {letter} — {what}"), Pt, RGBColor, size=13, bold=True)

        about = doc.add_paragraph()
        about.paragraph_format.space_after = Pt(8)
        _ink(about.add_run(_about(group)), Pt, RGBColor, size=9.5, colour=QUIET)
        _rule(about)

        for number, one in enumerate(group):
            if number:
                doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
            if one.of > 1:
                head = doc.add_paragraph()
                head.paragraph_format.space_after = Pt(4)
                _ink(head.add_run(one.label()), Pt, RGBColor, size=10.5, bold=True)
            if one.is_pdf():
                _the_file(doc, one, Pt, RGBColor)
            else:
                _picture(doc, one, Inches, Pt, RGBColor, ALIGN)
            if one.transcript:
                said = doc.add_paragraph()
                said.paragraph_format.space_before = Pt(6)
                said.paragraph_format.space_after = Pt(2)
                _ink(
                    said.add_run("What it says, in English"),
                    Pt, RGBColor, size=9.5, bold=True, colour=QUIET,
                )
                _transcript(doc, one.transcript, Pt, RGBColor, Inches)


def _split_at_first_argument(body: str) -> tuple[str, str]:
    """(the summary, everything from the first argument on).

    So the message table can go between them. Nothing is dropped: when there
    are no arguments the whole thing comes back as the opening.
    """
    lines = str(body or "").splitlines()
    for number, line in enumerate(lines):
        if _ARGUMENT.match(line):
            return "\n".join(lines[:number]).strip(), "\n".join(lines[number:]).strip()
    return str(body or "").strip(), ""


def _message_table(doc, messages, Pt, RGBColor, Inches) -> None:
    """The messages that decide it, as a table of when / who / what.

    Quoting them in a paragraph buries them. In a table an acquirer can read
    the four lines that matter without reading the exhibit, and then check the
    exhibit if they want to.
    """
    rows = []
    for line in messages.splitlines():
        parts = [part.strip() for part in line.split("|")]
        if len(parts) >= 3 and any(parts):
            rows.append(parts[:3])
    if not rows:
        return

    table = doc.add_table(rows=0, cols=3)
    table.style = "Table Grid"
    _hairlines(table)
    head = table.add_row().cells
    for cell, title in zip(head, ("Date / Time", "Sender", "Message")):
        cell.paragraphs[0].paragraph_format.space_after = Pt(2)
        _ink(cell.paragraphs[0].add_run(title), Pt, RGBColor, size=9,
             bold=True, colour=QUIET)
    for when, who, what in rows:
        cells = table.add_row().cells
        for cell, said, bold in ((cells[0], when, False), (cells[1], who, True),
                                 (cells[2], what, False)):
            cell.paragraphs[0].paragraph_format.space_after = Pt(2)
            _ink(cell.paragraphs[0].add_run(said), Pt, RGBColor, size=9, bold=bold)
    for row in table.rows:
        row.cells[0].width = Inches(1.15)
        row.cells[1].width = Inches(1.0)
        row.cells[2].width = Inches(4.6)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def _transcript(doc, transcript, Pt, RGBColor, Inches) -> None:
    """The lines of a conversation as one block, not twenty paragraphs.

    Each on its own line inside a single paragraph, so Word leads them like a
    transcript instead of putting seven points of air between every message.
    """
    lines = [row.strip() for row in transcript.splitlines() if row.strip()]
    if not lines:
        return
    block = doc.add_paragraph()
    block.paragraph_format.space_after = Pt(10)
    block.paragraph_format.left_indent = Inches(0.2)
    block.paragraph_format.line_spacing = 1.0
    for number, row in enumerate(lines):
        run = block.add_run(row)
        _ink(run, Pt, RGBColor, size=9)
        if number < len(lines) - 1:
            run.add_break()


# ------------------------------------------------------------------ the look


def _set_up(doc, Pt, RGBColor, Inches) -> None:
    """One typeface, margins that fit a screenshot, and no Word blue."""
    normal = doc.styles["Normal"]
    normal.font.name = FACE
    normal.font.size = Pt(10.5)
    normal.font.color.rgb = RGBColor(*INK)
    normal.paragraph_format.space_after = Pt(7)
    normal.paragraph_format.line_spacing = 1.15

    for name in ("List Bullet", "List Number"):
        try:
            style = doc.styles[name]
        except KeyError:  # pragma: no cover - depends on the template
            continue
        style.font.name = FACE
        style.font.size = Pt(10.5)
        style.font.color.rgb = RGBColor(*INK)
        style.paragraph_format.space_after = Pt(3)

    for level in (1, 2, 3):
        try:
            style = doc.styles[f"Heading {level}"]
        except KeyError:  # pragma: no cover - depends on the template
            continue
        style.font.name = FACE
        style.font.color.rgb = RGBColor(*INK)
        style.font.bold = True
        style.font.size = Pt(12 if level == 1 else 11)

    for section in doc.sections:
        section.top_margin = Inches(0.8)
        section.bottom_margin = Inches(0.8)
        section.left_margin = Inches(0.9)
        section.right_margin = Inches(0.9)


def _ink(run, Pt, RGBColor, *, size: float = 10.5, bold=False, italic=False,
         colour=INK) -> None:
    run.font.name = FACE
    run.font.size = Pt(size)
    run.font.color.rgb = RGBColor(*colour)
    run.bold = bold
    run.italic = italic


def _title(doc, dispute, Pt, RGBColor, ALIGN) -> None:
    line = doc.add_paragraph()
    line.paragraph_format.space_after = Pt(1)
    _ink(
        line.add_run("CHARGEBACK REBUTTAL / REPRESENTMENT"),
        Pt, RGBColor, size=17, bold=True,
    )
    under = doc.add_paragraph()
    under.paragraph_format.space_after = Pt(10)
    _ink(
        under.add_run(f"{dispute.dba} — Response to Cardholder Dispute"),
        Pt, RGBColor, size=10.5, colour=QUIET,
    )
    _rule(under)


def _hairlines(table) -> None:
    """Strip the black grid off a table and leave a rule between rows.

    Word's only borderless built-in table style is not in every template, so
    the borders are set here rather than chosen. A fact table boxed in black
    on every cell reads as a spreadsheet somebody pasted in; the same rows
    with a hairline between them read as a document.
    """
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    marks = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideV"):
        one = OxmlElement(f"w:{edge}")
        one.set(qn("w:val"), "none")
        one.set(qn("w:sz"), "0")
        borders.append(one)
    inside = OxmlElement("w:insideH")
    inside.set(qn("w:val"), "single")
    inside.set(qn("w:sz"), "4")
    inside.set(qn("w:color"), "E0E0E0")
    borders.append(inside)
    marks.append(borders)


def _rule(paragraph) -> None:
    """A hairline under a paragraph, drawn as a bottom border."""
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    marks = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "6")
    bottom.set(qn("w:color"), RULE)
    borders.append(bottom)
    marks.append(borders)


def _heading(doc, text, Pt, RGBColor, *, level: int = 1) -> None:
    """A real Word heading, restyled. The style matters as much as the look:
    it is what gives the document an outline, a navigation pane and a shape
    somebody can skim - `Normal` text made bold is a document with no parts.
    """
    line = doc.add_paragraph(style=f"Heading {level}")
    for run in list(line.runs):
        run.text = ""
    line.paragraph_format.space_before = Pt(15)
    line.paragraph_format.space_after = Pt(4)
    line.paragraph_format.keep_with_next = True
    _ink(line.add_run(text), Pt, RGBColor, size=12 if level == 1 else 11, bold=True)


def _facts(doc, dispute, Pt, RGBColor) -> None:
    rows = rebuttal.header(dispute)
    if not rows:
        return
    table = doc.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    table.autofit = True
    _hairlines(table)
    for label, value in rows:
        cells = table.add_row().cells
        left = cells[0].paragraphs[0]
        left.paragraph_format.space_after = Pt(2)
        _ink(left.add_run(label), Pt, RGBColor, size=9.5, bold=True, colour=QUIET)
        right = cells[1].paragraphs[0]
        right.paragraph_format.space_after = Pt(2)
        _ink(right.add_run(str(value)), Pt, RGBColor, size=9.5)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def _holes(doc, holes, Pt, RGBColor) -> None:
    """What is still needed, at the top where somebody reads it.

    Removed before this goes anywhere near an acquirer - and saying so on the
    page is what makes sure it is.
    """
    line = doc.add_paragraph()
    line.paragraph_format.space_before = Pt(10)
    line.paragraph_format.space_after = Pt(4)
    _ink(
        line.add_run("BEFORE YOU SEND THIS — still needed"),
        Pt, RGBColor, size=11, bold=True, colour=WARN,
    )
    for hole in holes:
        one = doc.add_paragraph(style="List Bullet")
        _ink(one.add_run(hole), Pt, RGBColor, size=9.5, colour=WARN)
    note = doc.add_paragraph()
    note.paragraph_format.space_after = Pt(12)
    _ink(
        note.add_run("Delete this whole section before submitting."),
        Pt, RGBColor, size=9, italic=True, colour=WARN,
    )
    _rule(note)


def _prose(doc, text, Pt, RGBColor, numbered=None) -> None:
    """Claude's writing, as paragraphs, bullets and bold rather than markup.

    It writes markdown because everything else it writes is read as markdown.
    Word is not, so "**attached files**" printed with its asterisks showing in
    the middle of a document going to an acquirer.
    """
    numbered = numbered if numbered is not None else [0]
    for block in str(text or "").split("\n"):
        line = block.rstrip()
        if not line.strip():
            continue
        if _HEADING.match(line):
            bare = _HEADING.sub("", line).strip().strip("*")
            if not bare:
                continue
            small = doc.add_paragraph()
            small.paragraph_format.space_before = Pt(8)
            small.paragraph_format.space_after = Pt(2)
            _ink(small.add_run(bare), Pt, RGBColor, size=10.5, bold=True)
            continue
        section = _SECTION.match(line)
        if section:
            _heading(doc, section.group(1).title(), Pt, RGBColor)
            continue
        argued = _ARGUMENT.match(line)
        if argued:
            numbered[0] += 1
            _heading(
                doc, f"{numbered[0]}.  {argued.group(1).strip().rstrip('.')}",
                Pt, RGBColor,
            )
            continue
        if _BULLET.match(line):
            _runs(doc.add_paragraph(style="List Bullet"), _BULLET.sub("", line), Pt, RGBColor)
            continue
        if _NUMBERED.match(line):
            _runs(doc.add_paragraph(style="List Number"), _NUMBERED.sub("", line), Pt, RGBColor)
            continue
        _runs(doc.add_paragraph(), line.strip(), Pt, RGBColor)


def _runs(paragraph, text, Pt, RGBColor) -> None:
    """One paragraph, with **bold** turned into bold rather than asterisks."""
    where = 0
    for found in _BOLD.finditer(text):
        if found.start() > where:
            _ink(paragraph.add_run(text[where:found.start()]), Pt, RGBColor)
        _ink(paragraph.add_run(found.group(1)), Pt, RGBColor, bold=True)
        where = found.end()
    if where < len(text):
        _ink(paragraph.add_run(text[where:]), Pt, RGBColor)


def _picture(doc, exhibit, Inches, Pt, RGBColor, ALIGN) -> None:
    holder = doc.add_paragraph()
    holder.alignment = ALIGN.CENTER
    holder.paragraph_format.space_before = Pt(8)
    holder.paragraph_format.space_after = Pt(1)
    try:
        holder.add_run().add_picture(io.BytesIO(exhibit.data), width=Inches(PICTURE_INCHES))
    except Exception:
        # A screenshot Word will not take is not worth losing the document
        # over. Named instead, so somebody can drop it in by hand.
        _ink(
            holder.add_run(f"[couldn't embed {exhibit.name} — attach it by hand]"),
            Pt, RGBColor, size=9, italic=True, colour=WARN,
        )
        return
    caption = doc.add_paragraph()
    caption.alignment = ALIGN.CENTER
    caption.paragraph_format.space_after = Pt(10)
    _ink(
        caption.add_run(
            exhibit.caption or CAPTIONS.get(exhibit.kind, CAPTIONS["other"])
        ),
        Pt, RGBColor, size=8.5, italic=True, colour=QUIET,
    )


def _about(group) -> str:
    """One line describing a whole exhibit, rather than repeating the first
    screenshot's caption over the top of that same screenshot."""
    if len(group) == 1:
        return group[0].caption or CAPTIONS.get(group[0].kind, "")
    return (
        f"{len(group)} screenshots, in the order the conversation happened. "
        "Each is followed by what it says in English."
    )


def _the_file(doc, exhibit, Pt, RGBColor) -> None:
    """A PDF exhibit, which is a file rather than a picture.

    Word will not take a PDF as an image and rendering one to an image needs
    a tool the Mac does not have. Named and described instead, and submitted
    beside the rebuttal - which is what an acquirer's portal wants anyway: a
    six-page agreement flattened into a screenshot is a six-page agreement
    nobody can read.
    """
    line = doc.add_paragraph()
    line.paragraph_format.space_after = Pt(3)
    _ink(
        line.add_run(f"Submitted as a separate file: {exhibit.name}"),
        Pt, RGBColor, size=10, bold=True,
    )
    if exhibit.text:
        said = " ".join(exhibit.text.split())
        note = doc.add_paragraph()
        note.paragraph_format.space_after = Pt(6)
        _ink(note.add_run(said[:900] + ("…" if len(said) > 900 else "")),
             Pt, RGBColor, size=9, colour=QUIET)


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
