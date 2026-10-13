"""Progress instrumentation for the full-index release; durable indexing follows separately."""

import logging
import time
import uuid
from collections import Counter

import numpy as np

from app.telemetry import Telemetry, duration

log = logging.getLogger(__name__)


class IndexObserver:
    def __init__(self, settings):
        self.state = Telemetry(settings.monitor_dir)
        self.run_id = uuid.uuid4().hex
        self.started = time.monotonic()
        self.files, self.counts = [], {}
        self.completed = set()
        self.encoded = 0
        self.total_chunks = 0
        self.last_report = 0
        self.state.component("indexer", "running", self.run_id)
        self.report("preparing")

    def prepare(self, root, chunks):
        import json

        manifest_path = next(root.rglob("manifest.json"))
        self.files = [
            row["file"]
            for row in json.loads(manifest_path.read_text(encoding="utf-8"))["documents"]
        ]
        self.counts = Counter(chunk["file"] for chunk in chunks)
        self.total_chunks = len(chunks)
        self.report("embedding")

    def report(self, phase, current_file="", force=False):
        elapsed = time.monotonic() - self.started
        rate = self.encoded / elapsed if elapsed else 0
        eta = (self.total_chunks - self.encoded) / rate if rate else None
        payload = {
            "state": phase,
            "total": len(self.files),
            "done": len(self.completed),
            "remaining": len(self.files) - len(self.completed),
            "failed": 0,
            "percent": self.encoded / self.total_chunks * 100 if self.total_chunks else 0,
            "elapsed": elapsed,
            "eta": eta,
            "current_file": current_file,
            "encoded_chunks": self.encoded,
            "known_chunks": self.total_chunks,
            "published_files": len(self.completed) if phase == "completed" else 0,
            "updated": time.time(),
        }
        self.state.put("index_progress", payload)
        log.info(
            "index_progress phase=%s files=%d/%d chunks=%d/%d percent=%.1f eta=%s",
            phase,
            len(self.completed),
            len(self.files),
            self.encoded,
            self.total_chunks,
            payload["percent"],
            duration(eta),
        )
        if force or time.monotonic() - self.last_report > 30:
            self.state.event(
                "indexer",
                "indexer",
                f"Индексатор: {phase}\n"
                f"Файлы: {payload['done']}/{payload['total']}; осталось {payload['remaining']}\n"
                f"Прогресс: {payload['percent']:.1f}%\nПрошло {duration(elapsed)}; ETA ≈ {duration(eta)}",
                payload,
            )
            self.last_report = time.monotonic()

    def encode(self, embedder, chunks, batch_size):
        from app.rag.indexing import encode_passages, retrieval_text

        vectors = []
        done_counts = Counter()
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            vectors.append(
                encode_passages(embedder, [retrieval_text(x) for x in batch], batch_size)
            )
            self.encoded += len(batch)
            done_counts.update(chunk["file"] for chunk in batch)
            for name, count in done_counts.items():
                if count == self.counts[name] and name not in self.completed:
                    self.completed.add(name)
                    self.state.event("files", "indexer", f"Рассчитаны векторы файла: {name}")
            self.report("embedding", batch[-1]["file"])
        return np.concatenate(vectors).astype("float32")
