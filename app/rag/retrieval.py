import json
import logging
import re
from collections import defaultdict

import faiss
import numpy as np
from nltk.stem.snowball import SnowballStemmer
from rank_bm25 import BM25Okapi

from app.config import Settings
from app.rag.indexing import (
    FORMAT_VERSION,
    current_generation,
    make_embedder,
    normalize_faq,
    retrieval_text,
)

stemmer = SnowballStemmer("russian")


def tokenize(text: str) -> list[str]:
    return [
        stemmer.stem(word)
        for word in re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", text.lower())
        if len(word) > 2
    ]


def is_english(question: str) -> bool:
    return len(re.findall(r"[A-Za-z]", question)) > len(re.findall(r"[А-Яа-яЁё]", question))


class Retriever:
    def __init__(self, settings: Settings, embedder=None):
        self.settings = settings
        directory = current_generation(settings.index_dir)
        metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        self.metadata = metadata
        if metadata["format_version"] != FORMAT_VERSION:
            raise ValueError("Unsupported index format; run indexer")
        if metadata["embed_model"] != settings.embed_model:
            raise ValueError("Embedding model changed; run indexer")
        if metadata.get("quantized", False) != (
            settings.embed_quantize and settings.embed_device == "cpu"
        ):
            raise ValueError("Embedding precision changed; run indexer")
        self.chunks = json.loads((directory / "chunks.json").read_text(encoding="utf-8"))
        self.links = json.loads((directory / "links.json").read_text(encoding="utf-8"))
        self.faq = json.loads((directory / "faq.json").read_text(encoding="utf-8"))
        self.index = faiss.read_index(str(directory / "documents.faiss"))
        if not self.chunks or self.index.ntotal != len(self.chunks):
            raise ValueError("Invalid document index; run indexer")
        with np.load(directory / "extras.npz", allow_pickle=False) as extras:
            self.link_vectors = extras["links"]
            self.faq_vectors = extras["faq"]
        if self.link_vectors.shape != (len(self.links), self.index.d):
            raise ValueError("Invalid links index; run indexer")
        if self.faq_vectors.shape != (len(self.faq), self.index.d):
            raise ValueError("Invalid FAQ index; run indexer")
        self.embedder = embedder or make_embedder(settings)
        self.document_bm25 = BM25Okapi([tokenize(retrieval_text(x)) for x in self.chunks])
        self.faq_bm25 = BM25Okapi([x["question"].split() for x in self.faq]) if self.faq else None
        self.positions = {
            (chunk["document_id"], int(chunk["chunk_id"])): idx
            for idx, chunk in enumerate(self.chunks)
        }
        logging.getLogger(__name__).info(
            "retriever_ready chunks=%d faq=%d links=%d",
            len(self.chunks),
            len(self.faq),
            len(self.links),
        )

    def encode_query(self, question: str) -> np.ndarray:
        return self.embedder.encode(
            [f"query: {question}"], normalize_embeddings=True, convert_to_numpy=True
        ).astype("float32")

    def candidates(self, question: str, top_k: int = 40) -> list[dict]:
        _, dense_ids = self.index.search(self.encode_query(question), min(60, len(self.chunks)))
        lexical = self.document_bm25.get_scores(tokenize(question))
        lexical_ids = np.argsort(lexical)[::-1][:60]
        scores = defaultdict(float)
        for rank, idx in enumerate(dense_ids[0], start=1):
            if idx >= 0:
                scores[int(idx)] += 1 / (60 + rank)
        for rank, idx in enumerate(lexical_ids, start=1):
            scores[int(idx)] += 0.9 / (60 + rank)
        return [
            {"idx": idx, "rrf_score": score, **self.chunks[idx]}
            for idx, score in sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
        ]

    def best_links(self, question: str, top_k: int = 2) -> list[dict]:
        if not self.links:
            return []
        scores = self.link_vectors @ self.encode_query(question)[0]
        return [
            self.links[int(idx)]
            for idx in np.argsort(scores)[::-1][:top_k]
            if scores[idx] > self.settings.link_threshold
        ]

    def prepared_answer(self, question: str) -> str | None:
        # The supplied spreadsheet contains Russian answers only.
        if not self.faq or is_english(question):
            return None
        cleaned = normalize_faq(question)
        if not cleaned:
            return None
        dense = self.faq_vectors @ self.encode_query(cleaned)[0]
        lexical = self.faq_bm25.get_scores(cleaned.split())
        scores = np.zeros(len(self.faq))
        for rank, idx in enumerate(np.argsort(dense)[::-1], start=1):
            scores[idx] += 1 / (60 + rank)
        for rank, idx in enumerate(np.argsort(lexical)[::-1], start=1):
            if lexical[idx] > 0:
                scores[idx] += 1 / (60 + rank)
        best = int(np.argmax(scores))
        # Both thresholds must apply to the same winning candidate.
        if (
            scores[best] >= self.settings.faq_rrf_threshold
            and dense[best] >= self.settings.faq_cosine_threshold
        ):
            return self.faq[best]["answer"]
        return None

    def select_context(self, ranked: list[dict], max_chunks: int = 9) -> list[dict]:
        grouped = defaultdict(list)
        for item in ranked:
            grouped[item["document_id"]].append(item)
        documents = []
        for hits in grouped.values():
            hits.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
            score = sum(
                hit.get("rerank_score", 0) * weight
                for hit, weight in zip(hits, [1.0, 0.55, 0.30, 0.15])
            )
            documents.append((score, hits))
        documents.sort(key=lambda x: x[0], reverse=True)
        if not documents:
            return []
        chosen = documents[:1]
        if len(documents) > 1:
            first, second = documents[0][0], documents[1][0]
            if second >= 0.60 * first and second >= 0.35:
                chosen.append(documents[1])
        indices = []
        seen = set()
        for position, (_, hits) in enumerate(chosen):
            for hit in hits[: 5 if position == 0 else 2]:
                cid = int(hit["chunk_id"])
                neighbours = [hit["idx"]] + [
                    self.positions.get((hit["document_id"], neighbour))
                    for neighbour in [cid - 1, cid + 1]
                ]
                for idx in neighbours:
                    if idx is not None and idx not in seen:
                        indices.append(idx)
                        seen.add(idx)
        return [{"idx": idx, **self.chunks[idx]} for idx in indices[:max_chunks]]


def build_context(selected: list[dict], max_chars: int = 18000) -> str:
    separator = "\n\n============================\n\n"
    blocks = []
    used = 0
    for item in selected:
        block = (
            f"Источник: {item['title']}\nКатегория: {item['category']}\n"
            f"Раздел: {item['section']}\n\n{item['text']}"
        )
        size = len(block) + (len(separator) if blocks else 0)
        if used + size <= max_chars:
            blocks.append(block)
            used += size
    return separator.join(blocks)
