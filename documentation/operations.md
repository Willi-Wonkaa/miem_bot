# Запуск

1. Скопировать `.env.example` в `.env`; заполнить токены. Для внутреннего токена:
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. Добавить `MIEM_knowledge_base_unified.zip` и `HSE_Answers_RU.xlsx` в `data/raw/`.
   Если они рядом с исходным ноутбуком: `python scripts/import_data.py ..`.
   Исходное имя `MIEM_HSE_knowledge_base.zip` тоже поддерживается при импорте.
   Данные разрешено коммитить в Git; файлы от 100 MiB нужно хранить на сервере.
3. Выполнить команды из README. Сначала индексатор, затем сервисы.

Архив содержит один `manifest.json` с массивом `documents`.
Документ: `document_id`, `file` (относительно manifest), `title`, необязательная `category`.
Форматы: DOCX/TXT/PDF. `all_links.txt` внутри архива необязателен: `описание https://адрес`.
Excel необязателен; первая вкладка, колонки: `Основной вопрос`,
`Варианты вопроса на русском` (по одному на строку), `Ответ на русском`.

На VPS используется `/opt/miem-bot`:

- `shared/.env` — токены, права `600`;
- `shared/raw/` — исходная база и Excel;
- `releases/<commit>` — версии проекта;
- `current` — ссылка на успешно запущенную версию.

До первого запуска подготовленная версия доступна через `pending`.
После заполнения `shared/.env` подготовка и первый запуск (индексация выполняется явно):

```bash
release=$(basename "$(readlink -f /opt/miem-bot/pending)")
bash "/opt/miem-bot/incoming/$release.sh" "$release" prepare
cd /opt/miem-bot/pending
export RELEASE_TAG="$release"
docker compose -f compose.yaml -f compose.server.yaml --profile index run --rm indexer
bash "/opt/miem-bot/incoming/$release.sh" "$release"
```

Подготовка нового Ubuntu 24.04: `sudo bash scripts/bootstrap.sh deploy-key.pub`.
Требуются SSH-ключ и пользователь `miem-deploy`; скрипт ставит Docker + Compose.
Учётная запись входит в группу Docker и может управлять контейнерами.

Обслуживание на VPS:

```bash
cd /opt/miem-bot/current
export RELEASE_TAG=$(basename "$(pwd -P)")
docker compose -f compose.yaml -f compose.server.yaml ps
docker compose -f compose.yaml -f compose.server.yaml logs --tail=100 bot rag
docker compose -f compose.yaml -f compose.server.yaml --profile index run --rm indexer
docker compose -f compose.yaml -f compose.server.yaml up -d --force-recreate rag bot
```

После обновления базы нужен индексатор и перезапуск RAG. Неизменённая база использует старый индекс.
История: 5 пар сообщений, TTL 24 часа; `/reset` очищает диалог.
Для бэкапа сохранить `shared/` и Docker volumes. `docker compose down -v` удаляет volumes.
Реальные ответы проверяются после добавления базы и токенов; offline-тесты используют тестовый embedder.
