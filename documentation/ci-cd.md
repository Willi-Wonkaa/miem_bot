# CI/CD

Workflow: `.github/workflows/ci-cd.yml`.
PR/push: Ruff, pytest, Compose validation, сборка двух образов, проверка импортов в контейнерах.
Push в `main` при `DEPLOY_ENABLED=true`: архив проверенного commit → SSH → сборка на VPS → проверка готового индекса → healthchecks → запуск.
Секреты и `.env` не входят в архив или образы. Образы собираются на сервере; registry не требуется.
При ошибке запуска скрипт восстанавливает предыдущий release.
Предыдущие версии и индексы сохраняются; очистку выполнять после проверки новой версии.

Repository settings → Secrets and variables → Actions → Secrets:

| Secret | Значение |
| --- | --- |
| `SSH_HOST` | IP VPS, сейчас `178.208.88.42` |
| `SSH_PORT` | `22` |
| `SSH_USER` | `miem-deploy` |
| `SSH_PRIVATE_KEY` | Полный приватный OpenSSH-ключ доступа `miem-deploy` |
| `SSH_KNOWN_HOSTS` | Проверенная запись host key VPS из `known_hosts` |

Repository variable `DEPLOY_ENABLED=true` включает автодеплой по push.
Пока рабочий индекс не готов, оставить `false`.
GitHub environment: `production`.

Actions → CI and deploy → Run workflow (ветка `main`):

- `prepare` — загрузить проверенный release и собрать образы на VPS; сервисы не запускаются.
- `deploy` — запустить сервисы с готовым актуальным индексом; работает и при выключенном автодеплое.

Индексация запускается отдельно вручную, без ограничения времени GitHub job.
Деплой не пересчитывает базу: при отсутствии или изменении индекса завершается с понятной ошибкой.

На сервере в `shared/.env`: `TELEGRAM_BOT_TOKEN`, `POLZA_API_KEY`, `RAG_API_TOKEN`.
Значения API-токенов в Actions не нужны. Пароль root в workflow не используется.
При коммите базы положить файлы в `data/raw/`; деплой скопирует их в `shared/raw/`.
