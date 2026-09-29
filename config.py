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
    s3_chunks_key: str = "chunks.json"
    s3_prefix: str = "vectors"
    s3_pending_key: str = "engrave/processed"

    evolution_api_url: str                # e.g. https://aisich-evolution-app.herokuapp.com
    evolution_api_key: str
    evolution_instance: str

    llm_timeout_s: float = 60.0
    rag_top_k: int = 3

    # config.py — add:
    local_vector_dir: str | None = None      # dev only: load index from disk, skip S3

    #db settings
    # pg_password: str

    # --- Behavior gating ---
    bot_number: str                        # bot's own number, digits only, e.g. "919999999999"
    bot_lid: str | None = None             # bot's LID number if clients mention via LID (populate after live test)
    allowed_group_jids: set[str] = set()   # the ONE group the bot serves; None = deaf in all groups
    dm_conversation_enabled: bool = False  # False = DMs are commands-only
    dm_reply_cooldown_s: int = 300         # non-command DM redirect cooldown
    unreg_cooldown_s: int = 300            # per-sender unregistered-reply cooldown (replaces UNREG_COOLDOWN_S)

    # -------- embeddings Vars ----------------
    embedding_backend: str = "onnx"           # "onnx" | "remote"
    embedding_provider: str = "jina"          # read only when backend=remote
    embedding_api_key: str | None = None      # deliberately NOT a provider-standard name
    s3_manifest_key: str = "manifest.json"
    model_cache_dir: str | None = None
    index_backend: str = "numpy"

    #--------------- trial feature -----------------
    trial_enabled: bool = False
    trial_quota: int = 20
    llm_stub: bool = False
    agy_model: str | None = None
    agy_project: str | None = None
    agy_location: str | None = None
    s3_engrave_prefix: str = "engrave"

    #------------- admin settings ----------------------
    allow_freebies: bool = False
    admin_phones: set[str] = set()
    admin_token: str

    #--------------------- LLM providers ---------------------
    groq_url: str = "https://api.groq.com/openai/v1"
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-20b"
    gemini_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    ortr_url: str = "https://openrouter.ai/api/v1"
    ortr_api_key: str = ""
    ortr_model: str = "meta-llama/llama-3.3-70b-instruct:free"
    llm_weights: str = "groq:50,gemini:40,openrouter:10"
    ldr_provider: str | None = "gemini"

    # ---------------------------- Taxila Sync and context ---------------------------
    taxila_url: str = ""
    taxila_chrome_extension: str = ""
    elearn_url: str = ""
    exam_url: str = ""
    mettl_url: str = ""
    erp_url: str = ""
    grades_n_result_url: str = ""
    bits_contact_mail: str = ""
    bits_contact_number: list[str] = []




    # class Config:
    #     env_file = ".env"
    # model_config = SettingsConfigDict(env_file=".env")
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()