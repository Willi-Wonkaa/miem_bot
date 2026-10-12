FROM ghcr.io/astral-sh/uv:0.12.23 AS uv
FROM python:3.12-slim-bookworm AS base
COPY --from=uv /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_CACHE=1 \
    HF_HOME=/data/models TOKENIZERS_PARALLELISM=false \
    OMP_NUM_THREADS=2 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app \
    && mkdir -p /data/index /data/models && chown -R app:app /data
COPY pyproject.toml uv.lock ./

FROM base AS bot
RUN uv sync --frozen --no-dev --extra bot
COPY app ./app
USER app
CMD ["python", "-m", "app.telegram.bot"]

FROM base AS test
RUN uv sync --frozen --extra bot
COPY app ./app
COPY tests ./tests
CMD ["pytest", "-q"]

FROM base AS rag
RUN uv sync --frozen --no-dev --extra rag
COPY app ./app
USER app
CMD ["uvicorn", "app.rag.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
