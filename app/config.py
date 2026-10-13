from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: SecretStr = SecretStr("")
    service_bot_token: SecretStr = SecretStr("")
    service_admin_ids: tuple[int, ...] = ()
    conversation_dir: Path = Path("data/history")
    polza_api_key: SecretStr = SecretStr("")
    rag_api_token: SecretStr = SecretStr("")
    polza_base_url: str = "https://api.polza.ai/v1"
    generator_model: str = "qwen/qwen3.5-9b"
    embed_model: str = "intfloat/multilingual-e5-base"
    embed_device: str = "cpu"
    embed_batch_size: int = Field(default=16, ge=1)
    embed_quantize: bool = True
    embed_threads: int = Field(default=2, ge=1, le=32)
    index_parse_workers: int = Field(default=2, ge=1, le=16)
    index_workers: int = Field(default=3, ge=1, le=8)
    index_start_id: str = ""
    index_control_dir: Path = Path("data/host")
    index_estimated_chunks_per_second: float = Field(default=0.5, gt=0)
    index_publish_percent: int = Field(default=5, ge=1, le=100)
    index_reload_seconds: float = Field(default=3, ge=0.1)
    monitor_dir: Path = Path("data/monitor")
    log_dir: Path = Path("data/logs")
    log_level: str = "INFO"
    log_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    log_backup_count: int = Field(default=5, ge=1)
    service_report_seconds: int = Field(default=60, ge=10)
    timezone: str = "Europe/Moscow"
    kb_zip: Path = Path("data/raw/MIEM_knowledge_base_unified.zip")
    faq_path: Path = Path("data/raw/HSE_Answers_RU.xlsx")
    index_dir: Path = Path("data/index")
    rag_url: str = "http://rag:8000"
    redis_url: str = "redis://redis:6379/0"
    history_ttl: int = Field(default=86400, ge=60)
    request_timeout: float = Field(default=360, ge=30)
    rag_concurrency: int = Field(default=2, ge=1, le=16)
    link_threshold: float = Field(default=0.75, ge=0, le=1)
    faq_cosine_threshold: float = Field(default=0.8, ge=0, le=1)
    faq_rrf_threshold: float = Field(default=0.032, ge=0)

    def require(self, *names: str) -> None:
        for name in names:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                value = value.get_secret_value()
            if not value or value.startswith("<"):
                raise ValueError(f"Set {name.upper()} in .env")
