import json
import logging
import re
import time

from openai import OpenAI

from app.config import Settings
from app.rag.prompts import FINAL_SYSTEM, RERANK_SYSTEM, REWRITE_SYSTEM
from app.rag.retrieval import Retriever, build_context, is_english

log = logging.getLogger(__name__)


def parse_json_response(text: str) -> dict:
    text = re.sub(r"^```(?:json)?", "", text.strip(), flags=re.I)
    text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise ValueError("Reranker returned invalid JSON") from None


class Pipeline:
    def __init__(self, settings: Settings, retriever: Retriever, client=None):
        self.settings = settings
        self.retriever = retriever
        self.client = client or OpenAI(
            api_key=settings.polza_api_key.get_secret_value(),
            base_url=settings.polza_base_url,
            timeout=90,
            max_retries=0,
        )

    def chat(self, messages: list[dict], max_tokens: int, deadline: float, temperature=0.0):
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError("Answer time budget exhausted")
        response = self.client.chat.completions.create(
            model=self.settings.generator_model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=min(90, remaining),
        )
        text = (response.choices[0].message.content or "").strip()
        tokens = getattr(response.usage, "total_tokens", 0) or 0
        return text, tokens

    def rewrite(self, question: str, history: list[str], deadline: float):
        if not history:
            return question, 0
        history_text = "\n".join(history[-10:])
        text, tokens = self.chat(
            [
                {"role": "system", "content": REWRITE_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"История диалога:\n{history_text}\n\nТекущий вопрос: {question}\n\nПолный вопрос:"
                    ),
                },
            ],
            max_tokens=150,
            deadline=deadline,
        )
        return text or question, tokens

    def rerank(self, question: str, candidates: list[dict], deadline: float):
        compact = [
            {
                "id": x["idx"],
                "title": x["title"],
                "category": x["category"],
                "section": x["section"],
                "text": x["text"][:1100],
            }
            for x in candidates[:28]
        ]
        text, tokens = self.chat(
            [
                {"role": "system", "content": RERANK_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Вопрос:\n{question}\n\nФрагменты:\n"
                        + json.dumps(compact, ensure_ascii=False)
                    ),
                },
            ],
            max_tokens=750,
            deadline=deadline,
        )
        candidate_map = {x["idx"]: x for x in candidates[:28]}
        ranked = []
        seen = set()
        try:
            data = parse_json_response(text)
            for row in data.get("ranked", []):
                idx = int(row["id"])
                score = float(row.get("score", 0))
                if idx in candidate_map and idx not in seen and 0 <= score <= 1:
                    ranked.append({**candidate_map[idx], "rerank_score": score})
                    seen.add(idx)
        except (ValueError, TypeError, KeyError, AttributeError):
            log.warning("Invalid reranker output; falling back to hybrid ranking")
            ranked, seen = [], set()
        ranked.extend({**x, "rerank_score": 0.0} for x in candidates if x["idx"] not in seen)
        return ranked[:18], tokens

    def answer(self, question: str, history: list[str]) -> dict:
        started = time.perf_counter()
        deadline = started + self.settings.request_timeout - 10
        prepared = self.retriever.prepared_answer(question)
        if prepared is not None:
            return {
                "answer": prepared,
                "sources": [],
                "route": "faq",
                "tokens": 0,
                "time_sec": round(time.perf_counter() - started, 3),
            }
        query, rewrite_tokens = self.rewrite(question, history, deadline)
        ranked, rerank_tokens = self.rerank(query, self.retriever.candidates(query), deadline)
        selected = self.retriever.select_context(ranked)
        links = self.retriever.best_links(query)
        links_text = "\n".join(f"- {x['description']}: {x['url']}" for x in links)
        history_text = "\n".join(history[-6:])
        prompt = (
            f"История текущего диалога:\n{history_text or 'нет'}\n\n"
            f"Материалы:\n{build_context(selected) or 'релевантные материалы не найдены'}\n\n"
            f"Релевантные ссылки:\n{links_text or 'релевантных ссылок нет'}\n\n"
            f"Вопрос пользователя:\n{question}\n\nОтвет:"
        )
        language = (
            "Отвечай полностью на английском языке."
            if is_english(question)
            else "Отвечай полностью на русском языке."
        )
        answer, generation_tokens = self.chat(
            [
                {"role": "system", "content": FINAL_SYSTEM + "\nОБЯЗАТЕЛЬНО: " + language},
                {"role": "user", "content": prompt},
            ],
            max_tokens=1100,
            temperature=0.3,
            deadline=deadline,
        )
        if not answer:
            raise ValueError("Generator returned an empty answer")
        return {
            "answer": answer,
            "sources": list(dict.fromkeys(x["title"] for x in selected)),
            "route": "rag",
            "tokens": rewrite_tokens + rerank_tokens + generation_tokens,
            "time_sec": round(time.perf_counter() - started, 3),
        }
