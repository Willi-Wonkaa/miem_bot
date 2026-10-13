from datetime import datetime, timezone
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest
from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update, User

from app.telegram.bot import create_dispatcher
from app.telegram.formatting import answer_parts, format_telegram_answer
from app.telegram.history import HistoryStore


async def test_history_ttl_isolation_and_lock_owner():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = HistoryStore(redis, history_ttl=60, lock_ttl=600)
    owner = await store.acquire("chat:user")
    assert owner and await store.acquire("chat:user") is None
    await store.release("chat:user", "wrong-owner")
    assert await store.acquire("chat:user") is None
    for i in range(8):
        await store.append("chat:user", f"question {i}", f"answer {i}")
    history = await store.read("chat:user")
    assert len(history) == 10 and history[0] == "Пользователь: question 3"
    assert await store.read("other-chat:user") == []
    assert 0 < await redis.ttl("history:chat:user") <= 60
    await store.release("chat:user", owner)
    assert await store.acquire("chat:user")
    await store.clear("chat:user")
    assert await store.read("chat:user") == []
    await redis.aclose()


def test_formatting_escapes_html_and_preserves_links():
    result = format_telegram_answer("**ВШЭ** <script> [Сайт](https://hse.ru/?a=1&b=2)")
    assert "<b>ВШЭ</b>" in result and "&lt;script&gt;" in result
    assert '<a href="https://hse.ru/?a=1&amp;b=2">Сайт</a>' in result
    assert 'href="https://hse.ru/**path**"' in format_telegram_answer(
        "[Сайт](https://hse.ru/**path**)"
    )
    parts = answer_parts("🙂" * 5000 + " [Канал](https://max.ru/channel)")
    assert all(len(part.encode("utf-16-le")) // 2 < 4096 for part in parts)
    assert "max.ru" not in "".join(parts) and "https://www.hse.ru/" in "".join(parts)


@pytest.mark.parametrize(
    "coverage",
    [
        {"indexed_files": 1, "total_files": 205, "complete": False, "files": ["private.docx"]},
        {"indexed_files": 0, "total_files": 0, "complete": False},
        {"indexed_files": 205, "total_files": 205, "complete": True},
    ],
)
async def test_dispatcher_success_error_and_non_text(coverage):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = HistoryStore(redis, 60, 600)
    sent = []

    async def telegram_call(bot, method, timeout=None):
        if isinstance(method, SendMessage):
            sent.append(method.text)
            return Message(
                message_id=len(sent),
                date=datetime.now(timezone.utc),
                chat=Chat(id=42, type="private"),
                text=method.text,
            )
        return True

    bot = Bot(token="123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    bot.session.make_request = AsyncMock(side_effect=telegram_call)
    status = 200
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(
            status, json={"answer": "**Ответ**", "route": "rag", "coverage": coverage}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), base_url="http://rag"
    ) as client:
        dispatcher = create_dispatcher(store, client)

        async def feed(text=None):
            message = Message(
                message_id=99,
                date=datetime.now(timezone.utc),
                chat=Chat(id=42, type="private"),
                from_user=User(id=7, is_bot=False, first_name="Student"),
                text=text,
            )
            await dispatcher.feed_update(bot, Update(update_id=99, message=message))

        await feed("Что такое СОП")
        assert "<b>Ответ</b>" in sent
        if not coverage["complete"]:
            assert sent[-2] == "<b>Ответ</b>"
            assert sent[-1] == "Ответ может быть неполным: база знаний сейчас обновляется."
        else:
            assert sent[-1] == "<b>Ответ</b>"
        assert all(
            "205" not in text and "0/0" not in text and "private.docx" not in text for text in sent
        )
        assert len(await store.read("42:7")) == 2
        assert await redis.get("lock:42:7") is None
        status = 502
        await feed("Где библиотека")
        assert len(await store.read("42:7")) == 2
        assert "недоступен" in sent[-1]
        count = len(requests)
        await feed()
        assert "текстом" in sent[-1] and len(requests) == count
        owner = await store.acquire("42:7")
        await feed("Экзамены")
        assert "Предыдущий" in sent[-1] and len(requests) == count
        await store.release("42:7", owner)
        await feed("/reset")
        assert await store.read("42:7") == []
    await bot.session.close()
    await redis.aclose()
