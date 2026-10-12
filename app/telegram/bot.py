import asyncio
import contextlib
import logging
import math
import os
import time
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings
from app.telegram.formatting import answer_parts, format_telegram_answer
from app.telegram.history import HistoryStore

log = logging.getLogger(__name__)


def session_id(message: Message) -> str:
    return f"{message.chat.id}:{message.from_user.id}"


def create_dispatcher(store: HistoryStore, client: httpx.AsyncClient) -> Dispatcher:
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def start(message: Message):
        await message.answer("Напиши вопрос про учебу в НИУ ВШЭ. /reset — очистить историю.")

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
            answer_text = response.json()["answer"]
            for part in answer_parts(answer_text):
                try:
                    await message.answer(format_telegram_answer(part), parse_mode=ParseMode.HTML)
                except TelegramBadRequest:
                    await message.answer(part, parse_mode=None)
            await store.append(session, question, answer_text)
        except httpx.HTTPStatusError as error:
            log.warning("RAG request failed: status=%d", error.response.status_code)
            await message.answer("Сервис сейчас занят или недоступен. Попробуй чуть позже.")
        except Exception:
            log.error("Message processing failed")
            await message.answer("Не удалось обработать вопрос. Попробуй ещё раз позже.")
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


async def heartbeat(redis: Redis, client: httpx.AsyncClient, bot: Bot):
    path = Path("/tmp/bot-heartbeat")
    while True:
        try:
            await redis.ping()
            response = await client.get("/health")
            response.raise_for_status()
            await bot.get_me()
            path.write_text(str(time.time()), encoding="utf-8")
        except Exception:
            log.warning("Bot dependency check failed")
        await asyncio.sleep(30)


async def main():
    settings = Settings()
    settings.require("telegram_bot_token", "rag_api_token")
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    store = HistoryStore(redis, settings.history_ttl, math.ceil(settings.request_timeout) + 120)
    async with (
        httpx.AsyncClient(
            base_url=settings.rag_url,
            timeout=settings.request_timeout,
            headers={"Authorization": "Bearer " + settings.rag_api_token.get_secret_value()},
        ) as client,
        Bot(token=settings.telegram_bot_token.get_secret_value()) as bot,
    ):
        await redis.ping()
        dp = create_dispatcher(store, client)
        task = asyncio.create_task(heartbeat(redis, client, bot))
        log.info("Telegram polling started")
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await redis.aclose()


if __name__ == "__main__":
    # aiogram's INFO update logs contain user IDs; keep production logs minimal.
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "WARNING"))
    asyncio.run(main())
