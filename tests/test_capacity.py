import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from fastapi.testclient import TestClient

from app.rag.api import create_app
from app.config import Settings


def test_ten_parallel_answers_and_overflow_waits(tmp_path):
    settings = Settings(
        _env_file=None, monitor_dir=tmp_path / "monitor", log_dir=tmp_path / "logs",
        polza_api_key="test", rag_api_token="test", rag_concurrency=10,
    )
    lock = threading.Lock()
    active = 0
    maximum = 0

    def answer(question, history):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.2)
        with lock:
            active -= 1
        return {"answer": "ok", "sources": [], "route": "rag", "tokens": 0, "time_sec": 0.2}

    pipeline = Mock()
    pipeline.answer.side_effect = answer
    with TestClient(create_app(settings, pipeline)) as client:
        with ThreadPoolExecutor(max_workers=15) as workers:
            responses = list(workers.map(
                lambda number: client.post("/answer", json={"question": f"question {number}"},
                                           headers={"Authorization": "Bearer test"}),
                range(15),
            ))
    assert all(response.status_code == 200 for response in responses)
    assert maximum == 10
    assert pipeline.answer.call_count == 15
