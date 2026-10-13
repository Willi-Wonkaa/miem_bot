import json
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.rag.api import create_app
from app.rag.incremental import IncrementalIndexer
from app.rag.indexing import build_index, current_generation
from app.rag.live_index import LiveIndex
from app.rag.retrieval import Retriever
from app.rag.storage import writer_lock


def add_document(settings):
    with zipfile.ZipFile(settings.kb_zip) as original:
        contents = {name: original.read(name) for name in original.namelist()}
    manifest = json.loads(contents["kb/manifest.json"])
    manifest["documents"].append({"document_id": "new", "file": "new.txt", "title": "Стипендия"})
    contents["kb/manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode()
    contents["kb/new.txt"] = ("Стипендию назначают по итогам аттестации. " * 10).encode()
    with zipfile.ZipFile(settings.kb_zip, "w") as archive:
        for name, value in contents.items():
            archive.writestr(name, value)


def test_first_document_is_searchable_before_completion(corpus, tmp_path, monkeypatch):
    settings, embedder = corpus
    settings.index_dir = tmp_path / "fresh"
    publications = []
    original = IncrementalIndexer.publish

    def observe(self, complete=False):
        path = original(self, complete)
        if path:
            retriever = Retriever(settings, embedder, directory=path)
            publications.append(
                (retriever.metadata["indexed_files"], retriever.metadata["complete"])
            )
            assert retriever.candidates("Университет")
        return path

    monkeypatch.setattr(IncrementalIndexer, "publish", observe)
    final = build_index(settings, embedder)
    assert publications[0] == (1, False)
    assert publications[-1] == (3, True)
    assert current_generation(settings.index_dir) == final


def test_batch_checkpoint_resumes_without_recomputing(tmp_path, corpus):
    settings, base = corpus
    settings.embed_batch_size = 1
    texts = ["Первый вопрос", "Второй вопрос", "Третий вопрос"]

    class Failing:
        calls = 0

        def encode(self, values, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("simulated interruption")
            return base.encode(values, **kwargs)

    checkpoint = tmp_path / "checkpoint"
    with pytest.raises(RuntimeError):
        IncrementalIndexer(settings, Failing()).encode_checkpoint(texts, checkpoint)
    assert (checkpoint / "batch-00000000.npy").is_file()

    class Counting:
        calls = 0

        def encode(self, values, **kwargs):
            self.calls += 1
            return base.encode(values, **kwargs)

    resumed = Counting()
    result = IncrementalIndexer(settings, resumed).encode_checkpoint(texts, checkpoint)
    assert resumed.calls == 2 and result.shape == (3, 128)
    np.testing.assert_allclose(result, base.encode(["passage: " + text for text in texts]))


def test_finished_documents_reused_when_archive_changes(corpus):
    settings, base = corpus
    with zipfile.ZipFile(settings.kb_zip, "a") as archive:
        archive.writestr("note.txt", "changed archive")

    class NoRecompute:
        def encode(self, *args, **kwargs):
            raise AssertionError("Cached vectors must be reused")

    final = build_index(settings, NoRecompute())
    assert json.loads((final / "metadata.json").read_text())["complete"]
    assert Retriever(settings, base).index.ntotal == 3


def test_reload_does_not_block_readers_or_change_inflight_snapshot(corpus, monkeypatch):
    settings, embedder = corpus
    live = LiveIndex(settings, embedder)
    assert live.refresh()
    before = live.snapshot()
    add_document(settings)
    build_index(settings, embedder)
    entered, release = threading.Event(), threading.Event()
    original = Retriever

    def slow_loader(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr("app.rag.live_index.Retriever", slow_loader)
    with ThreadPoolExecutor() as executor:
        future = executor.submit(live.refresh)
        assert entered.wait(5)
        try:
            assert live.snapshot() is before
            assert len(live.snapshot().chunks) == 3
            assert live.snapshot().candidates("Экзамены")
        finally:
            release.set()
        assert future.result(timeout=10)
    assert len(live.snapshot().chunks) == 4
    assert len(before.chunks) == 3


def test_invalid_publication_keeps_last_consistent_copy(corpus):
    settings, embedder = corpus
    live = LiveIndex(settings, embedder)
    assert live.refresh()
    before = live.snapshot()
    bad = settings.index_dir / ("a" * 32)
    bad.mkdir()
    (settings.index_dir / "CURRENT").write_text(bad.name)
    assert live.refresh() is False
    assert live.snapshot() is before


def test_api_works_while_first_index_is_not_yet_published(tmp_path):
    settings = Settings(
        _env_file=None,
        index_dir=tmp_path / "index",
        monitor_dir=tmp_path / "monitor",
        log_dir=tmp_path / "logs",
        polza_api_key="test-key",
        rag_api_token="test-internal",
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json() == {"status": "ok", "index_ready": False}
        response = client.post(
            "/answer",
            headers={"Authorization": "Bearer test-internal"},
            json={"question": "Как получить книгу?"},
        )
        assert response.status_code == 200 and response.json()["route"] == "warming"
        assert (
            client.get("/index", headers={"Authorization": "Bearer test-internal"}).status_code
            == 200
        )


def test_only_one_writer_can_publish(tmp_path):
    with writer_lock(tmp_path):
        with pytest.raises((RuntimeError, OSError)):
            with writer_lock(tmp_path):
                pytest.fail("Second writer must be rejected")
