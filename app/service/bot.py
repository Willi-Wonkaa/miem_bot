import asyncio
import contextlib
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
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
    states = {
        "preparing": "Подготовка", "analyzing": "Анализ корпуса",
        "indexing": "Обработка корпуса", "extras": "Обработка FAQ и ссылок",
        "completed": "Завершена", "completed_with_errors": "Завершена с ошибками",
        "interrupted": "Остановлена; прогресс сохранён",
    }
    phase = states.get(progress.get("state"), progress.get("state", "Ожидание"))
    lines = ["Индексация корпуса", phase, ""]
    lines.append(f"Общий прогресс: {progress.get('percent', 0):.1f}%")
    lines.append(f"Документы: {progress.get('done', 0)}/{progress.get('total', 0)} готово; "
                 f"ошибок {progress.get('failed', 0)}")
    lines.append(f"Доступно для ответов: {progress.get('published_files', 0)} документов")
    if progress.get("analysis_complete"):
        lines.append(f"Объём: {progress.get('total_bytes', 0) / 1024**2:.1f} МБ")
        formats = progress.get("formats", {})
        if formats:
            lines.append("Форматы: " + ", ".join(f"{k.upper()} — {v}" for k, v in sorted(formats.items())))
        lines.append(f"Весь корпус: {progress.get('processed_units', 0)}/"
                     f"{progress.get('total_units', 0)} фрагментов и записей обработано")
        lines.append(f"Дополнительно: FAQ — {progress.get('faq_total', 0)}, "
                     f"ссылок — {progress.get('links_total', 0)}")
    else:
        lines.append(f"Проанализировано документов: {progress.get('parsed_files', 0)}/"
                     f"{progress.get('total', 0)}")
    workers = progress.get("workers", {})
    if workers:
        active = sum(worker.get("state") == "running" for worker in workers.values())
        lines.append(f"Индексаторы: {len(workers)}; работают {active}; "
                     f"прошли проверку старта {progress.get('workers_ready', 0)}")
    lines.extend(["", f"Прошло: {duration(progress.get('elapsed'))}",
                  f"Осталось примерно: {duration(progress.get('eta'))}"])
    return "\n".join(lines)


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
    text = index_status(progress)
    publication = state.get("index_publication", {})
    if (progress and progress.get("state") == "completed" and publication.get("complete")
            and publication.get("build_hash") == progress.get("build_hash")
            and progress.get("done") == progress.get("total") and not progress.get("failed")):
        text += "\n\nПроверка: все документы обработаны, полная база опубликована."
    return text


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
    lines = ["Система", ""]
    names = {"service": "Служебный бот", "bot": "Основной бот", "rag": "Ответы (RAG)",
             "redis": "История диалогов", "indexer": "Индексация"}
    host = state.host()
    if host and time.time() - host.get("updated", 0) < 60:
        for name in ["service", "bot", "rag", "redis", "indexer"]:
            component = host.get("containers", {}).get(name)
            if not component:
                continue
            running = component.get("state") == "running"
            label = "Работает" if running and component.get("health") in {"healthy", "—"} else (
                "Запускается" if running else "Остановлен")
            if name == "indexer" and component.get("state") == "exited" and component.get("exit_code") == 0:
                label = "Обработка завершена"
            lines.append(
                f"{names[name]}: {label}"
            )
        budget = host.get("budget", {})
        if budget:
            lines.append(
                f"Лимит индексатора: {budget.get('cpus', 0):.2f} CPU, "
                f"{budget.get('memory', 0) / 1024**3:.2f} ГБ RAM"
            )
    else:
        lines.append("Данные Docker ещё не поступили или устарели.")
    progress = state.get("index_progress", {})
    lines.extend(["", f"Корпус: {progress.get('percent', 0):.1f}%; "
                  f"готово документов {progress.get('done', 0)}/{progress.get('total', 0)}",
                  "Подробный прогресс — кнопка «Индексация»."])
    stats = state.request_stats()
    lines.extend(["", f"Всего запросов: {stats['received']}; "
                  f"обработано {stats['processed']}; ошибок {stats['errors']}"])
    return "\n".join(lines)[:3900]


def keyboard(prefs, view="status"):
    rows = [[InlineKeyboardButton(text="Статус", callback_data="status"),
             InlineKeyboardButton(text="Индексация", callback_data="index"),
             InlineKeyboardButton(text="Запросы", callback_data="requests")]]
    if view == "notifications":
        for kind, label in {"all": "Все уведомления", **EVENT_TYPES}.items():
            rows.append([InlineKeyboardButton(
                text=f"{label}: {'вкл' if prefs.get(kind, True) else 'выкл'}",
                callback_data=f"toggle:{kind}",
            )])
    else:
        rows.append([InlineKeyboardButton(text="Обновить", callback_data=view),
                     InlineKeyboardButton(text="Уведомления", callback_data="notifications")])
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
        await message.answer(index_text(state), reply_markup=keyboard(prefs(message.chat.id), "index"))

    @dp.message(Command("requests", "stats"), F.chat.type == "private")
    async def requests(message: Message):
        await message.answer(requests_text(state), reply_markup=keyboard(prefs(message.chat.id), "requests"))

    @dp.callback_query(F.data.in_({"status", "index", "requests", "notifications"}))
    async def status_button(query: CallbackQuery):
        if query.message and query.message.chat.type == "private":
            text = {
                "status": lambda: status_text(state, settings),
                "index": lambda: index_text(state),
                "requests": lambda: requests_text(state),
                "notifications": lambda: "Уведомления\n\nАнализ, запуск индексаторов и завершение "
                    "приходят автоматически. Подробные файлы и статистику можно включить здесь.",
            }[query.data]()
            try:
                await query.message.edit_text(
                    text, reply_markup=keyboard(prefs(query.message.chat.id), query.data)
                )
            except TelegramBadRequest as error:
                if "message is not modified" not in str(error).lower():
                    raise
        await query.answer()

    @dp.callback_query(F.data.startswith("toggle:"))
    async def toggle(query: CallbackQuery):
        if query.message and query.message.chat.type == "private":
            kind = query.data.split(":", 1)[1]
            if kind in {"all", *EVENT_TYPES}:
                updated = state.toggle(query.message.chat.id, kind)
                await query.message.edit_reply_markup(reply_markup=keyboard(updated, "notifications"))
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
                        if name == "indexer" and component.get("state") == "exited" and component.get("exit_code") == 0:
                            previous[name] = identity
                            continue
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
                    if prefs.get("all") and prefs.get(event["kind"], event["kind"] == "completion"):
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
