from pydantic_settings import BaseSettings


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
    local_vector_dir: str | None = None      # dev only: load index from disk, skip S3

    #db settings
    pg_password: str

    class Config:
        env_file = ".env"


settings = Settings()