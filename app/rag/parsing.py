import logging
import re
from pathlib import Path

from docx import Document
from docx.document import Document as _Document
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader


def clean_text(text):
    if text is None:
        return ""

    text = str(text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

    noise_patterns = [
        r"Мы используем файлы cookie.*",
        r"Настройки файлов cookie.*",
        r"Нашли опечатку\?.*",
        r"Ctrl\s*\+\s*Enter.*",
        r"Javascript.*",
    ]

    for pattern in noise_patterns:
        text = re.sub(pattern, " ", text, flags=re.I)

    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def iter_docx_blocks(parent):
    if isinstance(parent, _Document):
        parent_elm = parent.element.body
    else:
        parent_elm = parent._tc

    for child in parent_elm.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)


def table_to_blocks(table, section):
    rows = []

    for row in table.rows:
        values = [clean_text(cell.text) for cell in row.cells]
        if any(values):
            rows.append(values)

    if not rows:
        return []

    header = rows[0]
    result = []

    for row in rows[1:] if len(rows) > 1 else rows:
        pairs = []

        for i, value in enumerate(row):
            if not value:
                continue

            if i < len(header) and header[i] and header[i] != value:
                pairs.append(f"{header[i]}: {value}")
            else:
                pairs.append(value)

        text = " | ".join(pairs)

        if text:
            result.append(
                {
                    "section": section,
                    "kind": "table",
                    "text": text,
                }
            )

    return result


def extract_docx_blocks(path):
    doc = Document(path)
    blocks = []
    section = ""

    for item in iter_docx_blocks(doc):
        if isinstance(item, Paragraph):
            text = clean_text(item.text)

            if not text:
                continue

            style_name = ""
            try:
                style_name = (item.style.name or "").lower()
            except Exception:
                pass

            is_heading = "heading" in style_name or "заголов" in style_name

            if is_heading:
                section = text

            blocks.append(
                {
                    "section": section,
                    "kind": "heading" if is_heading else "paragraph",
                    "text": text,
                }
            )

        elif isinstance(item, Table):
            blocks.extend(table_to_blocks(item, section))

    return blocks


def extract_txt_blocks(path):
    raw = Path(path).read_text(
        encoding="utf-8",
        errors="ignore",
    )
    raw = clean_text(raw)

    blocks = []
    section = ""
    buffer = []

    def flush():
        nonlocal buffer
        if buffer:
            text = clean_text(" ".join(buffer))
            if text:
                blocks.append(
                    {
                        "section": section,
                        "kind": "paragraph",
                        "text": text,
                    }
                )
            buffer = []

    for line in raw.splitlines():
        line = clean_text(line)

        if not line:
            flush()
            continue

        if re.match(r"^#{1,6}\s+", line):
            flush()
            section = re.sub(r"^#{1,6}\s+", "", line).strip()
            blocks.append(
                {
                    "section": section,
                    "kind": "heading",
                    "text": section,
                }
            )
        else:
            buffer.append(line)

    flush()
    return blocks


def extract_pdf_blocks(path):
    reader = PdfReader(path)
    blocks = []

    for page_num, page in enumerate(reader.pages, start=1):
        text = clean_text(page.extract_text() or "")
        if not text:
            continue

        blocks.append(
            {
                "section": f"Страница {page_num}",
                "kind": "page",
                "text": text,
            }
        )

    return blocks


def extract_blocks(path):
    logging.getLogger(__name__).info("document_parse file=%s", Path(path).name)
    ext = Path(path).suffix.lower()

    if ext == ".docx":
        return extract_docx_blocks(path)
    if ext == ".txt":
        return extract_txt_blocks(path)
    if ext == ".pdf":
        return extract_pdf_blocks(path)

    return []
