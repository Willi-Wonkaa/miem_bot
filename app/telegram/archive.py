"""Archive incoming messages and Telegram replies within their user's request context."""

from contextvars import ContextVar
from dataclasses import dataclass

from aiogram import BaseMiddleware
from aiogram.methods import DeleteMessage
from aiogram.types import Message


@dataclass
class Conversation:
    user_id: int
    question_id: int
    answer_id: int | None = None


conversation = ContextVar("conversation_archive", default=None)


class IncomingArchive(BaseMiddleware):
    def __init__(self, archive):
        self.archive = archive

    async def __call__(self, handler, message, data):
        if message.from_user is None:
            return await handler(message, data)
        user = message.from_user
        self.archive.user(user.id, user.full_name, user.username)
        attachment = None
        if message.content_type != "text":
            media = getattr(message, message.content_type, None)
            if isinstance(media, list):
                media = media[-1] if media else None
            attachment = {"type": message.content_type}
            if media is not None and hasattr(media, "model_dump"):
                attachment["metadata"] = media.model_dump(mode="json", exclude_none=True)
        question_id = self.archive.message(
            user.id, "user", message.text or message.caption or "", message.edit_date or message.date,
            f"incoming:{message.chat.id}:{message.message_id}" +
            (f":edited:{message.edit_date.timestamp()}" if message.edit_date else ""), attachment=attachment,
        )
        token = conversation.set(Conversation(user.id, question_id))
        try:
            return await handler(message, data)
        finally:
            conversation.reset(token)


class OutgoingArchive:
    def __init__(self, archive):
        self.archive = archive

    async def __call__(self, make_request, bot, method):
        result = await make_request(bot, method)
        scope = conversation.get()
        if isinstance(result, Message):
            user_id = scope.user_id if scope else result.chat.id if result.chat.type == "private" else None
            if user_id is not None:
                self.archive.user(user_id)
                self.archive.message(
                    user_id, "bot", result.text or result.caption or "", result.date,
                    f"outgoing:{result.chat.id}:{result.message_id}",
                    question_id=scope.question_id if scope else None,
                    answer_id=scope.answer_id if scope else None,
                )
                markup = getattr(method, "reply_markup", None)
                if scope and scope.answer_id and markup and hasattr(markup, "inline_keyboard"):
                    if any((button.callback_data or "").startswith(f"feedback:{scope.answer_id}:")
                           for row in markup.inline_keyboard for button in row):
                        self.archive.sent_feedback(scope.answer_id,
                            f"outgoing:{result.chat.id}:{result.message_id}", result.date)
        elif isinstance(method, DeleteMessage) and result:
            self.archive.deleted(f"outgoing:{method.chat_id}:{method.message_id}")
        return result
