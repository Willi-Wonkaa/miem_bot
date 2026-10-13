"""Resumable per-file/per-batch indexing with atomic partial publication."""

import hashlib
import json
import logging
import math
import multiprocessing
import os
import signal
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path

import faiss
import numpy as np

from app.config import Settings
from app.rag.indexing import (
    FORMAT_VERSION,
    current_generation,
    encode_passages,
    encoding_signature,
    fingerprint,
    make_embedder,
    read_faq,
    read_links,
    retrieval_text,
    safe_extract,
)
from app.rag.parser_worker import parse_document
from app.rag.storage import atomic_array, atomic_json, read_json, sync_directory
from app.telemetry import Telemetry, duration

log = logging.getLogger(__name__)


def worker_logging(log_directory):
    from app.logging_setup import setup_logging

    setup_logging(f"parser-{os.getpid()}", Settings(log_dir=Path(log_directory)))


class IncrementalIndexer:
    def __init__(self, settings: Settings, embedder=None):
        self.settings = settings
        self.embedder = embedder
        self.telemetry = Telemetry(settings.monitor_dir)
        self.run_id = uuid.uuid4().hex
        self.started = time.monotonic()
        self.previous_elapsed = 0
        self.documents = []
        self.completed = []
        self.failed = {}
        self.chunks = []
        self.index = None
        self.dimension = 0
        self.links, self.faq = [], []
        self.link_vectors = self.faq_vectors = None
        self.extras_ready = False
        self.published_files = 0
        self.encoded_now = 0
        self.encoding_seconds = 0
        self.encoded_total = 0
        self.parsed = {}
        self.current_file = ""
        self.file_chunks_done = self.file_chunks_total = 0
        self.stop_requested = False
        self.last_notice = 0
        self.state = "preparing"

    def progress(self, notify=False):
        total = len(self.documents)
        done = len(self.completed)
        remaining = max(0, total - done - len(self.failed))
        known = sum(self.parsed.values())
        estimated_chunks = known / len(self.parsed) * total if self.parsed else 0
        rate = self.encoded_now / self.encoding_seconds if self.encoding_seconds else 0
        eta = max(0, estimated_chunks - self.encoded_total) / rate if rate else None
        elapsed = self.previous_elapsed + time.monotonic() - self.started
        payload = {
            "run_id": self.run_id,
            "state": self.state,
            "total": total,
            "done": done,
            "failed": len(self.failed),
            "remaining": remaining,
            "percent": 100 * (done + len(self.failed)) / total if total else 0,
            "elapsed": elapsed,
            "eta": eta,
            "current_file": self.current_file,
            "file_chunks_done": self.file_chunks_done,
            "file_chunks_total": self.file_chunks_total,
            "parsed_files": len(self.parsed),
            "known_chunks": known,
            "encoded_chunks": self.encoded_total,
            "published_files": self.published_files,
            "updated": time.time(),
        }
        self.telemetry.put("index_progress", payload)
        if hasattr(self, "work"):
            atomic_json(self.work / "progress.json", payload)
        if notify or time.monotonic() - self.last_notice >= 30:
            text = (
                f"Индексатор: {self.state}\nФайлы: {done}/{total}, осталось {remaining}, "
                f"ошибок {len(self.failed)} ({payload['percent']:.1f}%)\n"
                f"Прошло {duration(elapsed)}; ETA ≈ {duration(eta)}\n"
                f"Файл: {self.current_file or '—'}; чанки {self.file_chunks_done}/{self.file_chunks_total}"
            )
            self.telemetry.event("indexer", "indexer", text, payload)
            self.last_notice = time.monotonic()
        log.info(
            "index_progress state=%s files=%d/%d failed=%d percent=%.1f chunks=%d elapsed=%.1f eta=%s",
            self.state,
            done,
            total,
            len(self.failed),
            payload["percent"],
            self.encoded_total,
            elapsed,
            round(eta) if eta is not None else "unknown",
        )
        return payload

    def model(self):
        if self.embedder is None:
            self.telemetry.component("indexer-model", "starting", self.run_id)
            self.embedder = make_embedder(self.settings)
            self.telemetry.component("indexer-model", "ready", self.run_id)
        return self.embedder

    def checkpoint(self, meta, path):
        digest = hashlib.sha256(encoding_signature(self.settings).encode())
        digest.update(
            json.dumps(
                {key: meta.get(key, "") for key in ["document_id", "file", "title", "category"]},
                sort_keys=True,
            ).encode()
        )
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        directory = self.settings.index_dir / "checkpoints" / digest.hexdigest()
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def encode_checkpoint(self, texts, directory, auxiliary=False):
        final = directory / "vectors.npy"
        if final.exists():
            try:
                vectors = np.load(final, mmap_mode="r", allow_pickle=False)
                if vectors.ndim == 2 and len(vectors) == len(texts) and np.isfinite(vectors).all():
                    self.dimension = vectors.shape[1]
                    if not auxiliary:
                        self.encoded_total += len(texts)
                    self.file_chunks_done = len(texts)
                    return vectors
            except (OSError, ValueError):
                pass
        batches = []
        self.file_chunks_total = len(texts)
        for start in range(0, len(texts), self.settings.embed_batch_size):
            if self.stop_requested:
                raise InterruptedError("Indexing stop requested")
            batch = texts[start : start + self.settings.embed_batch_size]
            path = directory / f"batch-{start:08d}.npy"
            vectors = None
            if path.exists():
                try:
                    vectors = np.load(path, mmap_mode="r", allow_pickle=False)
                    if (
                        len(vectors) != len(batch)
                        or vectors.ndim != 2
                        or not np.isfinite(vectors).all()
                    ):
                        vectors = None
                except (OSError, ValueError):
                    pass
            if vectors is None:
                before = time.monotonic()
                vectors = encode_passages(self.model(), batch, self.settings.embed_batch_size)
                if not auxiliary:
                    self.encoding_seconds += time.monotonic() - before
                    self.encoded_now += len(batch)
                atomic_array(path, vectors)
            self.dimension = vectors.shape[1]
            batches.append(path)
            if not auxiliary:
                self.encoded_total += len(batch)
            self.file_chunks_done = start + len(batch)
            self.progress()
        temporary = directory / "vectors.pending.npy"
        target = np.lib.format.open_memmap(
            temporary, mode="w+", dtype="float32", shape=(len(texts), self.dimension)
        )
        offset = 0
        for path in batches:
            values = np.load(path, mmap_mode="r", allow_pickle=False)
            target[offset : offset + len(values)] = values
            offset += len(values)
        target.flush()
        del target
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, final)
        sync_directory(directory)
        return np.load(final, mmap_mode="r", allow_pickle=False)

    def add_document(self, meta, checkpoint):
        chunks = read_json(checkpoint / "chunks.json")
        self.current_file = meta["file"]
        self.file_chunks_done = 0
        self.file_chunks_total = len(chunks)
        self.state = "indexing"
        before_encoded = self.encoded_now
        vectors = self.encode_checkpoint([retrieval_text(chunk) for chunk in chunks], checkpoint)
        if self.index is None:
            self.index = faiss.IndexFlatIP(self.dimension)
        self.index.add(vectors)
        self.chunks.extend(chunks)
        self.completed.append(meta)
        atomic_json(checkpoint / "complete.json", {"chunks": len(chunks), "completed": time.time()})
        cached = self.encoded_now == before_encoded
        label = "Восстановлен из сохранённого прогресса" if cached else "Проиндексирован файл"
        self.telemetry.event(
            "files",
            "indexer",
            f"{label}: {meta['file']}\nФайлы: {len(self.completed)}/{len(self.documents)}",
            {"file": meta["file"]},
        )
        log.info(
            "file_indexed file=%s chunks=%d cached=%s",
            meta["file"],
            len(chunks),
            cached,
        )
        self.progress(notify=True)
        threshold = max(
            1, math.ceil(len(self.documents) * self.settings.index_publish_percent / 100)
        )
        if len(self.completed) == 1 or len(self.completed) - self.published_files >= threshold:
            self.publish()

    def extras(self, root):
        for label, rows, key in [
            ("links", read_links(root), "description"),
            ("faq", read_faq(self.settings.faq_path), "question"),
        ]:
            self.current_file = "all_links.txt" if label == "links" else self.settings.faq_path.name
            self.state = "extras"
            digest = hashlib.sha256(
                (
                    encoding_signature(self.settings) + label + json.dumps(rows, ensure_ascii=False)
                ).encode()
            ).hexdigest()
            directory = self.settings.index_dir / "checkpoints" / digest
            directory.mkdir(parents=True, exist_ok=True)
            vectors = (
                self.encode_checkpoint([row[key] for row in rows], directory, auxiliary=True)
                if rows
                else np.empty((0, self.dimension), dtype="float32")
            )
            if label == "links":
                self.links, self.link_vectors = rows, vectors
            else:
                self.faq, self.faq_vectors = rows, vectors
        self.extras_ready = True
        self.publish()

    def publish(self, complete=False):
        if self.index is None:
            return None
        generation = uuid.uuid4().hex
        directory = self.settings.index_dir / generation
        directory.mkdir()
        faiss.write_index(self.index, str(directory / "documents.faiss"))
        with (directory / "documents.faiss").open("rb") as stream:
            os.fsync(stream.fileno())
        atomic_json(directory / "chunks.json", self.chunks)
        # Static FAQ/link arrays are shared by hard links between snapshots.
        extra_key = hashlib.sha256(
            f"{self.build_hash}:{len(self.links)}:{len(self.faq)}:{self.dimension}".encode()
        ).hexdigest()
        extra_path = self.settings.index_dir / "extras" / f"{extra_key}.npz"
        if not extra_path.exists():
            extra_path.parent.mkdir(parents=True, exist_ok=True)
            with extra_path.open("wb") as stream:
                empty = np.empty((0, self.dimension), dtype="float32")
                np.savez(
                    stream,
                    links=self.link_vectors if self.link_vectors is not None else empty,
                    faq=self.faq_vectors if self.faq_vectors is not None else empty,
                )
                stream.flush()
                os.fsync(stream.fileno())
        try:
            os.link(extra_path, directory / "extras.npz")
        except OSError:
            import shutil

            shutil.copyfile(extra_path, directory / "extras.npz")
        for label, rows in [("links", self.links), ("faq", self.faq)]:
            cached_json = extra_path.with_suffix(f".{label}.json")
            if not cached_json.exists():
                atomic_json(cached_json, rows)
            try:
                os.link(cached_json, directory / f"{label}.json")
            except OSError:
                import shutil

                shutil.copyfile(cached_json, directory / f"{label}.json")
        files = [meta["file"] for meta in self.completed]
        metadata = {
            "format_version": FORMAT_VERSION,
            "build_hash": self.build_hash,
            "embed_model": self.settings.embed_model,
            "quantized": self.settings.embed_quantize and self.settings.embed_device == "cpu",
            "encoding_signature": encoding_signature(self.settings),
            "generation": generation,
            "chunks": len(self.chunks),
            "links": len(self.links),
            "faq": len(self.faq),
            "total_files": len(self.documents),
            "indexed_files": len(files),
            "files": files,
            "complete": complete,
            "published": time.time(),
            "failed": self.failed,
        }
        atomic_json(directory / "metadata.json", metadata)
        sync_directory(directory)
        # Publish only after all files of the immutable generation are durable.
        pointer = self.settings.index_dir / f"CURRENT.{generation}.tmp"
        with pointer.open("w", encoding="utf-8") as stream:
            stream.write(generation)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pointer, self.settings.index_dir / "CURRENT")
        sync_directory(self.settings.index_dir)
        self.published_files = len(files)
        self.telemetry.put("index_publication", metadata)
        self.telemetry.event(
            "indexer",
            "indexer",
            f"Рабочая база обновлена: {len(files)}/"
            f"{len(self.documents)} файлов; {len(self.chunks)} чанков.",
            metadata,
        )
        log.info(
            "index_published generation=%s files=%d chunks=%d complete=%s",
            generation,
            len(files),
            len(self.chunks),
            complete,
        )
        self.progress()
        return directory

    def run(self):
        if not self.settings.kb_zip.is_file():
            raise FileNotFoundError(f"Place knowledge base archive at {self.settings.kb_zip}")
        self.build_hash = fingerprint(self.settings)
        self.work = self.settings.index_dir / "work" / self.build_hash
        self.work.mkdir(parents=True, exist_ok=True)
        previous = read_json(self.work / "progress.json", {})
        self.previous_elapsed = previous.get("elapsed", 0)
        try:
            directory = current_generation(self.settings.index_dir)
            metadata = read_json(directory / "metadata.json", {})
            if (
                metadata.get("build_hash") == self.build_hash
                and metadata.get("complete")
                and all(
                    (directory / name).exists()
                    for name in [
                        "documents.faiss",
                        "chunks.json",
                        "links.json",
                        "faq.json",
                        "extras.npz",
                    ]
                )
            ):
                log.info("index_reused generation=%s", directory.name)
                return directory
        except (OSError, ValueError):
            pass
        root = self.work / "source"
        if not (root / ".unpacked").exists():
            root.mkdir(parents=True, exist_ok=True)
            safe_extract(self.settings.kb_zip, root)
            atomic_json(root / ".unpacked", {"archive": self.build_hash})
        manifests = list(root.rglob("manifest.json"))
        if len(manifests) != 1:
            raise ValueError("Knowledge base must contain exactly one manifest.json")
        manifest_path = manifests[0]
        self.documents = read_json(manifest_path)["documents"]
        if not self.documents:
            raise ValueError("Knowledge base is empty")
        if len({meta["document_id"] for meta in self.documents}) != len(self.documents):
            raise ValueError("Duplicate document_id in manifest")
        jobs = []
        for meta in self.documents:
            path = (manifest_path.parent / meta["file"]).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                raise ValueError(f"Missing or unsafe document: {meta['file']}")
            checkpoint = self.checkpoint(meta, path)
            jobs.append((meta, path, checkpoint))
        # Small documents become searchable first. Parsing never loads an embedding model.
        jobs.sort(key=lambda job: (job[1].suffix.lower() != ".txt", job[1].stat().st_size))
        self.telemetry.component("indexer", "running", self.run_id)
        self.progress(notify=True)
        old_handler = None
        if multiprocessing.current_process().name == "MainProcess":
            import threading

            if threading.current_thread() is threading.main_thread():
                old_handler = signal.signal(
                    signal.SIGTERM, lambda *_: setattr(self, "stop_requested", True)
                )
        pool = ProcessPoolExecutor(
            max_workers=self.settings.index_parse_workers,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=worker_logging,
            initargs=(str(self.settings.log_dir),),
        )
        futures = {
            pool.submit(parse_document, str(path), meta, str(checkpoint)): (meta, checkpoint)
            for meta, path, checkpoint in jobs
        }
        try:
            while futures:
                if self.stop_requested:
                    raise InterruptedError("Indexing stop requested")
                ready, _ = wait(futures, timeout=1, return_when=FIRST_COMPLETED)
                if not ready:
                    self.progress()
                    continue
                # Register available parse counts so ETA converges to the real total.
                for future, (meta, _) in futures.items():
                    if future.done() and not future.exception():
                        self.parsed[meta["document_id"]] = future.result()
                selected = min(
                    ready, key=lambda f: self.parsed.get(futures[f][0]["document_id"], 0)
                )
                meta, checkpoint = futures.pop(selected)
                try:
                    selected.result()
                    self.add_document(meta, checkpoint)
                except InterruptedError:
                    raise
                except Exception as error:
                    self.failed[meta["file"]] = type(error).__name__
                    self.telemetry.event(
                        "errors",
                        "indexer",
                        f"Ошибка индексации файла: {meta['file']} "
                        f"({type(error).__name__}); повторный запуск восстановит прогресс.",
                    )
                    log.exception("file_indexing_failed file=%s", meta["file"])
                    self.progress(notify=True)
                if len(self.completed) == 1 and not self.extras_ready:
                    self.extras(root)
            if self.index is None:
                raise ValueError("No document could be indexed")
            self.state = "completed" if not self.failed else "completed_with_errors"
            self.current_file = ""
            directory = self.publish(complete=not self.failed and self.extras_ready)
            self.progress(notify=True)
            self.telemetry.component("indexer", self.state, self.run_id)
            return directory
        except BaseException:
            self.state = "interrupted"
            self.publish()
            self.progress(notify=True)
            self.telemetry.component("indexer", "interrupted", self.run_id)
            raise
        finally:
            pool.shutdown(wait=not self.stop_requested, cancel_futures=True)
            if old_handler is not None:
                signal.signal(signal.SIGTERM, old_handler)
