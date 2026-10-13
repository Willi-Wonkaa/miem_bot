import asyncio
import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, StringConstraints, field_validator
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.logging_setup import setup_logging
from app.rag.pipeline import Pipeline
from app.rag.retrieval import Retriever
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


def create_app(settings: Settings | None = None, pipeline=None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging("rag", settings)
        settings.require("polza_api_key", "rag_api_token")
        app.state.pipeline = pipeline or Pipeline(settings, Retriever(settings))
        app.state.slots = asyncio.Semaphore(settings.rag_concurrency)
        app.state.telemetry = Telemetry(settings.monitor_dir)
        run_id = uuid.uuid4().hex
        app.state.telemetry.component("rag", "ready", run_id)
        log.info("rag_api_started")
        yield
        client = getattr(app.state.pipeline, "client", None)
        if client is not None:
            client.close()
        app.state.telemetry.component("rag", "stopped", run_id)
        log.info("rag_api_stopped")

    application = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @application.get("/health")
    async def health():
        return {"status": "ok"}

    @application.get("/index")
    async def index_status(authorization: str = Header(default="")):
        expected = "Bearer " + settings.rag_api_token.get_secret_value()
        if not secrets.compare_digest(authorization.encode(), expected.encode()):
            raise HTTPException(status_code=401, detail="Unauthorized")
        retriever = getattr(application.state.pipeline, "retriever", None)
        return {
            "coverage": getattr(retriever, "metadata", {}) if pipeline is None else {},
            "progress": application.state.telemetry.get("index_progress", {}),
        }

    @application.post("/answer", response_model=AnswerResponse)
    async def answer(request: AnswerRequest, authorization: str = Header(default="")):
        expected = "Bearer " + settings.rag_api_token.get_secret_value()
        if not secrets.compare_digest(authorization.encode(), expected.encode()):
            raise HTTPException(status_code=401, detail="Unauthorized")
        try:
            await asyncio.wait_for(application.state.slots.acquire(), timeout=5)
        except TimeoutError:
            raise HTTPException(status_code=429, detail="Server busy") from None
        try:
            started = time.perf_counter()
            result = await run_in_threadpool(
                application.state.pipeline.answer, request.question, request.history
            )
            log.info(
                "answer_completed route=%s seconds=%.3f tokens=%d",
                result["route"],
                time.perf_counter() - started,
                result["tokens"],
            )
            return result
        except Exception:
            # Questions, contexts, upstream credentials and response bodies stay out of logs.
            log.exception("answer_generation_failed")
            application.state.telemetry.event(
                "errors", "rag", "Ошибка генерации ответа. См. rag.txt."
            )
            raise HTTPException(status_code=502, detail="Answer generation failed") from None
        finally:
            application.state.slots.release()

    return application


app = create_app()
