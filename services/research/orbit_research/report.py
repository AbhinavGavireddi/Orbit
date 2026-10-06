import hashlib
import html
import io
import re
import uuid
from dataclasses import dataclass
from datetime import date
from functools import partial
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer
from pypdf import PdfReader
import reportlab


@dataclass(frozen=True)
class Source:
    title: str
    url: str


@dataclass(frozen=True)
class Report:
    title: str
    body: str
    sources: list[Source]

    def markdown(self):
        refs = "\n".join(f"{n}. [{s.title}]({s.url})" for n, s in enumerate(self.sources, 1))
        return f"# {self.title}\n\n*Orbit research · {date.today().isoformat()}*\n\n{self.body}\n\n## Sources\n\n{refs}\n"


def from_response(data, title):
    sources, body = [], []
    for item in data.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if part.get("type") != "output_text":
                continue
            text = part["text"]
            insertions = []
            for citation in part.get("annotations", []):
                if citation.get("type") != "url_citation":
                    continue
                url = citation.get("url", "")
                parsed = urlsplit(url)
                if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username:
                    continue
                source = Source(citation.get("title", parsed.hostname)[:180], url)
                existing = next((i for i, s in enumerate(sources) if s.url == url), None)
                if existing is None:
                    if len(sources) >= 10:
                        continue
                    sources.append(source)
                    existing = len(sources) - 1
                end = min(len(text), max(0, citation.get("end_index", len(text))))
                insertions.append((end, f" [{existing + 1}]({url})"))
            for position, citation in sorted(insertions, reverse=True):
                text = text[:position] + citation + text[position:]
            text = re.sub(r"cite.*?", "", text)
            body.append(text)
    if len(sources) < 2 or not any(x.get("type") == "web_search_call" for x in data.get("output", [])):
        raise ValueError("Research returned insufficient traceable web sources")
    text = "\n\n".join(body)
    if len(text.split()) > 1400 or len(text) > 22000:
        raise ValueError("Report exceeds the concise report budget; narrow the topic")
    return Report(title[:160], text, sources)


class ArtifactStorage(Protocol):
    def put(self, task_id: str, filename: str, content: bytes) -> dict: ...


class LocalArtifacts:
    """Local Compose volume adapter; replace for a multi-host object store."""
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def put(self, task_id, filename, content):
        aid = str(uuid.uuid5(uuid.NAMESPACE_URL, "orbit:" + task_id + ":" + filename))
        path = self.root / aid
        try:
            with path.open("xb") as handle:
                handle.write(content)
        except FileExistsError:
            if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(content).digest():
                raise ValueError("Artifact ID already contains different content")
        return {"artifact_id": aid, "filename": filename,
                "media_type": "application/pdf" if filename.endswith(".pdf") else "text/markdown"}


def safe_markup(text):
    # Parse only a small Markdown subset; no model-provided HTML or external images.
    pieces, pos = [], 0
    for match in re.finditer(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", text):
        pieces.append(html.escape(text[pos:match.start()]))
        pieces.append(f'<link href="{html.escape(match[2], quote=True)}" color="#256f89">{html.escape(match[1])}</link>')
        pos = match.end()
    pieces.append(html.escape(text[pos:]))
    return "".join(pieces).replace("**", "")


def render_pdf(report):
    font_path = Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"
    if "OrbitVera" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("OrbitVera", str(font_path)))
    output = io.BytesIO()
    doc = SimpleDocTemplate(output, pagesize=(612, 792), rightMargin=54, leftMargin=54,
                            topMargin=60, bottomMargin=55, title=report.title, author="Orbit", invariant=1)
    body = ParagraphStyle("Body", fontName="OrbitVera", fontSize=10, leading=15, spaceAfter=9,
                          textColor=colors.HexColor("#25343c"), alignment=TA_LEFT)
    heading = ParagraphStyle("Heading", parent=body, fontSize=15, leading=20, spaceBefore=12, spaceAfter=10)
    title = ParagraphStyle("Title", parent=heading, fontSize=25, leading=31)
    small = ParagraphStyle("Small", parent=body, fontSize=8, leading=12)
    story = [Paragraph("ORBIT / RESEARCH", small), Paragraph(html.escape(report.title), title),
             Paragraph(date.today().isoformat(), small), Spacer(1, 0.15 * inch)]
    for paragraph in re.split(r"\n\s*\n", report.body):
        lines = paragraph.strip().splitlines()
        for line in lines:
            if line.startswith("#"):
                story.append(Paragraph(safe_markup(line.lstrip("# ")), heading))
            elif line.strip():
                story.append(Paragraph(safe_markup(line.lstrip("- ")), body))
    story += [PageBreak(), Paragraph("Sources & verification", heading),
              Paragraph("Links below are citations returned by the web research provider. Check the original publications for decisions that depend on exact details.", body)]
    for number, source in enumerate(report.sources, 1):
        label = f"{number}. {html.escape(source.title)}"
        story += [Paragraph(label, body), Paragraph(
            f'<link href="{html.escape(source.url, quote=True)}" color="#256f89">{html.escape(urlsplit(source.url).netloc)} — Open source</link>', small), Spacer(1, 8)]

    def footer(canvas, document):
        canvas.setFont("OrbitVera", 8)
        canvas.setFillColor(colors.HexColor("#657680"))
        canvas.drawString(54, 31, "Orbit · Source-linked research")
        canvas.drawRightString(558, 31, str(document.page))

    doc.build(story, onFirstPage=footer, onLaterPages=footer, canvasmaker=partial(Canvas, invariant=1))
    value = output.getvalue()
    if not 2 <= len(PdfReader(io.BytesIO(value)).pages) <= 4:
        raise ValueError("Report exceeded the 2–4 page layout budget; narrow the topic")
    return value
