from dataclasses import dataclass

import asyncpg

from config import settings

_pool: asyncpg.Pool | None = None


@dataclass(frozen=True)
class UserLLMConfig:
    api_key: str
    provider: str
    model: str

    def __repr__(self) -> str:              # defense-in-depth: key never appears
        return "UserLLMConfig(<redacted>)"  # in tracebacks, logs, or debug output


async def init_pool() -> None:
    global _pool
    # Essential-tier Postgres has a low connection cap shared across add-on
    # attachments — keep this pool tiny. One dyno, async I/O: 2 is enough.
    # _pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=2)
    _pool = await asyncpg.create_pool(
    settings.database_url, min_size=0, max_size=2,   # min_size=0: don't hold idle conns open
    max_inactive_connection_lifetime=180,             # recycle before Neon's ~300s suspend
    )


async def close_pool() -> None:
    if _pool:
        await _pool.close()


async def get_user_llm_config(phone: str) -> UserLLMConfig | None:
    """Read fresh on EVERY message — no caching, so 'update then send'
    always uses the new registration. Sub-1ms via PK index."""
    assert _pool is not None, "DB pool not initialized"
    row = await _pool.fetchrow(
        "SELECT api_key, llm_provider, llm_model FROM users "
        "WHERE phone_number = $1 AND is_active = true",
        phone,
    )
    if not row:
        return None
    return UserLLMConfig(row["api_key"], row["llm_provider"], row["llm_model"])


async def upsert_user_llm_config(phone: str, api_key: str, provider: str, model: str) -> None:
    """Registers or fully overwrites. One row per user (PK); ON CONFLICT makes
    'any change overwrites' atomic — no read-then-write race."""
    assert _pool is not None, "DB pool not initialized"
    await _pool.execute(
        """
        INSERT INTO users (phone_number, api_key, llm_provider, llm_model, is_active)
        VALUES ($1, $2, $3, $4, true)
        ON CONFLICT (phone_number) DO UPDATE SET
            api_key      = EXCLUDED.api_key,
            llm_provider = EXCLUDED.llm_provider,
            llm_model    = EXCLUDED.llm_model,
            is_active    = true
        """,
        phone, api_key, provider, model,
    )


async def delete_user_by_phone(phone: str) -> int:
    """Removes the sender's own registration. Sender identity is the credential."""
    assert _pool is not None, "DB pool not initialized"
    result = await _pool.execute("DELETE FROM users WHERE phone_number = $1", phone)
    return int(result.split()[-1])