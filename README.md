# MIEM Bot

Telegram-бот для вопросов о МИЭМ НИУ ВШЭ. Сначала ищет готовый ответ в Excel, затем — подходящие фрагменты документов. Если база ещё не готова, отвечает через LLM без ссылок на неё. Для поиска используются E5, FAISS и BM25; генерация идёт через Polza API.

Стек: Python 3.12, aiogram, FastAPI, Redis, SQLite, Docker Compose. Боты работают через Telegram long polling; внешние HTTP-порты не открываются.

## С чего начать

1. [Архитектура](documentation/architecture.md) — путь вопроса и связи процессов.
2. [Карта проекта](documentation/structure.md) — где лежат код, данные и скрипты.
3. Компоненты: [основной бот](documentation/telegram.md), [RAG API](documentation/rag.md), [индексатор](documentation/indexing.md), [служебный бот](documentation/service.md), [Redis](documentation/redis.md), [монитор VPS](documentation/host-monitor.md).
4. [Деплой](documentation/deployment.md) — сервер, секреты, Actions и режимы запуска.
5. [Эксплуатация](documentation/operations.md) — проверка состояния, логи, восстановление.

## Запуск опубликованной версии

После первоначальной настройки VPS и секретов из [инструкции по деплою](documentation/deployment.md):

```bash
python scripts/start.py
```

Скрипт запускает GitHub Actions для **ветки `main`**, показывает ссылку на запуск и ждёт результат. Нужны Python 3.12, доступ к репозиторию и авторизация GitHub через Git Credential Manager либо `GH_TOKEN`/`GITHUB_TOKEN`. Локальные правки в запуск не попадают: их нужно закоммитить и отправить в `main`.

Другие режимы: `python scripts/start.py --mode prepare` только доставляет и собирает версию; `--mode deploy` запускает сервисы без индексации. Тот же workflow можно запустить из GitHub Actions: **CI and deploy → Run workflow**.

## Локальная разработка

Из каталога `miem_bot/`:

```bash
uv sync --frozen --extra bot --extra rag --python 3.12
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

Для ручного запуска скопируйте `.env.example` в `.env`, заполните токены и запустите Redis. Локальные пути для данных и адреса сервисов приведены в `.env.example`. Точки входа: `uvicorn app.rag.api:app --host 127.0.0.1 --port 8000`, `python -m app.telegram.bot`, `python -m app.service.bot` и `python -m app.rag.indexing`. В production команды задают `Dockerfile` и `compose.yaml`.

Исходники базы лежат в `data/raw/`. Для загрузки пары файлов из родительского каталога есть `python scripts/import_data.py ..`. Формат данных описан в [индексации](documentation/indexing.md).

Переписки и оценки хранятся постоянно в общем каталоге `/opt/miem-bot/shared/history`. Кнопки оценки исчезают после нажатия или через 10 минут. Просмотр и выгрузка доступны администраторам сервисного бота из `SERVICE_ADMIN_IDS`; см. [хранение переписок](documentation/history.md).
