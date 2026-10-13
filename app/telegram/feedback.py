"""Single-use feedback buttons with persistent expiry and linked follow-up comments."""

import asyncio
import logging

from aiogram import F
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
)

from app.telegram.archive import Conversation, conversation

log = logging.getLogger(__name__)


def keyboard(answer_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Помог ответ", callback_data=f"feedback:{answer_id}:1"),
                InlineKeyboardButton(text="Не помог", callback_data=f"feedback:{answer_id}:-1"),
            ]
        ]
    )


async def remove_buttons(bot, archive, answer_id, transport_key):
    _, chat, message = transport_key.split(":")
    try:
        await bot.edit_message_reply_markup(
            chat_id=int(chat), message_id=int(message), reply_markup=None
        )
    except TelegramBadRequest as error:
        if not any(
            reason in str(error).lower()
            for reason in (
                "message is not modified",
                "message to edit not found",
                "message can't be edited",
            )
        ):
            raise
    archive.close_buttons(answer_id)


def register_feedback(dp, archive):
    @dp.callback_query(F.data.startswith("feedback:"))
    async def vote(query: CallbackQuery, bot):
        if query.message is None:
            return await query.answer()
        try:
            _, answer_id, score = query.data.split(":")
            key = f"outgoing:{query.message.chat.id}:{query.message.message_id}"
            archive.rate(int(answer_id), query.from_user.id, int(score), f"vote:{query.id}", key)
        except (ValueError, TypeError):
            return await query.answer(
                "Оценка уже закрыта или этот ответ принадлежит другому пользователю."
            )
        try:
            await remove_buttons(bot, archive, int(answer_id), key)
        except TelegramAPIError:
            log.warning("feedback_markup_removal_deferred answer_id=%s", answer_id)
        await query.answer("Спасибо, оценка сохранена.")

    def is_comment(message: Message):
        if message.text and message.text.startswith("/"):
            return False
        if message.from_user and message.reply_to_message:
            key = f"{message.chat.id}:{message.reply_to_message.message_id}"
            return archive.pending(key, message.from_user.id) is not None
        return False

    @dp.message(is_comment)
    async def comment(message: Message):
        key = f"{message.chat.id}:{message.reply_to_message.message_id}"
        answer_id = archive.pending(key, message.from_user.id)
        scope = conversation.get()
        if scope is None or answer_id is None:
            return
        archive.comment(
            answer_id,
            message.from_user.id,
            scope.question_id,
            message.text or message.caption or "[Вложение]",
            key,
        )
        await message.answer("Спасибо, комментарий сохранён.")


async def maintain(bot, archive):
    while True:
        for row in archive.cleanup_buttons():
            try:
                await remove_buttons(bot, archive, row["answer_id"], row["buttons_key"])
            except TelegramAPIError:
                log.warning("feedback_expiry_retry answer_id=%s", row["answer_id"])
        for row in archive.prompt_queue():
            _, chat, question = row["question_key"].split(":")
            token = conversation.set(Conversation(row["user_id"], row["question_id"]))
            try:
                prompt = await bot.send_message(
                    int(chat),
                    "Что не так?",
                    reply_markup=ForceReply(
                        selective=True, input_field_placeholder="Опишите, что не так"
                    ),
                    reply_parameters=ReplyParameters(
                        message_id=int(question), allow_sending_without_reply=True
                    ),
                )
                archive.prompt(
                    f"{prompt.chat.id}:{prompt.message_id}", row["user_id"], row["answer_id"]
                )
            except TelegramAPIError:
                log.warning("feedback_prompt_retry answer_id=%s", row["answer_id"])
            finally:
                conversation.reset(token)
        await asyncio.sleep(1)
