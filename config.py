from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # All values from environment (Heroku config vars). No defaults for secrets.
    database_url: str
    webhook_secret: str                   # shared secret validated on inbound webhooks

    aws_access_key_id: str
    aws_secret_access_key: str
    aws_region: str = "us-east-1"
    s3_bucket: str
    s3_index_key: str = "index.faiss"
    s3_chunks_key: str = "chunks.pkl"

    evolution_api_url: str                # e.g. https://aisich-evolution-app.herokuapp.com
    evolution_api_key: str
    evolution_instance: str

    llm_timeout_s: float = 60.0
    rag_top_k: int = 3

    # config.py — add:
    # local_vector_dir: str | None = None      # dev only: load index from disk, skip S3

    #db settings
    # pg_password: str

    # --- Behavior gating ---
    bot_number: str                        # bot's own number, digits only, e.g. "919999999999"
    bot_lid: str | None = None             # bot's LID number if clients mention via LID (populate after live test)
    allowed_group_jid: str | None = None   # the ONE group the bot serves; None = deaf in all groups
    dm_conversation_enabled: bool = False  # False = DMs are commands-only
    dm_reply_cooldown_s: int = 300         # non-command DM redirect cooldown
    unreg_cooldown_s: int = 300            # per-sender unregistered-reply cooldown (replaces UNREG_COOLDOWN_S)

    # class Config:
    #     env_file = ".env"
    # model_config = SettingsConfigDict(env_file=".env")
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()