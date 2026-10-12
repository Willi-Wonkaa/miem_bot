import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, StringConstraints, field_validator
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.rag.pipeline import Pipeline
from app.rag.retrieval import Retriever

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
        settings.require("polza_api_key", "rag_api_token")
        app.state.pipeline = pipeline or Pipeline(settings, Retriever(settings))
        app.state.slots = asyncio.Semaphore(settings.rag_concurrency)
        yield
        client = getattr(app.state.pipeline, "client", None)
        if client is not None:
            client.close()

    application = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @application.get("/health")
    async def health():
        return {"status": "ok"}

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
            return await run_in_threadpool(
                application.state.pipeline.answer, request.question, request.history
            )
        except Exception:
            # Questions, contexts, upstream credentials and response bodies stay out of logs.
            log.error("Answer generation failed")
            raise HTTPException(status_code=502, detail="Answer generation failed") from None
        finally:
            application.state.slots.release()

    return application


app = create_app()
