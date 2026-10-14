import json
from datetime import datetime, timezone
from zipfile import ZipFile

from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendDocument
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.config import Settings
from app.service.bot import create_dispatcher, keyboard
from app.service.exports import export_keyboard
from app.telemetry import Telemetry
from app.transcripts import Transcripts


def test_all_chats_export_keeps_each_users_complete_history(tmp_path):
    archive = Transcripts(tmp_path)
    for user_id in (7, 8):
        archive.user(user_id, f"User {user_id}")
        question = archive.message(user_id, "user", f"question {user_id}", 1000, f"in:{user_id}")
        archive.answer(user_id, question, {"answer": f"answer {user_id}", "answered_at": 1001})
        archive.message(user_id, "bot", f"answer {user_id}", 1002, f"out:{user_id}")
    path = archive.export_all()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    finally:
        path.unlink()
    assert [user["user"]["id"] for user in data["users"]] == [7, 8]
    assert [row["role"] for row in data["users"][0]["messages"]] == ["user", "bot"]
    assert data["users"][1]["answers"][0]["text"] == "answer 8"
    assert all(len(user["feedback"]) == 1 for user in data["users"])


async def test_log_menu_and_downloads_are_admin_only_and_bundle_is_one_document(tmp_path, monkeypatch):
    monkeypatch.setattr("app.service.exports.ZoneInfo", lambda _: timezone.utc)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "bot.txt").write_text("bot log", encoding="utf-8")
    (log_dir / "rag.txt").write_text("rag log", encoding="utf-8")
    (log_dir / "parser-42.txt").write_text("parser log", encoding="utf-8")
    settings = Settings(
        _env_file=None, log_dir=log_dir, conversation_dir=tmp_path / "history",
        monitor_dir=tmp_path / "monitor", service_admin_ids=(55,), timezone="UTC",
    )
    archive = Transcripts(settings.conversation_dir)
    archive.user(7, "Student")
    archive.message(7, "user", "hello", 1000, "in:7")
    state = Telemetry(settings.monitor_dir)
    dispatcher = create_dispatcher(state, settings)
    bot = Bot(token="123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    sent = []
    edited = []
    alerts = []

    async def telegram_call(bot, method, timeout=None):
        if isinstance(method, SendDocument):
            path = method.document.path
            payload = path.read_bytes()
            sent.append((method.document.filename, method.caption, payload))
            return Message(
                message_id=2, date=datetime.now(timezone.utc),
                chat=Chat(id=55, type="private"),
            )
        if isinstance(method, EditMessageText):
            edited.append(method)
            return Message(
                message_id=1, date=datetime.now(timezone.utc),
                chat=Chat(id=55, type="private"), text=method.text,
            )
        if isinstance(method, AnswerCallbackQuery):
            alerts.append(method)
            return True
        raise AssertionError(type(method))

    bot.session.make_request = telegram_call

    async def click(data, user_id=55):
        query = CallbackQuery(
            id=str(len(alerts) + 1), from_user=User(id=user_id, is_bot=False, first_name="Admin"),
            chat_instance="private", data=data,
            message=Message(
                message_id=1, date=datetime.now(timezone.utc),
                chat=Chat(id=55, type="private"), text="menu",
            ),
        )
        await dispatcher.feed_update(bot, Update(update_id=len(alerts) + 1, callback_query=query))

    try:
        assert "logs:menu" in {
            button.callback_data for row in keyboard({"_history_admin": True}).inline_keyboard
            for button in row
        }
        assert "logs:menu" not in {
            button.callback_data for row in keyboard({}).inline_keyboard for button in row
        }
        await click("logs:menu", user_id=99)
        assert not edited and not sent and alerts[-1].show_alert

        await click("logs:menu")
        assert edited[-1].text == "Выгрузка логов и переписок"
        buttons = {
            button.text: button.callback_data
            for row in export_keyboard(log_dir).inline_keyboard for button in row
        }
        assert {"Работа бота", "LLM и RAG", "Parser 42", "Все чаты", "Выгрузить все логи"} <= buttons.keys()

        await click(buttons["Работа бота"])
        assert sent[-1][0] == "bot.txt" and sent[-1][2] == b"bot log"
        assert "Логи: Работа бота" in sent[-1][1]
        assert "актуальны на момент" in sent[-1][1] and "размер 7 Б" in sent[-1][1]

        await click("logs:chats")
        assert sent[-1][0].startswith("all-chats-")
        assert json.loads(sent[-1][2])["users"][0]["messages"][0]["text"] == "hello"

        before = len(sent)
        await click("logs:all")
        assert len(sent) == before + 1
        assert sent[-1][0].endswith(".zip")
        from io import BytesIO

        with ZipFile(BytesIO(sent[-1][2])) as bundle:
            assert set(bundle.namelist()) == {
                "logs/bot.txt", "logs/rag.txt", "logs/parser-42.txt", "all-chats.json"
            }
            assert json.loads(bundle.read("all-chats.json"))["users"][0]["user"]["id"] == 7
    finally:
        await bot.session.close()
