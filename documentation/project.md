# Проект

Путь запроса: Telegram → bot → RAG API → готовый ответ или hybrid retrieval → Polza → Telegram.

| Файл / каталог | Ответственность | Блоки Colab |
| --- | --- | --- |
| `app/config.py` | Настройки из `.env`, проверка обязательных токенов | API и Telegram credentials |
| `app/rag/parsing.py` | DOCX, таблицы, TXT, PDF; очистка текста | 4 |
| `app/rag/chunking.py` | Структурные фрагменты с перекрытием | 5 |
| `app/rag/indexing.py` | Распаковка базы, E5, сохранение FAISS, ссылок и FAQ | 3, 6, ссылки, Excel |
| `app/rag/retrieval.py` | Отдельные BM25 для документов и FAQ; RRF, ссылки, соседние чанки | 9, 11, готовые ответы |
| `app/rag/prompts.py` | Исходные системные промпты | Rerank, answer, rewrite |
| `app/rag/pipeline.py` | FAQ → rewrite → retrieval → Qwen rerank → Qwen answer | 10, 12, история |
| `app/rag/api.py` | Внутренний HTTP API, авторизация, ограничение параллелизма | Новый серверный слой |
| `app/telegram/` | Polling, форматирование, Redis-история, блокировка повторных запросов | Бот |
| `app/service/`, `app/telemetry.py` | Служебный Telegram-бот, уведомления, SQLite-счётчики и кнопки | Новый мониторинг |
| `app/logging_setup.py`, `app/host_monitor.py` | TXT-логи, Docker-статусы, лимиты ресурсов индексатора | Новый мониторинг |
| `data/raw/` | Архив базы: 205 документов; Excel с готовыми ответами | Загрузки Colab |
| `scripts/` | Импорт данных, подготовка VPS, деплой | Замена ручного запуска |
| `tests/` | Индексация, retrieval, API, Telegram; без внешних токенов | Замена sanity-прогонов |

Контейнеры: `service`, `bot`, `rag`, `redis`. `indexer` — одноразовая задача через профиль `index`.
Volumes: `index-data` — версии индексов, `model-cache` — E5, `redis-data` — история.
Снаружи порты не публикуются. Бот использует long polling.

Исправлено: FAQ больше не перезаписывает BM25 основной базы; проверка cosine относится
к выбранному FAQ-ответу; пустые/нетекстовые сообщения обработаны; история учитывает чат и пользователя.
Сохранены пороги FAQ и ссылок, агрегация документов, соседние чанки и язык текущего вопроса.
Английские вопросы проходят через RAG, поскольку готовые ответы в Excel только на русском.
Colab upload, `nest_asyncio`, отладочные распечатки, benchmark и judge в production не включены.
