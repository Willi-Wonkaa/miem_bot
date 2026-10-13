# Монитор VPS

`app/host_monitor.py` работает на хосте, вне Docker. `scripts/deploy.sh` запускает его через `scripts/start_monitor.sh` после старта служебного бота. Процесс опрашивает Docker и ресурсы хоста каждые 10 секунд, пишет состояние в `/opt/miem-bot/shared/host/host.json` и лог `host-monitor.txt`.

Монитор оставляет резерв CPU/RAM для системы и управляет контейнером `indexer`: снижает CPU-квоту при нагрузке, а при нехватке памяти просит сохранить checkpoints, останавливает контейнер и позже возобновляет его. Для фактических лимитов смотрите Docker и `host.json`: после `docker update` они могут отличаться от `INDEX_CPU_LIMIT`/`INDEX_MEM_LIMIT` в Compose.

Основной деплой запускает монитор через `nohup`, поэтому автоматический старт самого процесса после перезагрузки VPS этим способом не обеспечен. `scripts/install_monitor.sh` содержит отдельную установку systemd-сервиса, но pipeline его не вызывает. После рестарта VPS проверьте время `updated` в `shared/host/host.json`; диагностика доступна через [System status](operations.md). Ручной параметр `repair_monitor=true` в workflow перезапускает только монитор.

В контейнеры Docker socket не монтируется. Служебный бот читает сохранённый статус хоста из bind mount.
