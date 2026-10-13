import pytest

from app.config import Settings
from app.rag.incremental import IncrementalIndexer
from app.rag.timing import IndexTiming


def test_three_concurrent_workers_use_one_wall_clock_interval():
    timer = IndexTiming(started=0)
    timer.begin_encoding(now=0)
    for _ in range(3):
        timer.record(5, now=10)
    assert timer.rate(fallback=0.5, now=10) == pytest.approx(1.5)
    assert timer.elapsed(now=10) == 10


def test_rate_tracks_recent_work_and_detects_a_stall():
    timer = IndexTiming(started=0)
    timer.begin_encoding(now=0)
    timer.record(100, now=10)
    timer.record(6, now=70)
    assert timer.rate(fallback=0.5, now=70) == pytest.approx(0.1)
    assert timer.rate(fallback=0.5, now=135) == 0


def test_progress_shows_current_run_elapsed_without_previous_runs(tmp_path):
    settings = Settings(_env_file=None, monitor_dir=tmp_path / "monitor")
    indexer = IncrementalIndexer(settings)
    indexer.previous_elapsed = 1800
    indexer.timing.elapsed = lambda: 600
    indexer.encoded(30)
    progress = indexer.progress()
    assert progress["elapsed"] == 600
    assert progress["elapsed_total"] == 2400
    assert indexer.timing.count == 0


def test_stop_signal_reports_the_matching_controller_reason(tmp_path, monkeypatch):
    from app.rag.storage import atomic_json

    settings = Settings(
        _env_file=None, monitor_dir=tmp_path / "monitor", index_control_dir=tmp_path / "host"
    )
    indexer = IncrementalIndexer(settings)
    monkeypatch.setattr("app.rag.incremental.socket.gethostname", lambda: "container-1")
    atomic_json(settings.index_control_dir / "indexer-stop.json", {
        "container_id": "container-1", "updated": indexer.started_at + 1,
        "kind": "resource_pressure", "reason": "Недостаточно памяти",
    })
    indexer.request_stop()
    assert indexer.stop_requested and indexer.stop_reason == "Недостаточно памяти"
    assert indexer.stop_kind == "resource_pressure"
