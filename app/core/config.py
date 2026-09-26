from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = "ClusterX"
    DATABASE_URL: str = "sqlite:///./data/app.db"
    SECRET_KEY: str = "change-me-in-production"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24
    ADMIN_EMAIL: str = "admin@clusterx.local"
    ADMIN_PASSWORD: str = "ChangeMe123!"
    PORT: int = 8001
    LLM_BASE_URL: str = ""
    LLM_API_KEY: str = ""
    LLM_CHAT_MODEL: str = "gpt-4o-mini"
    LLM_EMBED_MODEL: str = "text-embedding-3-small"
    EMBEDDINGS: str = "hashed"  # "hashed" (default, zero-dep) or "st" (sentence-transformers, enterprise-brain)
    SEED_ON_STARTUP: bool = False


settings = Settings()
