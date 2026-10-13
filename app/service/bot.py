import asyncio
import contextlib
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.config import Settings
from app.logging_setup import setup_logging
from app.telemetry import EVENT_TYPES, Telemetry, duration

log = logging.getLogger(__name__)


def index_status(progress):
    if not progress:
        return "Индексация ещё не запускалась."
    return (
        f"Индексатор: {progress.get('state', 'unknown')}\n"
        f"Файлы: {progress.get('done', 0)}/{progress.get('total', 0)}, "
        f"осталось {progress.get('remaining', 0)}, ошибок {progress.get('failed', 0)}\n"
        f"Прогресс: {progress.get('percent', 0):.1f}%\n"
        f"Прошло: {duration(progress.get('elapsed'))}; ETA ≈ {duration(progress.get('eta'))}\n"
        f"Текущий файл: {progress.get('current_file') or '—'}\n"
        f"Чанки файла: {progress.get('file_chunks_done', 0)}/{progress.get('file_chunks_total', 0)}\n"
        f"Опубликовано файлов: {progress.get('published_files', 0)}"
    )


def timestamp(value, timezone):
    if not value:
        return "—"
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    return datetime.fromtimestamp(value, ZoneInfo(timezone)).strftime("%d.%m %H:%M:%S")


def index_text(state):
    progress = state.get("index_progress")
    component = state.host().get("containers", {}).get("indexer", {})
    if component.get("state") not in {None, "running"}:
        if not progress:
            return "Индексатор остановлен. Сохранённый прогресс отсутствует; проверьте ошибки."
        if progress.get("state") not in {"completed", "completed_with_errors"}:
            progress = {
                **progress,
                "state": "остановлен (последний сохранённый прогресс)",
                "eta": None,
            }
    return index_status(progress)


def requests_text(state):
    stats = state.request_stats()
    return (
        "Запросы к системе\n"
        f"За 1 минуту: {stats['windows']['1']}\n"
        f"За 5 минут: {stats['windows']['5']}\n"
        f"За 30 минут: {stats['windows']['30']}\n"
        f"Всего: {stats['received']}\n"
        f"Обработано: {stats['processed']}; ошибок: {stats['errors']}"
    )


def status_text(state, settings):
    lines = ["Состояние системы"]
    host = state.host()
    if host and time.time() - host.get("updated", 0) < 60:
        for name, component in sorted(host.get("containers", {}).items()):
            lines.append(
                f"{name}: {component['state']} ({component.get('health', '—')}), "
                f"запуск {timestamp(component.get('started'), settings.timezone)}"
            )
        budget = host.get("budget", {})
        if budget:
            lines.append(
                f"Лимит индексатора: {budget.get('cpus', 0):.2f} CPU, "
                f"{budget.get('memory', 0) / 1024**3:.2f} ГБ RAM"
            )
    else:
        lines.append("Данные Docker ещё не поступили или устарели.")
    for component in state.components():
        lines.append(
            f"Компонент {component['name']}: {component['state']}, "
            f"запуск {timestamp(component['started'], settings.timezone)}"
        )
    lines.extend(["", index_text(state), "", requests_text(state), ""])
    stats = state.message_stats()
    lines.append(
        f"Сообщения за 1/5/30 минут: {stats['windows']['1']}/"
        f"{stats['windows']['5']}/{stats['windows']['30']}"
    )
    lines.append(
        f"С запуска основного бота: получено {stats['received']}, "
        f"обработано {stats['processed']}, ошибок {stats['errors']}"
    )
    return "\n".join(lines)[:3900]


def keyboard(prefs):
    rows = [[InlineKeyboardButton(text="📊 Статус системы", callback_data="status")]]
    rows.append([InlineKeyboardButton(text="Прогресс индексации", callback_data="index")])
    rows.append([InlineKeyboardButton(text="Количество запросов", callback_data="requests")])
    for kind, label in {"all": "Все уведомления", **EVENT_TYPES}.items():
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{'✅' if prefs.get(kind, True) else '⏸'} {label}",
                    callback_data=f"toggle:{kind}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def create_dispatcher(state, settings):
    dp = Dispatcher()

    def prefs(chat):
        state.subscribe(chat)
        return next(row["prefs"] for row in state.subscribers() if row["chat"] == chat)

    @dp.message(CommandStart(), F.chat.type == "private")
    async def start(message: Message):
        log.info("service_subscriber_activated chat=%d", message.chat.id)
        await message.answer(
            "Служебный бот активирован. Уведомления включены.\n\n" + status_text(state, settings),
            reply_markup=keyboard(prefs(message.chat.id)),
        )

    @dp.message(Command("status"), F.chat.type == "private")
    async def status(message: Message):
        await message.answer(
            status_text(state, settings), reply_markup=keyboard(prefs(message.chat.id))
        )

    @dp.message(Command("index", "progress"), F.chat.type == "private")
    async def index(message: Message):
        await message.answer(index_text(state))

    @dp.message(Command("requests", "stats"), F.chat.type == "private")
    async def requests(message: Message):
        await message.answer(requests_text(state))

    @dp.callback_query(F.data.in_({"status", "index", "requests"}))
    async def status_button(query: CallbackQuery):
        if query.message and query.message.chat.type == "private":
            text = {
                "status": lambda: status_text(state, settings),
                "index": lambda: index_text(state),
                "requests": lambda: requests_text(state),
            }[query.data]()
            await query.message.edit_text(
                text, reply_markup=keyboard(prefs(query.message.chat.id))
            )
        await query.answer()

    @dp.callback_query(F.data.startswith("toggle:"))
    async def toggle(query: CallbackQuery):
        if query.message and query.message.chat.type == "private":
            kind = query.data.split(":", 1)[1]
            if kind in {"all", *EVENT_TYPES}:
                updated = state.toggle(query.message.chat.id, kind)
                await query.message.edit_reply_markup(reply_markup=keyboard(updated))
                log.info(
                    "notification_toggle chat=%d kind=%s enabled=%s",
                    query.message.chat.id,
                    kind,
                    updated[kind],
                )
        await query.answer()

    return dp


async def monitor(bot, state, settings, run_id):
    next_report = time.monotonic() + settings.service_report_seconds
    previous = state.get("docker_seen", {})
    while True:
        try:
            host = state.host()
            if host and time.time() - host.get("updated", 0) < 60:
                for name, component in host.get("containers", {}).items():
                    identity = [
                        component.get("started"),
                        component.get("state"),
                        component.get("health"),
                    ]
                    if identity != previous.get(name):
                        state.event(
                            "startup" if component["state"] == "running" else "errors",
                            name,
                            f"Контейнер {name}: {component['state']} ({component.get('health', '—')})\n"
                            f"Запуск: {timestamp(component.get('started'), settings.timezone)}",
                        )
                        previous[name] = identity
                state.put("docker_seen", previous)
            if time.monotonic() >= next_report:
                state.event(
                    "traffic",
                    "bot",
                    requests_text(state),
                )
                next_report = time.monotonic() + settings.service_report_seconds
            for subscriber in state.subscribers():
                for event in state.events(subscriber["cursor"], limit=25):
                    prefs = subscriber["prefs"]
                    if prefs.get("all") and prefs.get(event["kind"], False):
                        try:
                            await bot.send_message(subscriber["chat"], event["text"][:3900])
                        except TelegramAPIError:
                            log.warning("notification_delivery_failed chat=%d", subscriber["chat"])
                            break
                    state.advance(subscriber["chat"], event["id"])
            state.component("service", run_id=run_id)
            Path("/tmp/service-heartbeat").write_text(str(time.time()), encoding="utf-8")
        except Exception:
            log.exception("service_monitor_failed")
        await asyncio.sleep(3)


async def main():
    settings = Settings()
    setup_logging("service", settings)
    settings.require("service_bot_token")
    state = Telemetry(settings.monitor_dir)
    run_id = uuid.uuid4().hex
    state.component("service", "starting", run_id)
    async with Bot(token=settings.service_bot_token.get_secret_value()) as bot:
        await bot.get_me()
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Включить уведомления"),
                BotCommand(command="status", description="Состояние системы"),
                BotCommand(command="index", description="Прогресс индексации"),
                BotCommand(command="requests", description="Количество запросов"),
            ]
        )
        dp = create_dispatcher(state, settings)
        task = asyncio.create_task(monitor(bot, state, settings, run_id))
        log.info("service_polling_started")
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            state.component("service", "stopped", run_id)
            log.info("service_stopped")


if __name__ == "__main__":
    asyncio.run(main())
