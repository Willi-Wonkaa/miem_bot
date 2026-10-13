import asyncio
import contextlib
import logging
import math
import time
import uuid
from pathlib import Path

import httpx
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings
from app.logging_setup import setup_logging
from app.telegram.formatting import answer_parts, format_telegram_answer
from app.telegram.history import HistoryStore
from app.telemetry import Telemetry

log = logging.getLogger(__name__)


def session_id(message: Message) -> str:
    return f"{message.chat.id}:{message.from_user.id}"


class MetricsMiddleware(BaseMiddleware):
    def __init__(self, telemetry, run_id):
        self.telemetry, self.run_id = telemetry, run_id

    async def __call__(self, handler, message, data):
        started = time.perf_counter()
        self.telemetry.received(message.chat.id, message.message_id, self.run_id)
        log.info(
            "message_received chat=%d message=%d text_chars=%d",
            message.chat.id,
            message.message_id,
            len(message.text or ""),
        )
        try:
            result = await handler(message, data)
            self.telemetry.processed(
                message.chat.id, message.message_id, success=result is not False
            )
            log.info(
                "message_processed chat=%d message=%d seconds=%.3f success=%s",
                message.chat.id,
                message.message_id,
                time.perf_counter() - started,
                result is not False,
            )
            return result
        except Exception:
            self.telemetry.processed(message.chat.id, message.message_id, success=False)
            log.exception("message_handler_failed")
            self.telemetry.event(
                "errors", "bot", "Ошибка обработки сообщения. Подробности в bot.txt."
            )
            raise


def coverage_note(coverage):
    if not coverage or coverage.get("complete"):
        return ""
    return "Ответ может быть неполным: база знаний сейчас обновляется."


def create_dispatcher(
    store: HistoryStore, client: httpx.AsyncClient, telemetry=None, run_id=None
) -> Dispatcher:
    dp = Dispatcher()
    if telemetry is not None:
        dp.message.outer_middleware(MetricsMiddleware(telemetry, run_id))

    @dp.message(CommandStart())
    async def start(message: Message):
        await message.answer("Напиши вопрос про учебу в НИУ ВШЭ. /reset — очистить историю.")

    @dp.message(Command("index"))
    async def show_index(message: Message):
        response = await client.get("/index")
        response.raise_for_status()
        data = response.json()
        coverage = data.get("coverage", {})
        await message.answer(
            "База знаний готова."
            if coverage.get("complete")
            else "База знаний обновляется. Можно продолжать задавать вопросы."
        )

    @dp.message(Command("index_watch"))
    async def watch_index(message: Message):
        if telemetry is not None:
            telemetry.watch(message.chat.id)
            await message.answer(
                "Уведомления о новых проиндексированных файлах включены. /index_unwatch — выключить."
            )

    @dp.message(Command("index_unwatch"))
    async def unwatch_index(message: Message):
        if telemetry is not None:
            telemetry.watch(message.chat.id, enabled=False)
            await message.answer("Уведомления о файлах выключены.")

    @dp.message(Command("reset"))
    async def reset(message: Message):
        if message.from_user is None:
            return
        session = session_id(message)
        token = await store.acquire(session)
        if token is None:
            await message.answer("Дождись ответа на предыдущий вопрос, затем повтори /reset.")
            return
        try:
            await store.clear(session)
            await message.answer("История диалога очищена.")
        finally:
            await store.release(session, token)

    @dp.message(F.text)
    async def answer(message: Message, bot: Bot):
        if message.from_user is None:
            return
        question = message.text.strip()
        if not question or not any(c.isalnum() for c in question):
            await message.answer("Напиши вопрос текстом.")
            return
        if len(question) > 4000:
            await message.answer("Сократи вопрос до 4000 символов.")
            return
        session = session_id(message)
        token = None
        pending = None
        try:
            token = await store.acquire(session)
            if token is None:
                await message.answer("⏳ Предыдущий запрос ещё обрабатывается. Дождись ответа.")
                return
            pending = await message.answer("Обрабатываю вопрос…")
            async with ChatActionSender(bot=bot, chat_id=message.chat.id, action=ChatAction.TYPING):
                response = await client.post(
                    "/answer",
                    json={
                        "question": question,
                        "history": await store.read(session),
                    },
                )
                response.raise_for_status()
            result = response.json()
            answer_text = result["answer"]
            for part in answer_parts(answer_text):
                try:
                    await message.answer(format_telegram_answer(part), parse_mode=ParseMode.HTML)
                except TelegramBadRequest:
                    await message.answer(part, parse_mode=None)
            if note := coverage_note(result.get("coverage")):
                await message.answer(note)
            if result.get("route") != "warming":
                await store.append(session, question, answer_text)
        except httpx.HTTPStatusError as error:
            log.warning("RAG request failed: status=%d", error.response.status_code)
            await message.answer("Сервис сейчас занят или недоступен. Попробуй чуть позже.")
            if telemetry is not None:
                telemetry.event("errors", "bot", f"RAG ответил HTTP {error.response.status_code}.")
            return False
        except Exception:
            log.exception("message_processing_failed")
            await message.answer("Не удалось обработать вопрос. Попробуй ещё раз позже.")
            if telemetry is not None:
                telemetry.event("errors", "bot", "Ошибка обработки вопроса; см. bot.txt.")
            return False
        finally:
            if token is not None:
                try:
                    await store.release(session, token)
                except RedisError:
                    log.warning("Request lock release failed; TTL will expire it")
            if pending is not None:
                with contextlib.suppress(TelegramAPIError):
                    await bot.delete_message(message.chat.id, pending.message_id)

    @dp.message()
    async def non_text(message: Message):
        await message.answer("Я принимаю вопросы текстом.")

    return dp


async def heartbeat(redis: Redis, client: httpx.AsyncClient, bot: Bot, telemetry=None, run_id=None):
    path = Path("/tmp/bot-heartbeat")
    while True:
        try:
            await redis.ping()
            response = await client.get("/health")
            response.raise_for_status()
            await bot.get_me()
            path.write_text(str(time.time()), encoding="utf-8")
            if telemetry is not None:
                telemetry.component("bot", "ready", run_id)
        except Exception:
            log.warning("Bot dependency check failed")
        await asyncio.sleep(30)


async def file_notifications(bot, telemetry):
    cursor = telemetry.last_event()
    while True:
        events = telemetry.events(cursor)
        files = [event["text"] for event in events if event["kind"] == "files"]
        if files:
            text = "База знаний обновлена. Можно продолжать задавать вопросы."
            for chat in telemetry.watchers():
                try:
                    for part in answer_parts(text):
                        await bot.send_message(chat, part)
                except TelegramAPIError:
                    log.warning("file_notification_failed chat=%d", chat)
        if events:
            cursor = events[-1]["id"]
        await asyncio.sleep(5)


async def main():
    settings = Settings()
    setup_logging("bot", settings)
    settings.require("telegram_bot_token", "rag_api_token")
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    store = HistoryStore(redis, settings.history_ttl, math.ceil(settings.request_timeout) + 120)
    telemetry = Telemetry(settings.monitor_dir)
    run_id = uuid.uuid4().hex
    telemetry.component("bot", "starting", run_id)
    async with (
        httpx.AsyncClient(
            base_url=settings.rag_url,
            timeout=settings.request_timeout,
            headers={"Authorization": "Bearer " + settings.rag_api_token.get_secret_value()},
        ) as client,
        Bot(token=settings.telegram_bot_token.get_secret_value()) as bot,
    ):
        await redis.ping()
        dp = create_dispatcher(store, client, telemetry, run_id)
        task = asyncio.create_task(heartbeat(redis, client, bot, telemetry, run_id))
        notifications = asyncio.create_task(file_notifications(bot, telemetry))
        log.info("Telegram polling started")
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        finally:
            task.cancel()
            notifications.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            with contextlib.suppress(asyncio.CancelledError):
                await notifications
            telemetry.component("bot", "stopped", run_id)
            log.info("bot_stopped")
            await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
