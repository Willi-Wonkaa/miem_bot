# CI/CD

Workflow: `.github/workflows/ci-cd.yml`.
PR/push: Ruff, pytest, Compose validation, сборка двух образов, проверка импортов в контейнерах.
Push в `main`: архив проверенного commit → SSH → сборка на VPS → индексатор → healthchecks → запуск.
Секреты и `.env` не входят в архив или образы. Образы собираются на сервере; registry не требуется.
При ошибке запуска скрипт восстанавливает предыдущий release и указатель индекса.
Предыдущие версии и индексы сохраняются; очистку выполнять после проверки новой версии.

Repository settings → Secrets and variables → Actions → Secrets:

| Secret | Значение |
| --- | --- |
| `SSH_HOST` | IP VPS, сейчас `178.208.88.42` |
| `SSH_PORT` | `22` |
| `SSH_USER` | `miem-deploy` |
| `SSH_PRIVATE_KEY` | Полный приватный OpenSSH-ключ доступа `miem-deploy` |
| `SSH_KNOWN_HOSTS` | Проверенная запись host key VPS из `known_hosts` |

Repository variable `DEPLOY_ENABLED=true` включает автодеплой.
До добавления базы и заполнения серверного `.env` оставить `false`.
GitHub environment: `production`. Workflow запускается вручную через Actions → Run workflow.

На сервере в `shared/.env`: `TELEGRAM_BOT_TOKEN`, `POLZA_API_KEY`, `RAG_API_TOKEN`.
Значения API-токенов в Actions не нужны. Пароль root в workflow не используется.
При коммите базы положить файлы в `data/raw/`; деплой скопирует их в `shared/raw/`.
