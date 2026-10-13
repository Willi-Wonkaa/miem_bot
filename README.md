# MIEM Bot

Telegram-ассистент НИУ ВШЭ: готовые ответы из Excel и RAG по базе документов.
Python 3.12, Docker Compose, GitHub Actions. GPU не требуется.

Код и проверки — через Git/GitHub Actions. Добавить токены в серверный `.env`,
затем Actions → CI and deploy → Run workflow: `prepare`, `deploy` или `index`.
`service` запускается первым; база становится доступна после первого проиндексированного файла.
Индексация запускается только при выборе `index`, обычный деплой её не возобновляет.

Проверки в CI: `uv sync --extra bot`, `uv run --extra bot pytest -q`, `uv run --extra bot ruff check .`.

- [Структура и перенос из Colab](documentation/project.md)
- [Запуск и обслуживание](documentation/operations.md)
- [CI/CD и секреты Actions](documentation/ci-cd.md)
- [Логи, служебный бот и статусы](documentation/monitoring.md)
