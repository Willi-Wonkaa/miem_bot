import asyncio
import logging
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update, User

from app.config import Settings
from app.host_monitor import budget
from app.logging_setup import setup_logging
from app.service.bot import create_dispatcher, keyboard, monitor, status_text
from app.telemetry import Telemetry


def test_file_logs_redact_credentials_and_rotate(tmp_path):
    settings = Settings(
        _env_file=None,
        log_dir=tmp_path,
        log_max_bytes=1024,
        telegram_bot_token="123456:secret-token",
        polza_api_key="private-polza-key",
    )
    setup_logging("test", settings)
    logger = logging.getLogger("app.logging_test")
    for _ in range(20):
        logger.info(
            "provider private-polza-key telegram /bot123456:secret-token/getMe %s", "x" * 100
        )
    files = list(tmp_path.glob("test.txt*"))
    assert len(files) > 1
    content = "".join(path.read_text(encoding="utf-8") for path in files)
    assert "[REDACTED]" in content
    assert "private-polza-key" not in content and "secret-token" not in content


def test_metrics_windows_deduplication_and_restart(tmp_path):
    state = Telemetry(tmp_path)
    state.component("bot", run_id="first")
    now = time.time()
    for message, age in [(1, 10), (2, 120), (3, 600), (4, 1900)]:
        state.received(100, message, "first")
        with state.connection() as db:
            db.execute("UPDATE messages SET received=? WHERE message=?", (now - age, message))
    state.received(100, 1, "first")
    state.processed(100, 1)
    state.processed(100, 2, success=False)
    stats = state.message_stats(now)
    assert stats == {
        "windows": {"1": 1, "5": 2, "30": 3},
        "received": 4,
        "processed": 2,
        "errors": 1,
    }
    state.component("bot", run_id="second")
    assert state.message_stats(now)["processed"] == 0
    assert state.message_stats(now)["windows"]["30"] == 3


def test_notification_preferences_survive_restart_and_are_per_chat(tmp_path):
    state = Telemetry(tmp_path)
    state.subscribe(1)
    state.subscribe(2)
    state.event("files", "indexer", "one.txt")
    changed = state.toggle(1, "files")
    assert changed["files"] is True and changed["all"] is True
    restarted = Telemetry(tmp_path)
    subscriptions = {row["chat"]: row for row in restarted.subscribers()}
    assert subscriptions[1]["prefs"]["files"] is True
    assert subscriptions[2]["prefs"]["files"] is False
    assert subscriptions[1]["cursor"] == state.last_event()
    assert restarted.toggle(1, "all")["all"] is False
    callbacks = [button.callback_data for row in keyboard(changed, "notifications").inline_keyboard
                 for button in row]
    assert "status" in callbacks and "toggle:all" in callbacks and "toggle:traffic" in callbacks
    assert "index" in callbacks and "requests" in callbacks


def test_request_counters_survive_restart_and_track_failures(tmp_path):
    state = Telemetry(tmp_path)
    first = state.request_received()
    second = state.request_received()
    state.request_processed(first)
    state.request_processed(second, success=False)
    state.request_processed(first)
    restarted = Telemetry(tmp_path)
    assert restarted.request_stats() == {
        "windows": {"1": 2, "5": 2, "30": 2},
        "received": 2,
        "processed": 2,
        "errors": 1,
    }


def test_resource_budget_reserves_headroom_and_uses_at_most_80_percent():
    gib = 1024**3
    limits = budget(3, 1, 8 * gib, 5 * gib, indexer_cpu=1, indexer_ram=gib)
    assert limits["cpus"] == 1.6
    assert limits["memory"] == int(3 * gib * 0.8)
    assert budget(3, 0, 8 * gib, 0)["cpus"] == 0
    assert budget(3, 0, 8 * gib, 0)["memory"] == 0
    assert budget(3, 0.061, 8 * gib, 5 * gib)["cpus"] <= 0.061 * 0.8


async def test_service_bot_activation_and_status(tmp_path):
    state = Telemetry(tmp_path / "monitor")
    settings = Settings(_env_file=None, monitor_dir=tmp_path / "monitor")
    replies = []

    async def telegram_call(bot, method, timeout=None):
        if isinstance(method, SendMessage):
            replies.append(method)
            return Message(
                message_id=1,
                date=datetime.now(timezone.utc),
                chat=Chat(id=55, type="private"),
                text=method.text,
            )
        return True

    bot = Bot(token="123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    bot.session.make_request = AsyncMock(side_effect=telegram_call)
    dispatcher = create_dispatcher(state, settings)
    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=55, type="private"),
        from_user=User(id=55, is_bot=False, first_name="Admin"),
        text="/start",
    )
    await dispatcher.feed_update(bot, Update(update_id=1, message=message))
    assert state.subscribers()[0]["chat"] == 55
    assert "Служебный бот активирован" in replies[-1].text
    assert replies[-1].reply_markup.inline_keyboard[0][0].callback_data == "status"
    assert "Всего запросов" in status_text(state, settings)
    state.put(
        "index_progress",
        {
            "state": "indexing",
            "done": 1,
            "total": 205,
            "percent": 0.5,
            "remaining": 204,
            "current_file": "example.docx",
        },
    )
    await dispatcher.feed_update(
        bot,
        Update(update_id=2, message=message.model_copy(update={"message_id": 2, "text": "/index"})),
    )
    assert "1/205" in replies[-1].text and "example.docx" not in replies[-1].text
    assert "Чанки файла" not in replies[-1].text
    request_id = state.request_received()
    state.request_processed(request_id)
    await dispatcher.feed_update(
        bot,
        Update(
            update_id=3, message=message.model_copy(update={"message_id": 3, "text": "/requests"})
        ),
    )
    assert "Всего: 1" in replies[-1].text and "Обработано: 1" in replies[-1].text
    await bot.session.close()


async def test_completion_notification_is_delivered_independently_of_progress_prefs(tmp_path, monkeypatch):
    state = Telemetry(tmp_path)
    state.subscribe(55)
    state.toggle(55, "indexer")
    state.event("completion", "indexer", "Индексация завершена. База знаний полностью обновлена.")
    bot = AsyncMock()
    settings = Settings(_env_file=None, monitor_dir=tmp_path)

    async def stop_after_cycle(seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr("app.service.bot.asyncio.sleep", stop_after_cycle)
    with pytest.raises(asyncio.CancelledError):
        await monitor(bot, state, settings, "test-service")
    bot.send_message.assert_awaited_once_with(
        55, "Индексация завершена. База знаний полностью обновлена."
    )
