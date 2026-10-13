# CI/CD

Workflow: `.github/workflows/ci-cd.yml`.
PR/push: Ruff, pytest, Compose validation, сборка двух образов, проверка импортов в контейнерах.
Push в `main` при `DEPLOY_ENABLED=true`: архив проверенного commit → SSH → сборка → служебный бот → монитор → остальные сервисы.
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
Пока не нужно автоматически запускать сервисы, оставить `false`.
GitHub environment: `production`.

Actions → CI and deploy → Run workflow (ветка `main`):

Или одна команда `python scripts/start.py` (по умолчанию `index`). Нужны Python 3.12
и GitHub-аутентификация в Git либо `GH_TOKEN`/`GITHUB_TOKEN`. Значения токенов не выводятся.
Опциональный `request_id` связывает команду с конкретным workflow; чужой запуск
или успешный CI с пропущенным deploy не выдаётся за успешный запуск системы.

- `prepare` — загрузить проверенный release и собрать образы на VPS; сервисы не запускаются.
- `deploy` — запустить сервисы, в том числе с частичным или ещё пустым индексом.
- `index` — запустить сервисы и индексатор с восстановлением сохранённого прогресса.

Индексация запускается только при явном выборе `index`; обычный деплой её не запускает.
Job допускает до 180 минут, сохраняет файлы и батчи для восстановления после прерывания.
Разработка и все проверки выполняются через Git/Actions; команды на VPS выполняет pipeline.

На сервере в `shared/.env`: `TELEGRAM_BOT_TOKEN`, `SERVICE_BOT_TOKEN`, `POLZA_API_KEY`, `RAG_API_TOKEN`.
Значения API-токенов в Actions не нужны. Пароль root в workflow не используется.
При коммите базы положить файлы в `data/raw/`; деплой скопирует их в `shared/raw/`.

После запуска контейнеров deployment проверяет Redis, `/health`, защищённый `/index`
и отказ `/answer` при неверном токене. Режим `index` ждёт первого доступного документа,
потом завершения индексатора и полного индекса; заключительная проверка выполняет
реальный вопрос через `/answer` (при маршруте RAG расходуются токены Polza).
Неполная база, даже при exit code 0 индексатора, не считается успешным результатом.

Отдельный workflow `status.yml` (`System status`) проверяет сервер без перезапуска:
Docker state/health, CPU/RAM, настройки ресурсов и модели (без токенов), монитор,
RAG health/auth, сохранённый прогресс и последние логи индексатора.
Он не блокируется выполняющейся индексацией из-за concurrency основного workflow.
Запуск: `gh workflow run status.yml --ref main` либо Actions → System status → Run workflow.
По умолчанию диагностика ничего не перезапускает. Параметр `repair_monitor=true`
перезапускает только монитор VPS; он позволяет освободить унаследованную монитором
блокировку старых версий deployment. Новая версия закрывает descriptor 9 перед `nohup`.
