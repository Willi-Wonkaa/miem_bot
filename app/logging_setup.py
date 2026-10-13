"""UTF-8 rotating text logs. Credentials are removed from messages and tracebacks."""

import logging
import re
from logging.handlers import RotatingFileHandler

from app.config import Settings


class SafeFormatter(logging.Formatter):
    def __init__(self, secrets: list[str]):
        super().__init__("%(asctime)sZ %(levelname)s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%S")
        import time

        self.converter = time.gmtime
        self.secrets = [value for value in secrets if value and not value.startswith("<")]

    def format(self, record):
        message = super().format(record)
        for value in self.secrets:
            message = message.replace(value, "[REDACTED]")
        message = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot[REDACTED]", message)
        return re.sub(
            r"(?i)(authorization[\s'\"]*[:=][\s'\"]*(?:bearer\s+)?)[^\s,'\"}]+",
            r"\1[REDACTED]",
            message,
        )


def setup_logging(component: str, settings: Settings | None = None):
    settings = settings or Settings()
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    formatter = SafeFormatter(
        [
            settings.telegram_bot_token.get_secret_value(),
            settings.service_bot_token.get_secret_value(),
            settings.polza_api_key.get_secret_value(),
            settings.rag_api_token.get_secret_value(),
        ]
    )
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "miem_handler", False):
            root.removeHandler(handler)
            handler.close()
    for handler in [
        RotatingFileHandler(
            settings.log_dir / f"{component}.txt",
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ]:
        handler.miem_handler = True
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(settings.log_level.upper())
    for name in ["httpx", "httpx2", "httpcore", "openai", "aiogram", "urllib3"]:
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger(__name__).info("logging_ready component=%s", component)
