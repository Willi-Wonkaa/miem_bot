"""Administrator-only browsing of permanent conversations and their feedback."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.telegram.formatting import answer_parts


def register_history(dp, archive, settings):
    def allowed(event):
        message = event.message if isinstance(event, CallbackQuery) else event
        return (isinstance(message, Message) and message.chat.type == "private"
                and event.from_user and event.from_user.id in settings.service_admin_ids)

    async def denied(event):
        if isinstance(event, CallbackQuery):
            await event.answer("Переписки доступны только администратору.", show_alert=True)
        else:
            await event.answer("Переписки доступны только администратору.")

    async def show(event, text, buttons):
        markup = InlineKeyboardMarkup(inline_keyboard=buttons)
        if isinstance(event, CallbackQuery):
            try:
                await event.message.edit_text(text, reply_markup=markup)
            except TelegramBadRequest as error:
                if "message is not modified" not in str(error).lower():
                    raise
            await event.answer()
        else:
            await event.answer(text, reply_markup=markup)

    def date(value):
        return datetime.fromtimestamp(value, ZoneInfo(settings.timezone)).strftime("%d.%m.%Y %H:%M:%S")

    async def users(event, offset=0, search=""):
        rows = archive.users(search, offset, 11)
        text = "Переписки пользователей\nПоиск: /users ID или /users @username"
        if not rows:
            text += "\n\nПереписки пока не найдены."
        buttons = [[InlineKeyboardButton(
            text=f"{row['name'] or row['username'] or row['id']} · {row['id']}"[:60],
            callback_data=f"archive:h:{row['id']}:0:0:253402300799",
        )] for row in rows[:10]]
        nav = []
        if offset > 0:
            nav.append(InlineKeyboardButton(text="Назад", callback_data=f"archive:u:{max(0,offset-10)}"))
        if len(rows) > 10 and not search:
            nav.append(InlineKeyboardButton(text="Далее", callback_data=f"archive:u:{offset+10}"))
        if nav:
            buttons.append(nav)
        buttons.append([InlineKeyboardButton(text="Статус", callback_data="status")])
        await show(event,text,buttons)

    async def history(event, user_id, offset=0, after=0, before=253402300799):
        rows = archive.messages(user_id,offset,4,after,before)
        profile = archive.profile(user_id)
        lines = [f"Переписка: {profile['name']} ({user_id})", "Новые сообщения сверху.", ""]
        buttons = []
        for row in rows[:3]:
            role = "Пользователь" if row["role"] == "user" else "Бот"
            lines.append(f"{date(row['occurred'])} · {role}")
            text = row["text"] or "[Вложение]"
            lines.append(text[:420] + ("…" if len(text)>420 else ""))
            if row["deleted"]:
                lines.append("Сообщение удалено из чата; сохранено в архиве.")
            if row["model_answered"] is not None:
                lines.append(f"Ответ готов: {date(row['model_answered'])}")
                lines.append(f"Модель: {row['model'] or 'готовый ответ FAQ'}")
                scores = {-1:"не помог",0:"оценка не поставлена",1:"помог"}
                lines.append("Оценка: " + scores.get(row["helpful"],"оценка не поставлена"))
                if row["rated_at"]:
                    lines.append("Оценка поставлена: " + date(row["rated_at"]))
                if row["feedback_comment"]:
                    lines.append("Комментарий: " + row["feedback_comment"][:200])
                buttons.append([InlineKeyboardButton(text="Полный ответ", callback_data=f"archive:a:{row['answer_id']}")])
            lines.append("")
        if not rows:
            lines.append("Сообщений за выбранный период нет.")
        nav = []
        if offset>0:
            nav.append(InlineKeyboardButton(text="Новее",callback_data=f"archive:h:{user_id}:{max(0,offset-3)}:{after}:{before}"))
        if len(rows)>3:
            nav.append(InlineKeyboardButton(text="Старее",callback_data=f"archive:h:{user_id}:{offset+3}:{after}:{before}"))
        if nav:
            buttons.append(nav)
        buttons.append([InlineKeyboardButton(text="Выгрузить полностью",callback_data=f"archive:e:{user_id}"),
                        InlineKeyboardButton(text="Пользователи",callback_data="archive:u:0")])
        await show(event,"\n".join(lines),buttons)

    async def export(event,user_id):
        path = archive.export(user_id)
        message = event.message if isinstance(event,CallbackQuery) else event
        try:
            await message.answer_document(FSInputFile(path,filename=f"history-{user_id}.json"),
                                          caption="Полная переписка, ответы и оценки. Время в выгрузке — UTC.")
        finally:
            path.unlink(missing_ok=True)
        if isinstance(event,CallbackQuery):
            await event.answer()

    @dp.message(Command("users"),F.chat.type=="private")
    async def users_command(message: Message):
        if not allowed(message):
            return await denied(message)
        await users(message,search=message.text.partition(" ")[2].strip())

    @dp.message(Command("history","export"),F.chat.type=="private")
    async def history_command(message: Message):
        if not allowed(message):
            return await denied(message)
        try:
            args = message.text.split()
            user_id = int(args[1])
            if args[0].split("@")[0]=="/export":
                return await export(message,user_id)
            after,before = 0,253402300799
            if len(args)>2:
                after = int(datetime.strptime(args[2],"%Y-%m-%d").replace(tzinfo=ZoneInfo(settings.timezone)).timestamp())
                if len(args)>3:
                    end = datetime.strptime(args[3],"%Y-%m-%d").replace(tzinfo=ZoneInfo(settings.timezone))
                    before = int((end+timedelta(days=1)).timestamp())
            await history(message,user_id,after=after,before=before)
        except (ValueError,IndexError):
            await message.answer("/history USER_ID [YYYY-MM-DD YYYY-MM-DD]\n/export USER_ID")

    @dp.callback_query(F.data.startswith("archive:"))
    async def callback(query: CallbackQuery):
        if not allowed(query):
            return await denied(query)
        try:
            args = query.data.split(":")
            if args[1]=="u":
                return await users(query,max(0,int(args[2])))
            if args[1]=="h":
                return await history(query,int(args[2]),max(0,int(args[3])),int(args[4]),int(args[5]))
            if args[1]=="e":
                return await export(query,int(args[2]))
            if args[1]=="a":
                row = archive.full_answer(int(args[2]))
                for part in answer_parts(f"Ответ готов: {date(row['answered'])}\n\n{row['text']}"):
                    await query.message.answer(part)
                return await query.answer()
        except (ValueError,IndexError):
            await query.answer("Переписка недоступна.",show_alert=True)
