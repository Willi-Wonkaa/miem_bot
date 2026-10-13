import logging
import re

from .parsing import clean_text


def split_long_text(text, max_chars=1400):
    text = clean_text(text)

    if len(text) <= max_chars:
        return [text]

    sentences = re.split(r"(?<=[.!?])\s+", text)
    parts = []
    current = ""

    for sentence in sentences:
        if not sentence:
            continue

        candidate = f"{current} {sentence}".strip()

        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                parts.append(current)

            if len(sentence) <= max_chars:
                current = sentence
            else:
                for i in range(0, len(sentence), max_chars):
                    parts.append(sentence[i : i + max_chars])
                current = ""

    if current:
        parts.append(current)

    return parts


def build_document_chunks(blocks, meta, max_chars=1600):
    chunks = []
    current_parts = []
    current_section = ""
    chunk_id = 0

    def flush():
        nonlocal current_parts, chunk_id

        if not current_parts:
            return

        text = clean_text("\n".join(current_parts))

        if len(text) >= 80:
            chunks.append(
                {
                    "chunk_id": chunk_id,
                    "document_id": meta["document_id"],
                    "file": meta["file"],
                    "title": meta["title"],
                    "category": meta.get("category", ""),
                    "section": current_section,
                    "text": text,
                }
            )
            chunk_id += 1

        if current_parts:
            overlap = current_parts[-1]
            current_parts = [overlap] if len(overlap) <= 350 else []
        else:
            current_parts = []

    for block in blocks:
        section = block.get("section") or ""
        text = clean_text(block.get("text", ""))

        if not text:
            continue

        if current_section and section and section != current_section:
            flush()
            current_parts = []

        if section:
            current_section = section

        for piece in split_long_text(text):
            candidate = "\n".join(current_parts + [piece])

            if current_parts and len(candidate) > max_chars:
                flush()

            current_parts.append(piece)

    flush()
    logging.getLogger(__name__).info(
        "chunking_completed document=%s chunks=%d", meta["document_id"], len(chunks)
    )
    return chunks
