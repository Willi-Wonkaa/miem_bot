"""Administrator-only downloads of application logs and complete conversations."""

import asyncio
import hashlib
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
from zoneinfo import ZoneInfo

from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

log = logging.getLogger(__name__)

LOG_NAMES = {
    "bot": "Работа бота",
    "service": "Служебный бот",
    "rag": "LLM и RAG",
    "indexer": "Индексация RAG",
}


def log_files(directory: Path):
    if not directory.exists():
        return []
    return sorted(
        (path for path in directory.glob("*.txt") if path.is_file() and not path.is_symlink()),
        key=lambda path: path.name.casefold(),
    )


def log_label(path: Path):
    return LOG_NAMES.get(path.stem, path.stem.replace("-", " ").replace("_", " ").capitalize())


def file_key(path: Path):
    return hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:16]


def export_keyboard(directory: Path):
    rows = [
        [InlineKeyboardButton(text=log_label(path), callback_data=f"logs:file:{file_key(path)}")]
        for path in log_files(directory)
    ]
    rows.extend([
        [InlineKeyboardButton(text="Все чаты", callback_data="logs:chats")],
        [InlineKeyboardButton(text="Выгрузить все логи", callback_data="logs:all")],
        [InlineKeyboardButton(text="Назад", callback_data="status")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def readable_size(size: int):
    if size < 1024:
        return f"{size} Б"
    for unit in ("КБ", "МБ", "ГБ"):
        size /= 1024
        if size < 1024:
            return f"{size:.1f} {unit}"
    return f"{size:.1f} ТБ"


def caption(label: str, path: Path, settings):
    now = datetime.now(ZoneInfo(settings.timezone)).strftime("%d.%m.%Y %H:%M:%S %Z")
    return f"{label}, актуальны на момент {now}, размер {readable_size(path.stat().st_size)}."


def make_bundle(log_paths: list[Path], chats_path: Path):
    with tempfile.NamedTemporaryFile(prefix="logs-all-", suffix=".zip", delete=False) as stream:
        bundle = Path(stream.name)
    try:
        with ZipFile(bundle, "w", compression=ZIP_DEFLATED) as archive:
            for path in log_paths:
                archive.write(path, arcname=f"logs/{path.name}")
            archive.write(chats_path, arcname="all-chats.json")
    except BaseException:
        bundle.unlink(missing_ok=True)
        raise
    return bundle


def register_exports(dp, archive, settings):
    async def send_file(query, path, filename, label):
        await query.message.answer_document(
            FSInputFile(path, filename=filename),
            caption=caption(label, path, settings),
        )

    @dp.callback_query(F.data.startswith("logs:"))
    async def callback(query: CallbackQuery):
        if (
            not isinstance(query.message, Message)
            or query.message.chat.type != "private"
            or query.from_user.id not in settings.service_admin_ids
        ):
            await query.answer("Выгрузка доступна только администратору.", show_alert=True)
            return

        action = query.data.split(":")
        if action[1] == "menu":
            try:
                await query.message.edit_text(
                    "Выгрузка логов и переписок", reply_markup=export_keyboard(settings.log_dir)
                )
            except TelegramBadRequest as error:
                if "message is not modified" not in str(error).lower():
                    raise
            await query.answer()
            return

        if action[1] == "file" and len(action) == 3:
            path = next((p for p in log_files(settings.log_dir) if file_key(p) == action[2]), None)
            if path is None:
                await query.answer("Файл лога больше не найден. Обновите меню.", show_alert=True)
                return
            await query.answer()
            try:
                await send_file(query, path, path.name, f"Логи: {log_label(path)}")
            except FileNotFoundError:
                await query.message.answer("Файл лога больше не найден. Обновите меню.")
            return

        if action[1] not in {"chats", "all"}:
            await query.answer("Неизвестная команда.", show_alert=True)
            return

        await query.answer("Готовлю выгрузку…")
        chats_path = None
        bundle = None
        try:
            chats_path = await asyncio.to_thread(archive.export_all)
            stamp = datetime.now(ZoneInfo(settings.timezone)).strftime("%Y%m%d-%H%M%S")
            if action[1] == "chats":
                await send_file(query, chats_path, f"all-chats-{stamp}.json", "Все чаты")
            else:
                paths = log_files(settings.log_dir)
                bundle = await asyncio.to_thread(make_bundle, paths, chats_path)
                await send_file(
                    query, bundle, f"all-logs-and-chats-{stamp}.zip", "Все логи и чаты"
                )
        except Exception:
            log.exception("service_export_failed kind=%s", action[1])
            await query.message.answer("Не удалось подготовить или отправить выгрузку. Попробуйте ещё раз.")
        finally:
            if bundle:
                bundle.unlink(missing_ok=True)
            if chats_path:
                chats_path.unlink(missing_ok=True)
