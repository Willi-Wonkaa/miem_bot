import asyncio
import contextlib
import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
from openai import OpenAI
from pydantic import BaseModel, Field, StringConstraints, field_validator
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.logging_setup import setup_logging
from app.rag.live_index import LiveIndex
from app.rag.pipeline import Pipeline
from app.telemetry import Telemetry

log = logging.getLogger(__name__)
Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
HistoryEntry = Annotated[str, StringConstraints(max_length=4000)]


class AnswerRequest(BaseModel):
    question: Question
    history: list[HistoryEntry] = Field(default_factory=list, max_length=10)

    @field_validator("question")
    @classmethod
    def require_words(cls, value: str):
        if not any(c.isalnum() for c in value):
            raise ValueError("Question must contain text")
        return value


class AnswerResponse(BaseModel):
    answer: str
    sources: list[str]
    route: str
    tokens: int
    time_sec: float
    coverage: dict = Field(default_factory=dict)


def create_app(settings: Settings | None = None, pipeline=None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging("rag", settings)
        settings.require("polza_api_key", "rag_api_token")
        state = Telemetry(settings.monitor_dir)
        run_id = uuid.uuid4().hex
        state.component("rag", "ready", run_id, "HTTP API доступен")
        app.state.telemetry = state
        app.state.live_index = LiveIndex(settings) if pipeline is None else None
        app.state.pipeline = pipeline
        app.state.llm_client = (
            OpenAI(
                api_key=settings.polza_api_key.get_secret_value(),
                base_url=settings.polza_base_url,
                timeout=90,
                max_retries=0,
            )
            if pipeline is None
            else None
        )
        app.state.slots = asyncio.Semaphore(settings.rag_concurrency)

        async def reload_loop():
            live = app.state.live_index
            while True:
                if live is not None:
                    changed = await asyncio.to_thread(live.refresh)
                    if changed:
                        state.component("rag-model", "ready", run_id, settings.embed_model)
                        state.event(
                            "startup",
                            "rag-index",
                            "Рабочая версия индекса загружена: "
                            f"{live.current.metadata.get('indexed_files', 0)} файлов.",
                        )
                state.component("rag", "ready", run_id)
                await asyncio.sleep(settings.index_reload_seconds)

        task = asyncio.create_task(reload_loop())
        log.info("rag_api_started")
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        client = getattr(app.state.pipeline, "client", None)
        if client is not None:
            client.close()
        if app.state.llm_client is not None:
            app.state.llm_client.close()
        state.component("rag", "stopped", run_id)
        log.info("rag_api_stopped")

    application = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @application.get("/health")
    async def health():
        live = application.state.live_index
        return {"status": "ok", "index_ready": bool(live and live.snapshot()) if live else True}

    def authenticate(authorization):
        expected = "Bearer " + settings.rag_api_token.get_secret_value()
        if not secrets.compare_digest(authorization.encode(), expected.encode()):
            raise HTTPException(status_code=401, detail="Unauthorized")

    @application.get("/index")
    async def index_status(authorization: str = Header(default="")):
        authenticate(authorization)
        live = application.state.live_index
        return {
            "coverage": live.coverage() if live else {},
            "progress": application.state.telemetry.get("index_progress", {}),
        }

    @application.post("/answer", response_model=AnswerResponse)
    async def answer(request: AnswerRequest, authorization: str = Header(default="")):
        authenticate(authorization)
        request_id = application.state.telemetry.request_received()
        live = application.state.live_index
        snapshot = live.snapshot() if live else None
        try:
            await asyncio.wait_for(application.state.slots.acquire(), timeout=5)
        except TimeoutError:
            application.state.telemetry.request_processed(request_id, success=False)
            raise HTTPException(status_code=429, detail="Server busy") from None
        try:
            active = application.state.pipeline or Pipeline(
                settings, snapshot, application.state.llm_client
            )
            started = time.perf_counter()
            result = await run_in_threadpool(active.answer, request.question, request.history)
            result["coverage"] = live.coverage(snapshot) if live else {}
            application.state.telemetry.request_processed(request_id)
            log.info(
                "answer_completed route=%s seconds=%.3f tokens=%d",
                result["route"],
                time.perf_counter() - started,
                result["tokens"],
            )
            return result
        except Exception:
            application.state.telemetry.request_processed(request_id, success=False)
            log.exception("answer_generation_failed")
            application.state.telemetry.event(
                "errors", "rag", "Ошибка генерации ответа. Подробности в rag.txt."
            )
            raise HTTPException(status_code=502, detail="Answer generation failed") from None
        finally:
            application.state.slots.release()

    return application


app = create_app()
