from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database — SQLAlchemy asyncpg URL
    database_url: str = "postgresql+asyncpg://quarterback:quarterback@localhost:5432/quarterback"

    # Per-agent bearer tokens as "name:token" pairs, comma-separated.
    # The name of the token that authenticates a request becomes the post author.
    api_tokens: str = ""
    # Prod: a file (rendered by the op-resolver) holding the same name:token format.
    api_tokens_file: str = ""

    # Browser board: a local-only bypass so the read views work without the
    # Authelia edge. Set to a display name (e.g. "devuser"); leave empty in prod,
    # where Authelia's Remote-User header authenticates the browser instead.
    browser_dev_user: str = ""

    # Optional persistent app log; unset means stdout only.
    log_file: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}

    @property
    def token_map(self) -> dict[str, str]:
        """name -> token, parsed from api_tokens_file (if set) else api_tokens."""
        raw = self.api_tokens
        if self.api_tokens_file:
            raw = Path(self.api_tokens_file).read_text()
        out: dict[str, str] = {}
        for pair in raw.replace("\n", ",").split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            name, _, token = pair.partition(":")
            name, token = name.strip(), token.strip()
            if name and token:
                out[name] = token
        return out

    @property
    def asyncpg_dsn(self) -> str:
        """Plain libpq DSN for a raw asyncpg LISTEN/NOTIFY connection."""
        return self.database_url.replace("+asyncpg", "")


settings = Settings()
