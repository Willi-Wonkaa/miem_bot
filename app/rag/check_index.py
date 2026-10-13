"""Validate persisted artifacts before deploy without loading E5 or rebuilding anything."""

import json
import sys
from pathlib import Path

from app.config import Settings
from app.rag.indexing import FORMAT_VERSION, current_generation, fingerprint


def validate_index(settings: Settings) -> Path:
    try:
        directory = current_generation(settings.index_dir)
        metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        if metadata["format_version"] != FORMAT_VERSION:
            raise ValueError("Unsupported index format")
        if metadata["embed_model"] != settings.embed_model:
            raise ValueError("Embedding model changed")
        if metadata["build_hash"] != fingerprint(settings):
            raise ValueError("Knowledge base or indexing code changed")
        for name in ["documents.faiss", "chunks.json", "links.json", "faq.json", "extras.npz"]:
            if not (directory / name).is_file():
                raise ValueError(f"Missing artifact: {name}")
    except (OSError, ValueError, KeyError) as error:
        raise ValueError(
            "Index is not ready or is outdated. Run indexing explicitly before deployment."
        ) from error
    return directory


if __name__ == "__main__":
    try:
        print("Validated index:", validate_index(Settings()).name)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
