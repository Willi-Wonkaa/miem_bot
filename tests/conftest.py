import hashlib
import json
import re
import zipfile

import numpy as np
import pytest
from openpyxl import Workbook

from app.config import Settings
from app.rag.indexing import build_index


class FakeEmbedder:
    """Deterministic token vectors for testing retrieval without downloading a model."""

    def encode(self, texts, **kwargs):
        vectors = []
        for text in texts:
            vector = np.zeros(128, dtype="float32")
            for word in re.findall(r"\w+", text.split(": ", 1)[-1].lower()):
                slot = int.from_bytes(hashlib.sha256(word.encode()).digest()[:2], "big") % 128
                vector[slot] += 1
            norm = np.linalg.norm(vector)
            vectors.append(vector / norm if norm else vector)
        return np.stack(vectors)


@pytest.fixture
def corpus(tmp_path):
    docs = [
        ("exam", "Экзамены", "Экзамены проходят по расписанию. " * 10),
        ("library", "Библиотека", "Библиотека выдаёт учебники студентам. " * 10),
        ("dorm", "Общежитие", "Общежитие предоставляет комнаты иногородним студентам. " * 10),
    ]
    archive = tmp_path / "kb.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        manifest = {"documents": []}
        for doc_id, title, text in docs:
            name = f"{doc_id}.txt"
            manifest["documents"].append({"document_id": doc_id, "title": title, "file": name})
            zipped.writestr(f"kb/{name}", text)
        zipped.writestr("kb/manifest.json", json.dumps(manifest, ensure_ascii=False))
        zipped.writestr("kb/all_links.txt", "Библиотека https://library.hse.ru/\n")
    faq = tmp_path / "faq.xlsx"
    workbook = Workbook()
    workbook.active.append(["Основной вопрос", "Варианты вопроса на русском", "Ответ на русском"])
    for row in [
        ("Что такое СОП", "Как расшифровать СОП", "Студенческая оценка преподавания"),
        ("Расписание экзаменов", "Когда экзамен", "Посмотри расписание"),
        ("Военная кафедра", "Отсрочка армии", "Обратись в военный учебный центр"),
        ("Библиотека учебники", "Потерял книгу", "Свяжись с библиотекой"),
    ]:
        workbook.active.append(row)
    workbook.save(faq)
    settings = Settings(
        _env_file=None,
        kb_zip=archive,
        faq_path=faq,
        index_dir=tmp_path / "index",
        rag_api_token="test-internal-key",
        polza_api_key="test-polza-key",
    )
    embedder = FakeEmbedder()
    build_index(settings, embedder)
    return settings, embedder
