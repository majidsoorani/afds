from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Database
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "afds"
    postgres_user: str = "afds_admin"
    postgres_password: str = "afds_secret"
    postgres_schema: str = "afds"

    # Kafka
    kafka_bootstrap_servers: str = "kafka:9092"
    kafka_security_protocol: str = "PLAINTEXT"

    # Security
    backend_secret_key: str = "change-me-in-production"
    backend_cors_origins: str = "http://localhost:5173,http://localhost:3000"

    # ── Advanced AFDS (GNN + DL + XAI) — Phase A/B feature flags ──
    # Mode gates the model integration in routers/realtime.py:
    #   off        → no model calls, rules only (default, safe)
    #   shadow     → call models, log scores, do not alter decisions
    #   hybrid     → model score can escalate soft-rule transactions
    #   autonomous → model-first (post-governance sign-off)
    afds_model_mode: str = "off"
    afds_model_enabled: bool = False
    afds_model_endpoint: str = ""  # e.g. http://afds-model-api:8080
    afds_model_timeout_ms: int = 40
    afds_gnn_enabled: bool = False
    afds_vae_enabled: bool = False
    # XAI: off | fastshap | symbolic (deterministic rule-factor reason codes)
    afds_xai_mode: str = "symbolic"
    afds_xai_timeout_ms: int = 10
    # Redis-backed online feature store. Empty => in-memory fallback.
    afds_feature_store_url: str = ""
    afds_drift_alert_threshold: float = 0.2

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def database_url_sync(self) -> str:
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.backend_cors_origins.split(",")]

    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
