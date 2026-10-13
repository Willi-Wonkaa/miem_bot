# Карта проекта

Рабочее приложение находится в каталоге `miem_bot/`. В его родительском каталоге лежат исходный notebook, копии Excel/ZIP и `extract_notebook.py`; контейнеры их не используют. Текущие рабочие модули — в `app/`.

| Путь | Для чего нужен |
| --- | --- |
| `app/config.py` | Настройки из переменных окружения и `.env`; проверка токенов |
| `app/telegram/` | Основной Telegram-бот, история Redis, форматирование, архив и оценки |
| `app/rag/` | HTTP API, поиск, генерация ответа, парсинг и индексация |
| `app/service/bot.py`, `app/service/history.py` | Служебный Telegram-бот; просмотр архива по списку администраторов |
| `app/telemetry.py` | SQLite событий, счётчиков и подписок |
| `app/host_monitor.py` | Процесс на VPS: состояние Docker и лимиты индексатора |
| `app/transcripts.py` | SQLite-архив диалогов и оценок |
| `app/logging_setup.py` | Файловые логи с ротацией и маскированием секретов |
| `scripts/start.py` | Запуск GitHub Actions из локальной консоли |
| `scripts/remote_deploy.py`, `scripts/deploy.sh` | Доставка релиза по SSH и запуск на VPS |
| `scripts/bootstrap.sh` | Первичная подготовка Ubuntu-сервера |
| `scripts/status.sh`, `scripts/remote_status.py` | Диагностика VPS через отдельный workflow |
| `scripts/start_monitor.sh`, `scripts/install_monitor.sh` | Запуск монитора; второй скрипт устанавливает вариант через systemd |
| `scripts/import_data.py` | Копирование исходного ZIP/Excel в `data/raw/` |
| `tests/` | Автономные тесты API, индексации, ботов, мониторинга и деплоя |
| `.github/workflows/` | CI, деплой и отдельная проверка статуса |
| `compose.yaml`, `Dockerfile` | Сервисы, volumes, лимиты и два образа (`bot`, `rag`) |
| `.env.example`, `pyproject.toml`, `uv.lock` | Образец настроек и закреплённые зависимости |
| `data/raw/` | Исходные документы, включённые в текущий репозиторий |

## Ориентиры внутри `app/rag/`

| Файл | Роль |
| --- | --- |
| `api.py` | `/health`, `/index`, `/answer` и авторизация |
| `pipeline.py`, `prompts.py` | Маршруты ответа, вызовы Polza, промпты |
| `retrieval.py`, `live_index.py` | FAISS/BM25 и загрузка опубликованных поколений |
| `indexing.py`, `incremental.py` | Загрузка модели, проверка источников, индексатор и публикация |
| `parsing.py`, `parser_worker.py`, `chunking.py` | Разбор DOCX/TXT/PDF и фрагменты |
| `storage.py`, `timing.py` | Атомарные записи, блокировка и расчёт скорости |
| `smoke.py`, `check_index.py` | Проверка запущенного API и целостности индекса |
| `progress.py` | Старый observer; основной `IncrementalIndexer` его не вызывает |

В корне `miem_bot/` находятся Git-репозиторий и актуальная документация. Каталог `.ops/` в родительском каталоге содержит рабочие файлы для ручных операций; он не входит в приложение и релизный архив `git archive`.
