"""Resumable per-file/per-batch indexing with atomic partial publication."""

import hashlib
import json
import logging
import math
import multiprocessing
import os
import signal
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from pathlib import Path
from queue import Empty, Queue

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
        self.stop_requested = False
        self.last_notice = 0
        self.state = "preparing"
        self.progress_lock = threading.RLock()
        self.model_lock = threading.Lock()
        self.workers = {}
        self.total_bytes = 0
        self.formats = {}
        self.aux_total = 0
        self.aux_encoded = 0
        self.indexing_started = None
        self.initial_eta = None
        self.analysis_complete = False
        self.cached_units = 0
        self.resume_generation = None
        self.resume_published_files = 0
        self.reused_complete = False

    def progress(self, notify=False):
        with self.progress_lock:
            return self._progress(notify)

    def _progress(self, notify=False):
        total = len(self.documents)
        done = len(self.completed)
        remaining = max(0, total - done - len(self.failed))
        known = sum(self.parsed.values())
        units = known + self.aux_total
        processed_units = self.encoded_total + self.aux_encoded
        active_elapsed = time.monotonic() - self.indexing_started if self.indexing_started else 0
        rate = self.encoded_now / active_elapsed if self.encoded_now and active_elapsed > 5 else 0
        estimate_rate = rate or self.settings.index_estimated_chunks_per_second
        cached_remaining = max(0, self.cached_units - (processed_units - self.encoded_now))
        eta = (
            max(0, units - processed_units - cached_remaining) / estimate_rate
            if self.analysis_complete
            else None
        )
        if self.state in {"completed", "completed_with_errors"}:
            eta = 0
        elif self.state == "interrupted":
            eta = None
        elapsed = self.previous_elapsed + time.monotonic() - self.started
        payload = {
            "run_id": self.run_id,
            "start_id": self.settings.index_start_id,
            "build_hash": getattr(self, "build_hash", None),
            "state": self.state,
            "total": total,
            "done": done,
            "failed": len(self.failed),
            "remaining": remaining,
            "percent": min(100, 100 * processed_units / units) if units else 0,
            "elapsed": elapsed,
            "eta": eta,
            "parsed_files": total if self.reused_complete else len(self.parsed),
            "known_chunks": known,
            "encoded_chunks": self.encoded_total,
            "total_units": units,
            "processed_units": processed_units,
            "total_bytes": self.total_bytes,
            "formats": self.formats,
            "aux_total": self.aux_total,
            "aux_encoded": self.aux_encoded,
            "faq_total": len(getattr(self, "planned_faq", self.faq)),
            "links_total": len(getattr(self, "planned_links", self.links)),
            "initial_eta": self.initial_eta,
            "analysis_complete": self.analysis_complete,
            "reused_complete": self.reused_complete,
            "workers": {key: dict(value) for key, value in self.workers.items()},
            "workers_total": len(self.workers),
            "workers_ready": sum(worker["done"] > 0 for worker in self.workers.values()),
            "startup_ready": bool(self.workers)
            and all(worker["done"] > 0 for worker in self.workers.values()),
            "published_files": self.published_files,
            "updated": time.time(),
        }
        self.telemetry.put("index_progress", payload)
        if hasattr(self, "work"):
            atomic_json(self.work / "progress.json", payload)
        if notify:
            text = (
                f"Индексация: {self.state}\nФайлы: {done}/{total}, осталось {remaining}, "
                f"ошибок {len(self.failed)} ({payload['percent']:.1f}%)\n"
                f"Общий объём: {units} фрагментов и дополнительных записей\n"
                f"Прошло {duration(elapsed)}; осталось ≈ {duration(eta)}"
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
        with self.model_lock:
            if self.embedder is None:
                self.telemetry.component("indexer-model", "starting", self.run_id)
                self.embedder = make_embedder(self.settings)
                self.telemetry.component("indexer-model", "ready", self.run_id)
            return self.embedder

    def encoded(self, count, auxiliary=False, computed=False, seconds=0):
        with self.progress_lock:
            if auxiliary:
                self.aux_encoded += count
            else:
                self.encoded_total += count
            if computed:
                self.encoded_now += count
                self.encoding_seconds += seconds
            self.progress()

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
                    self.encoded(len(texts), auxiliary)
                    return vectors
            except (OSError, ValueError):
                pass
        batches = []
        batch_size = max(1, self.settings.embed_batch_size // max(1, len(self.workers)))
        dimension = 0
        for start in range(0, len(texts), batch_size):
            if self.stop_requested:
                raise InterruptedError("Indexing stop requested")
            batch = texts[start : start + batch_size]
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
                model = self.model()
                if self.indexing_started is None:
                    self.indexing_started = time.monotonic()
                before = time.monotonic()
                vectors = encode_passages(model, batch, batch_size)
                seconds = time.monotonic() - before
                atomic_array(path, vectors)
                self.encoded(len(batch), auxiliary, computed=True, seconds=seconds)
            else:
                self.encoded(len(batch), auxiliary)
            self.dimension = vectors.shape[1]
            dimension = vectors.shape[1]
            batches.append(path)
        temporary = directory / "vectors.pending.npy"
        target = np.lib.format.open_memmap(
            temporary, mode="w+", dtype="float32", shape=(len(texts), dimension)
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

    def add_document(self, meta, checkpoint, vectors=None, worker_id=None, cached=None):
        chunks = read_json(checkpoint / "chunks.json")
        before_encoded = self.encoded_now
        if vectors is None:
            vectors = self.encode_checkpoint(
                [retrieval_text(chunk) for chunk in chunks], checkpoint
            )
        if self.index is None:
            self.index = faiss.IndexFlatIP(self.dimension)
        self.index.add(vectors)
        self.chunks.extend(chunks)
        self.completed.append(meta)
        if worker_id is not None:
            with self.progress_lock:
                self.workers[worker_id]["done"] += 1
        atomic_json(checkpoint / "complete.json", {"chunks": len(chunks), "completed": time.time()})
        if cached is None:
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
        self.progress()
        threshold = max(
            1, math.ceil(len(self.documents) * self.settings.index_publish_percent / 100)
        )
        if (
            len(self.completed) <= len(self.workers)
            or len(self.completed) == 1
            or len(self.completed) - self.published_files >= threshold
        ):
            self.publish()

    def extras(self, root):
        for label, rows, key in [
            ("links", self.planned_links, "description"),
            ("faq", self.planned_faq, "question"),
        ]:
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
        if (
            self.resume_generation
            and not complete
            and len(self.completed) < self.resume_published_files
        ):
            return self.resume_generation
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
            "total_bytes": self.total_bytes,
            "formats": self.formats,
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
        log.info(
            "index_published generation=%s files=%d chunks=%d complete=%s",
            generation,
            len(files),
            len(self.chunks),
            complete,
        )
        self.progress()
        return directory

    def file_error(self, meta, error):
        self.failed[meta["file"]] = type(error).__name__
        self.telemetry.event(
            "errors",
            "indexer",
            f"Не удалось обработать файл: {meta['file']} ({type(error).__name__}). "
            "Сохранённый прогресс остальных файлов доступен.",
        )
        log.error("file_indexing_failed file=%s error=%s", meta["file"], type(error).__name__)

    def analyse(self, root, jobs, pool):
        self.state = "analyzing"
        self.total_bytes = sum(path.stat().st_size for _, path, _ in jobs)
        self.formats = dict(Counter(path.suffix.lower().lstrip(".") for _, path, _ in jobs))
        self.progress()
        futures = {
            pool.submit(parse_document, str(path), meta, str(checkpoint)): meta
            for meta, path, checkpoint in jobs
        }
        while futures:
            if self.stop_requested:
                raise InterruptedError("Indexing stop requested")
            ready, _ = wait(futures, timeout=1, return_when=FIRST_COMPLETED)
            for future in ready:
                meta = futures.pop(future)
                try:
                    self.parsed[meta["document_id"]] = future.result()
                except Exception as error:
                    self.file_error(meta, error)
            self.progress()
        self.planned_links = read_links(root)
        self.planned_faq = read_faq(self.settings.faq_path)
        self.aux_total = len(self.planned_links) + len(self.planned_faq)
        self.analysis_complete = True
        for meta, _, checkpoint in jobs:
            count = self.parsed.get(meta["document_id"], 0)
            if count and self.cached_vectors(checkpoint / "vectors.npy", count):
                self.cached_units += count
        for label, rows in [("links", self.planned_links), ("faq", self.planned_faq)]:
            digest = hashlib.sha256(
                (
                    encoding_signature(self.settings) + label + json.dumps(rows, ensure_ascii=False)
                ).encode()
            ).hexdigest()
            if rows and self.cached_vectors(
                self.settings.index_dir / "checkpoints" / digest / "vectors.npy", len(rows)
            ):
                self.cached_units += len(rows)
        self.initial_eta = (
            sum(self.parsed.values()) + self.aux_total - self.cached_units
        ) / self.settings.index_estimated_chunks_per_second
        jobs = [job for job in jobs if job[0]["file"] not in self.failed]
        if not jobs:
            raise ValueError("No document could be parsed")
        self.workers = {
            str(index + 1): {"state": "starting", "done": 0}
            for index in range(min(self.settings.index_workers, len(jobs)))
        }
        self.state = "indexing"
        self.progress()
        self.telemetry.event(
            "indexer",
            "indexer",
            f"Корпус проанализирован.\nДокументов: {len(self.documents)}; "
            f"объём: {self.total_bytes / 1024**2:.1f} МБ\n"
            f"Текстовых фрагментов: {sum(self.parsed.values())}; "
            f"FAQ: {len(self.planned_faq)}; ссылок: {len(self.planned_links)}\n"
            f"Ориентировочное время: {duration(self.initial_eta)}\n"
            f"Индексаторов: {len(self.workers)}. Файлы обрабатываются от меньших к большим.",
        )
        return jobs

    @staticmethod
    def cached_vectors(path, count):
        try:
            vectors = np.load(path, mmap_mode="r", allow_pickle=False)
            return vectors.ndim == 2 and len(vectors) == count and np.isfinite(vectors).all()
        except (OSError, ValueError):
            return False

    def encode_worker(self, worker_id, pending, results, first_job):
        job = first_job
        while job is not None and not self.stop_requested:
            meta, _, checkpoint = job
            with self.progress_lock:
                self.workers[worker_id]["state"] = "running"
            try:
                chunks = read_json(checkpoint / "chunks.json")
                cached = self.cached_vectors(checkpoint / "vectors.npy", len(chunks))
                vectors = self.encode_checkpoint(
                    [retrieval_text(chunk) for chunk in chunks], checkpoint
                )
                results.put((meta, checkpoint, vectors, worker_id, None, cached))
            except Exception as error:
                log.exception("file_encoding_failed file=%s", meta["file"])
                results.put((meta, checkpoint, None, worker_id, error, False))
                if isinstance(error, InterruptedError):
                    break
            try:
                job = pending.get_nowait()
            except Empty:
                job = None
        with self.progress_lock:
            self.workers[worker_id]["state"] = "idle" if not self.stop_requested else "stopped"

    def parallel_index(self, jobs):
        pending, results = Queue(), Queue()
        count = len(self.workers)
        for job in jobs[count:]:
            pending.put(job)
        pool = ThreadPoolExecutor(max_workers=count, thread_name_prefix="indexer")
        tasks = [
            pool.submit(self.encode_worker, str(index + 1), pending, results, jobs[index])
            for index in range(count)
        ]
        received = 0
        announced = False
        try:
            while received < len(jobs):
                if self.stop_requested:
                    raise InterruptedError("Indexing stop requested")
                try:
                    meta, checkpoint, vectors, worker_id, error, cached = results.get(timeout=1)
                except Empty:
                    for task in tasks:
                        if task.done():
                            task.result()
                    self.progress()
                    continue
                received += 1
                if error is not None:
                    self.file_error(meta, error)
                else:
                    self.add_document(meta, checkpoint, vectors, worker_id, cached)
                if not announced and all(worker["done"] > 0 for worker in self.workers.values()):
                    announced = True
                    self.telemetry.event(
                        "indexer",
                        "indexer",
                        "Все индексаторы успешно начали обработку. "
                        "Индексация продолжается в фоне; общий прогресс доступен по /index.",
                    )
                self.progress()
        finally:
            self.stop_requested = self.stop_requested or received < len(jobs)
            pool.shutdown(wait=True, cancel_futures=True)

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
            if metadata.get("build_hash") == self.build_hash:
                self.resume_generation = directory
                self.resume_published_files = metadata.get("indexed_files", 0)
                self.published_files = self.resume_published_files
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
                self.documents = [{"file": name} for name in metadata.get("files", [])]
                self.completed = list(self.documents)
                self.parsed = {"cached": metadata["chunks"]}
                self.encoded_total = metadata["chunks"]
                self.aux_total = metadata.get("faq", 0) + metadata.get("links", 0)
                self.aux_encoded = self.aux_total
                self.analysis_complete = True
                self.reused_complete = True
                self.state = "completed"
                self.published_files = len(self.documents)
                self.total_bytes = metadata.get("total_bytes", previous.get("total_bytes", 0))
                self.formats = metadata.get("formats", previous.get("formats", {}))
                self.telemetry.put("index_publication", metadata)
                self.progress()
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
        jobs.sort(key=lambda job: (job[1].stat().st_size, job[0]["file"]))
        self.telemetry.component("indexer", "running", self.run_id)
        self.progress()
        old_handler = None
        if multiprocessing.current_process().name == "MainProcess":
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
        try:
            jobs = self.analyse(root, jobs, pool)
            pool.shutdown(wait=True)
            pool = None
            self.parallel_index(jobs)
            if self.index is None:
                raise ValueError("No document could be indexed")
            self.extras(root)
            self.state = "completed" if not self.failed else "completed_with_errors"
            directory = self.publish(complete=not self.failed and self.extras_ready)
            self.progress()
            elapsed = self.previous_elapsed + time.monotonic() - self.started
            self.telemetry.event(
                "completion",
                "indexer",
                (
                    "Индексация завершена. База знаний полностью обновлена."
                    if not self.failed
                    else "Обработка корпуса завершена с ошибками; база неполная."
                )
                + f"\nДокументов обработано: {len(self.completed)}/{len(self.documents)}. "
                f"Ошибок: {len(self.failed)}.\nВремя: {duration(elapsed)}",
            )
            self.telemetry.component("indexer", self.state, self.run_id)
            return directory
        except BaseException:
            self.state = "interrupted"
            self.publish()
            self.progress(notify=True)
            self.telemetry.component("indexer", "interrupted", self.run_id)
            raise
        finally:
            if pool is not None:
                pool.shutdown(wait=not self.stop_requested, cancel_futures=True)
            if old_handler is not None:
                signal.signal(signal.SIGTERM, old_handler)
