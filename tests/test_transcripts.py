import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest
from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update, User

from app.telegram.archive import OutgoingArchive
from app.telegram.bot import create_dispatcher
from app.telegram.feedback import maintain
from app.telegram.history import HistoryStore
from app.transcripts import Transcripts


def saved_answer(archive, now=1000):
    archive.user(7, "Student")
    question = archive.message(7, "user", "question", now, "incoming:7:10")
    answer = archive.answer(
        7,
        question,
        {
            "answer": "answer",
            "answered_at": now,
            "model": "test-model",
        },
    )
    archive.sent_feedback(answer, "outgoing:7:11", now)
    return question, answer


@pytest.mark.parametrize("score", [-1, 1])
def test_rating_is_single_use_owned_and_linked(tmp_path, monkeypatch, score):
    monkeypatch.setattr("app.transcripts.time.time", lambda: 1000)
    archive = Transcripts(tmp_path)
    question, answer = saved_answer(archive)
    with archive.connection() as db:
        assert db.execute("SELECT helpful FROM feedback").fetchone()[0] == 0
    for owner, key in [(8, "outgoing:7:11"), (7, "outgoing:7:12")]:
        with pytest.raises(ValueError):
            archive.rate(answer, owner, score, "invalid", key)
    archive.rate(answer, 7, score, "vote:1", "outgoing:7:11")
    with pytest.raises(ValueError):
        archive.rate(answer, 7, -score, "vote:2", "outgoing:7:11")
    reopened = Transcripts(tmp_path)
    cleanup = reopened.cleanup_buttons()
    assert len(cleanup) == 1 and cleanup[0]["helpful"] == score
    assert cleanup[0]["answer_id"] == answer
    reopened.close_buttons(answer)
    assert not reopened.cleanup_buttons()
    assert reopened.full_answer(answer)["question_id"] == question


def test_ten_minute_expiry_survives_restart_and_keeps_zero(tmp_path, monkeypatch):
    monkeypatch.setattr("app.transcripts.time.time", lambda: 1599)
    archive = Transcripts(tmp_path)
    _, answer = saved_answer(archive)
    assert not archive.cleanup_buttons()
    monkeypatch.setattr("app.transcripts.time.time", lambda: 1600)
    reopened = Transcripts(tmp_path)
    assert reopened.cleanup_buttons()[0]["answer_id"] == answer
    with pytest.raises(ValueError):
        reopened.rate(answer, 7, 1, "late", "outgoing:7:11")
    reopened.close_buttons(answer)
    with reopened.connection() as db:
        assert db.execute("SELECT helpful FROM feedback").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0] == 0


def test_full_history_and_feedback_export(tmp_path, monkeypatch):
    monkeypatch.setattr("app.transcripts.time.time", lambda: 1000)
    archive = Transcripts(tmp_path)
    _, answer = saved_answer(archive)
    archive.rate(answer, 7, -1, "vote:1", "outgoing:7:11")
    archive.prompt("7:12", 7, answer)
    archive = Transcripts(tmp_path)
    assert archive.pending("7:12", 8) is None
    assert archive.pending("7:12", 7) == answer
    comment = archive.message(7, "user", "Wrong information", 1001, "incoming:7:13")
    archive.comment(answer, 7, comment, "Wrong information", "7:12")
    assert archive.pending("7:12", 7) is None
    for number in range(20):
        archive.message(7, "bot", "x" * 5000, 1002 + number, f"outgoing:7:{100 + number}")
    archive.user(8, "Other")
    archive.message(8, "user", "private", 1000, "incoming:8:1")
    path = archive.export(7)
    content = path.read_text(encoding="utf-8")
    exported = json.loads(content)
    assert content.startswith('{\n  "user": {\n')
    assert '\n  "messages": [\n    {\n' in content
    assert len(exported["messages"]) == 22
    assert all(row["text"] != "private" for row in exported["messages"])
    assert (
        exported["answers"][0]["answered"] == datetime.fromtimestamp(1000, timezone.utc).isoformat()
    )
    feedback = exported["feedback"][0]
    assert feedback["helpful"] == -1
    assert feedback["comment_message_id"] == comment
    assert feedback["answer_id"] == answer
    assert len(exported["feedback_events"]) == 2
    path.unlink()


async def test_maintenance_removes_expired_buttons_and_prompts_negative(tmp_path, monkeypatch):
    monkeypatch.setattr("app.transcripts.time.time", lambda: 1000)
    archive = Transcripts(tmp_path)
    _, answer = saved_answer(archive)
    archive.rate(answer, 7, -1, "vote", "outgoing:7:11")
    bot = AsyncMock()
    bot.send_message.return_value = Message(
        message_id=12,
        date=datetime.fromtimestamp(1000, timezone.utc),
        chat=Chat(id=7, type="private"),
        text="Что не так?",
    )

    async def stop(delay):
        raise asyncio.CancelledError

    monkeypatch.setattr("app.telegram.feedback.asyncio.sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await maintain(bot, archive)
    bot.edit_message_reply_markup.assert_awaited_once_with(
        chat_id=7,
        message_id=11,
        reply_markup=None,
    )
    assert bot.send_message.call_args.args == (7, "Что не так?")
    assert archive.pending("7:12", 7) == answer
    assert not archive.cleanup_buttons()
    assert not archive.prompt_queue()


async def test_every_model_answer_has_buttons_and_archive_survives_reset(tmp_path):
    archive = Transcripts(tmp_path)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = HistoryStore(redis, 60, 600)
    sent = []

    async def telegram_call(bot, method, timeout=None):
        if isinstance(method, SendMessage):
            sent.append(method)
            return Message(
                message_id=len(sent) + 100,
                date=datetime.now(timezone.utc),
                chat=Chat(id=7, type="private"),
                text=method.text,
            )
        return True

    bot = Bot(token="123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    bot.session.make_request = AsyncMock(side_effect=telegram_call)
    bot.session.middleware(OutgoingArchive(archive))
    async with httpx.AsyncClient(
        base_url="http://rag",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "answer": "answer",
                    "answered_at": "2026-10-13T08:00:00+00:00",
                    "model": "model",
                    "coverage": {"complete": True},
                },
            )
        ),
    ) as client:
        dp = create_dispatcher(store, client, archive=archive)
        for number, text in enumerate(["question one", "question two", "/reset"]):
            await dp.feed_update(
                bot,
                Update(
                    update_id=number,
                    message=Message(
                        message_id=number + 1,
                        date=datetime.now(timezone.utc),
                        chat=Chat(id=7, type="private"),
                        from_user=User(id=7, is_bot=False, first_name="Student"),
                        text=text,
                    ),
                ),
            )
        responses = [method for method in sent if method.text == "answer"]
        assert len(responses) == 2
        assert all(len(method.reply_markup.inline_keyboard[0]) == 2 for method in responses)
        assert await store.read("7:7") == []
        with archive.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM answers").fetchone()[0] == 2
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM feedback WHERE helpful=0 AND buttons_active=1"
                ).fetchone()[0]
                == 2
            )
            assert db.execute("SELECT COUNT(*) FROM messages WHERE role='user'").fetchone()[0] == 3
    await redis.aclose()
    await bot.session.close()
