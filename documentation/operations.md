# Эксплуатация и диагностика

## Проверка состояния без перезапуска

`.github/workflows/status.yml` запускает диагностику VPS отдельно от основного workflow:

```bash
gh workflow run status.yml --ref main
```

Результат содержит состояния контейнеров, CPU/RAM, состояние монитора, прогресс индексации и последние логи индексатора. По умолчанию сервисы не перезапускаются. В ручном запуске есть параметр `repair_monitor=true` — он перезапускает только хостовый монитор.

На VPS в активной версии:

```bash
cd /opt/miem-bot/current
export RELEASE_TAG=$(basename "$(pwd -P)")
docker compose -f compose.yaml -f compose.server.yaml ps
docker compose -f compose.yaml -f compose.server.yaml logs --tail=100 bot rag service
```

Контейнер `indexer` запускается отдельно с именем `miem-bot-indexer`. Для него используйте `docker logs --tail=100 miem-bot-indexer` и `docker inspect miem-bot-indexer`. Текстовые логи приложения лежат в `/opt/miem-bot/shared/logs/`; монитор пишет в `shared/host/host-monitor.txt` и `monitor-console.txt`.

## Что смотреть при неполной базе

- `/health` может быть здоров при `index_ready=false`: это означает работающий API без опубликованной базы.
- Команда `/index` служебного бота показывает состояние анализа, документы, ошибки и ETA. `/index` основного бота даёт краткий статус.
- Ошибки файлов не уничтожают уже опубликованную часть корпуса. Полная готовность означает `complete=true` в метаданных текущего поколения и отсутствие ошибок.
- Если индексация остановилась, повторный `index` через Actions использует checkpoints. При нехватке памяти хостовый монитор может приостановить контейнер и возобновить его автоматически.

## Данные и резервная копия

Сохраняйте `/opt/miem-bot/shared/` и Docker volumes `index-data`, `redis-data`, `monitor-data`. `model-cache` можно восстановить загрузкой модели, но его сохранение ускорит запуск. Архив диалогов находится в `shared/history/`. Для работающей SQLite-базы используйте backup API или сохраняйте файл вместе с WAL-файлами; детали — в [описании архива](history.md). Не запускайте `docker compose down -v` без намерения удалить volumes.

`/reset` в основном боте очищает только краткую Redis-историю. Счётчики и подписки служебного бота хранятся отдельно в `monitor-data`. Параметры и токены меняются в серверном `shared/.env`; деплой использует именно его, не локальный `.env`.
