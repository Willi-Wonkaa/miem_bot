import html
import re
import textwrap

LINK_RE = re.compile(r"\[([^\]\n]+)\][ \t]*(?:\n[ \t]*)?\((https?://[^\s<>()]+)\)")


def replace_max_ru_links(text: str) -> str:
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]*max\.ru[^\s)]*)\)",
        r"[\1](https://www.hse.ru/)",
        text,
    )
    text = re.sub(r"https?://[^\s<>]*max\.ru[^\s<>]*", "Макс", text)
    return re.sub(r"\b[a-zA-Z0-9.-]*max\.ru\b", "Макс", text)


def format_plain(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"(?m)^([ \t]*)\*[ \t]+", r"\1• ", text)
    text = re.sub(r"\*\*([^\n]+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?m)^[ \t]*#{1,6}[ \t]+(.+)$", r"<b>\1</b>", text)
    return re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", text)


def format_telegram_answer(text: str) -> str:
    parts = []
    last = 0
    for match in LINK_RE.finditer(text):
        parts.append(format_plain(text[last : match.start()]))
        title = html.escape(match.group(1), quote=False)
        url = html.escape(match.group(2), quote=True)
        parts.append(f'<a href="{url}">{title}</a>')
        last = match.end()
    parts.append(format_plain(text[last:]))
    return "".join(parts)


def answer_parts(text: str) -> list[str]:
    # 1800 Unicode characters fit Telegram's 4096 limit even with surrogate pairs.
    # Format each part separately so a split cannot leave unclosed HTML tags.
    return textwrap.wrap(
        replace_max_ru_links(text),
        width=1800,
        replace_whitespace=False,
        drop_whitespace=False,
        break_long_words=True,
        break_on_hyphens=False,
    ) or ["Не удалось получить ответ."]
