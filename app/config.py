from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import unquote
from uuid import UUID

from arq.connections import RedisSettings
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, TypeAdapter, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

Locale = Literal["hi-IN", "ta-IN", "bn-IN", "en-IN"]
Digits = Annotated[str, StringConstraints(pattern=r"^[0-9]{5,30}$")]
ShopDomain = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")]
HIGH_QUEUE = "high_priority_chat"
BULK_QUEUE = "bulk_cod_verification"
DAY = 86400


class Store(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    store_id: UUID
    shop_domain: ShopDomain
    shopify_token: SecretStr
    whatsapp_token: SecretStr
    bulk_api_token: SecretStr
    locale: Locale = "hi-IN"
    cod_template: str | None = None
    cod_template_language: str = "hi"
    cod_gateways: tuple[str, ...] = ("Cash on Delivery (COD)",)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    whatsapp_app_secret: SecretStr = SecretStr("")
    whatsapp_verify_token: SecretStr = SecretStr("")
    whatsapp_graph_version: str = Field(default="v24.0", pattern=r"^v[0-9]+\.0$")
    shopify_api_version: str = Field(default="2026-07", pattern=r"^20[0-9]{2}-(01|04|07|10)$")
    stores_json: SecretStr = SecretStr("{}")
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = "gemini-2.5-flash"
    gemini_embedding_model: str = "gemini-embedding-001"
    sarvam_api_key: SecretStr = SecretStr("")
    sarvam_asr_model: str = "saaras:v4"
    sarvam_tts_model: str = "bulbul:v4-flash"
    bhashini_compute_url: str = ""
    bhashini_auth_header: str = "Authorization"
    bhashini_api_key: SecretStr = SecretStr("")
    bhashini_asr_services: dict[str, str] = Field(default_factory=dict)
    bhashini_tts_services: dict[str, str] = Field(default_factory=dict)
    asr_provider: Literal["sarvam", "bhashini"] = "sarvam"
    tts_provider: Literal["sarvam", "bhashini"] = "sarvam"
    asr_min_confidence: float = Field(default=0.8, ge=0, le=1)
    faq_min_similarity: float = Field(default=0.88, ge=0, le=1)
    supabase_postgres_dsn: SecretStr = SecretStr("")
    valkey_url: SecretStr = SecretStr("redis://localhost:6379/0")
    otel_exporter_otlp_endpoint: str = ""

    def redis_settings(self) -> RedisSettings:
        redis = RedisSettings.from_dsn(self.valkey_url.get_secret_value())
        redis.ssl_check_hostname = redis.ssl
        if redis.username is not None:
            redis.username = unquote(redis.username)
        if redis.password is not None:
            redis.password = unquote(redis.password)
        return redis


@lru_cache
def settings() -> Settings:
    return Settings()


@lru_cache
def stores() -> dict[str, Store]:
    raw = settings().stores_json.get_secret_value()
    try:
        return TypeAdapter(dict[Digits, Store]).validate_json(raw)
    except ValidationError:
        raise RuntimeError("STORES_JSON must match the store configuration in .env.example") from None
