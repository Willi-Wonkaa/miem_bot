"""Load complete immutable snapshots off the request path; swap references atomically."""

import logging
import threading

from app.rag.indexing import current_generation, make_embedder
from app.rag.retrieval import Retriever

log = logging.getLogger(__name__)


class LiveIndex:
    def __init__(self, settings, embedder=None):
        self.settings = settings
        self.embedder = embedder
        self.current = None
        self.last_error = None
        self.reload_lock = threading.Lock()
        self.model_lock = threading.Lock()

    def model(self):
        with self.model_lock:
            if self.embedder is None:
                self.embedder = make_embedder(self.settings)
            return self.embedder

    def refresh(self):
        if not self.reload_lock.acquire(blocking=False):
            return False
        try:
            try:
                directory = current_generation(self.settings.index_dir)
            except FileNotFoundError:
                return False
            if self.current is not None and directory.name == self.current.generation:
                return False
            retriever = Retriever(self.settings, self.model(), directory=directory)
            # An in-flight request holds its own reference to the old retriever.
            self.current = retriever
            self.last_error = None
            log.info(
                "live_index_switched generation=%s chunks=%d",
                retriever.generation,
                len(retriever.chunks),
            )
            return True
        except Exception as error:
            self.last_error = type(error).__name__
            log.exception("live_index_reload_failed; keeping previous generation")
            return False
        finally:
            self.reload_lock.release()

    def snapshot(self):
        return self.current

    def coverage(self, snapshot=None):
        snapshot = snapshot or self.current
        if snapshot is None:
            return {
                "indexed_files": 0,
                "total_files": 0,
                "complete": False,
                "files": [],
                "generation": None,
            }
        meta = snapshot.metadata
        return {
            key: meta.get(key)
            for key in ["indexed_files", "total_files", "complete", "files", "generation"]
        }
