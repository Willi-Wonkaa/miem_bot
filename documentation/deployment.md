# Деплой на VPS

Штатный путь: GitHub Actions собирает и проверяет commit, затем доставляет его по SSH на Ubuntu VPS. Сервер собирает Docker-образы локально. Рабочая копия на сервере — `/opt/miem-bot/current`, ссылка на каталог конкретного commit в `releases/`.

## Первичная настройка

1. На сервере под root запустите `bash scripts/bootstrap.sh /path/to/deploy-key.pub` из скопированного проекта. Скрипт ставит Docker и Compose, создаёт пользователя `miem-deploy`, добавляет ключ в `authorized_keys` и создаёт `/opt/miem-bot`. Пользователь входит в группу `docker`.
2. Создайте `/opt/miem-bot/shared/.env` по [`.env.example`](../.env.example), выставьте права `600` и заполните как минимум `TELEGRAM_BOT_TOKEN`, `SERVICE_BOT_TOKEN`, `POLZA_API_KEY` и `RAG_API_TOKEN`. Значение `RAG_API_TOKEN` — случайный внутренний секрет, общий для `bot` и `rag`.
3. Проверьте `/opt/miem-bot/shared/raw/MIEM_knowledge_base_unified.zip`. Если файлы есть в репозитории под `data/raw/`, `deploy.sh` скопирует их туда сам. Excel необязателен. Каталог `shared/raw` должен быть доступен UID 10001 контейнера.
4. В GitHub создайте environment `production` и добавьте Actions secrets: `SSH_HOST`, `SSH_PORT`, `SSH_USER`, `SSH_PRIVATE_KEY`, `SSH_KNOWN_HOSTS`. Host key должен быть заранее проверен. Токены Telegram/Polza остаются только в серверном `shared/.env`.

## Выпуск версии

`.github/workflows/ci-cd.yml` запускается при PR, push и вручную. CI делает Ruff, pytest, проверку Compose и shell-скриптов, собирает образы `bot` и `rag`, проверяет импорты в контейнерах. Деплой выполняется при push в `main` и ручном запуске на `main`; PR и другие ветки production не меняют.

```bash
python scripts/start.py                 # сервисы + индексация; ждёт deploy
python scripts/start.py --mode prepare  # доставка и сборка без запуска
python scripts/start.py --mode deploy   # запуск без индексации
```

Альтернатива: GitHub Actions → **CI and deploy** → **Run workflow**, выбрать `main` и режим. Команда `start.py` отслеживает конкретный запуск и проверяет результат job `deploy`. Она использует опубликованный commit, поэтому перед запуском локальные изменения нужно отправить в GitHub.

| Режим | Что происходит |
| --- | --- |
| `prepare` | Архив commit доставляется, проверяются данные, собираются образы, выставляется `pending` |
| `deploy` | Сервисы запускаются и проверяются; индексатор не стартует |
| `index` | То же, затем стартует/продолжает индексатор; Actions ждёт первых документов от рабочих потоков |

Индексация продолжается на VPS после завершения Actions. Успешный job `deploy` означает успешный старт и доступность первых документов, а не обработку всего корпуса.

## Что делает серверный скрипт

`scripts/remote_deploy.py` загружает `release.tar.gz` и `deploy.sh` по SFTP с проверкой SSH host key. `deploy.sh` берёт lock, распаковывает версию в `releases/<commit>`, копирует включённые в commit исходные данные в `shared/raw/` и создаёт ссылки на `shared/.env`, логи и хостовый монитор. Он добавляет `compose.server.yaml`, чтобы `indexer` читал `shared/raw/`. Затем собирает образы, запускает `service`, монитор, `redis`, `rag` и `bot`; выполняет smoke-проверку RAG и Redis. При ошибке до успешного переключения восстанавливает предыдущий release. После запуска `index` ждёт старта индексатора и первой доступной публикации.

```text
/opt/miem-bot/
├─ incoming/          архивы и серверные скрипты выпуска
├─ releases/<commit>/ код, Compose, образы этой версии
├─ current → releases/<commit>
├─ pending → releases/<commit>  только после prepare
└─ shared/
   ├─ .env             секреты
   ├─ raw/             ZIP и Excel
   ├─ logs/            логи приложения
   ├─ history/         SQLite-архив переписок и оценок
   └─ host/            host.json, лог и PID монитора
```

Compose volumes `index-data`, `model-cache`, `redis-data` и `monitor-data` переживают замену release. Их содержимое не входит в каталог `releases/`.

**Архив диалогов:** `deploy.sh` создаёт `/opt/miem-bot/shared/history/` и подключает его к обоим ботам. База сохраняется между релизами. Для просмотра и экспорта укажите `SERVICE_ADMIN_IDS` в серверном `.env` как JSON-массив Telegram ID; пустой список закрывает доступ к архиву. Подробности — в [переписках и оценках](history.md).
