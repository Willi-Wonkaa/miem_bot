# MIEM Bot

Telegram-ассистент НИУ ВШЭ: готовые ответы из Excel и RAG по базе документов.
Python 3.12, Docker Compose, GitHub Actions. GPU не требуется.

На сервере:

```bash
cp .env.example .env
# Заполнить TELEGRAM_BOT_TOKEN, POLZA_API_KEY, RAG_API_TOKEN.
# Положить архив базы и Excel в data/raw/.
docker compose build bot rag
docker compose --profile index run --rm indexer
docker compose up -d --wait --wait-timeout 300
```

Проверки в CI: `uv sync --extra bot`, `uv run --extra bot pytest -q`, `uv run --extra bot ruff check .`.

- [Структура и перенос из Colab](documentation/project.md)
- [Запуск и обслуживание](documentation/operations.md)
- [CI/CD и секреты Actions](documentation/ci-cd.md)
