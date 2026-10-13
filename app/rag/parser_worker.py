"""CPU parsing workers do not import or copy the embedding model."""

import logging
from pathlib import Path

from app.rag.chunking import build_document_chunks
from app.rag.parsing import extract_blocks
from app.rag.storage import atomic_json, read_json

log = logging.getLogger(__name__)


def parse_document(path, meta, checkpoint):
    checkpoint = Path(checkpoint)
    cached = read_json(checkpoint / "chunks.json")
    if cached is not None:
        return len(cached)
    log.info("parse_started file=%s", meta["file"])
    chunks = build_document_chunks(extract_blocks(Path(path)), meta)
    # A short valid document must still be searchable, even when below the chunker's minimum.
    if not chunks:
        blocks = extract_blocks(Path(path))
        text = "\n".join(block["text"] for block in blocks).strip()
        if text:
            chunks = [
                {
                    "chunk_id": 0,
                    "document_id": meta["document_id"],
                    "file": meta["file"],
                    "title": meta["title"],
                    "category": meta.get("category", ""),
                    "section": "",
                    "text": text,
                }
            ]
    if not chunks:
        raise ValueError(f"No extractable text in {meta['file']}")
    atomic_json(checkpoint / "chunks.json", chunks)
    log.info("parse_completed file=%s chunks=%d", meta["file"], len(chunks))
    return len(chunks)
