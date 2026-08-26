from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database — SQLAlchemy asyncpg URL. The port is compose's *published* one
    # (5435), matching .env.example and the test suite's fallback: a checkout
    # with no .env should land on this project's Postgres or fail, not connect
    # to whatever unrelated server happens to own the standard 5432.
    database_url: str = "postgresql+asyncpg://quarterback:quarterback@localhost:5435/quarterback"

    # Per-agent bearer tokens as "name:token" pairs, comma-separated.
    # The name of the token that authenticates a request becomes the post author.
    api_tokens: str = ""
    # Prod: a file (rendered by the op-resolver) holding the same name:token format.
    api_tokens_file: str = ""

    # Browser board: a local-only bypass so the READ views work without the
    # Authelia edge. Set to a display name (e.g. "devuser"); leave empty in prod,
    # where Authelia's Remote-User header authenticates the browser instead.
    # It grants reads only — see `human_edge_secret` for the human-only writes.
    browser_dev_user: str = ""

    # What makes an edge identity trustworthy (v2.39). `Remote-User` is a plain
    # request header: the app cannot tell one injected by the auth proxy from one
    # typed by a caller, and the human-only plan endpoints are an authorisation
    # decision rather than a read. So the edge must also inject this shared
    # secret as `X-Edge-Auth`, and a request without it is not a person.
    # Unset (the default) means *nobody* is a human: fail closed, because the
    # alternative failure is every agent on the box being able to reorder the
    # plan by sending one extra header.
    human_edge_secret: str = ""

    # A PERSON's key, and the counterpart of `api_tokens` for `human()` rather
    # than for `identify()`. `name:secret` pairs in the same format, where the
    # name is the person the write is recorded as — `rich:<secret>` authors as
    # `human/rich`, exactly as the edge does.
    #
    # It exists so a human write does not have to come through Authelia. The edge
    # path is a session on a wall clock, so anything depending on it needs
    # re-minting by hand whenever it lapses; this is a static secret that rotates
    # when somebody decides to rotate it and never otherwise. The dashboard's
    # DIALS panel is the caller it was added for.
    #
    # THE RESIDUAL IS KNOWN AND ACCEPTED (#479): the key sits on a workstation
    # readable by the processes running there, so an agent that goes looking can
    # find it and author as a person. That is a smaller hole than the design it
    # replaced — a browser session, which is SSO for a whole estate — and it is
    # bounded by being per person and revocable in one line. Narrowing it further
    # is deferred deliberately rather than overlooked.
    human_tokens: str = ""
    # Prod: a file (rendered by the op-resolver) holding the same format.
    human_tokens_file: str = ""

    # LOCAL DEV ONLY: treat `browser_dev_user` (or any `Remote-User`) as a human
    # for the human-only writes, with no shared secret. Off by default and must
    # stay off anywhere the app is reachable — see DEPLOY.md.
    browser_dev_human: bool = False

    # Optional persistent app log; unset means stdout only.
    log_file: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}

    @property
    def token_map(self) -> dict[str, str]:
        """name -> token, parsed from api_tokens_file (if set) else api_tokens."""
        raw = self.api_tokens
        if self.api_tokens_file:
            raw = Path(self.api_tokens_file).read_text(encoding="utf-8")
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
    def human_map(self) -> dict[str, str]:
        """person -> key, parsed exactly as :meth:`token_map` is.

        A second map rather than a flag on the first, and the shape is duplicated
        rather than shared: the two mean different things — who may write at all,
        versus who counts as a PERSON — and collapsing them is how a machine that
        should only have the first quietly gains the second.

        Guarded, unlike `token_map`'s read: this one is consulted from inside an
        auth dependency, so an unreadable file would surface as a 500 from
        `human()` — an internal error where the honest answer is "this deployment
        has no human keys", which fails closed.
        """
        raw = self.human_tokens
        if self.human_tokens_file:
            try:
                raw = Path(self.human_tokens_file).read_text(encoding="utf-8")
            except OSError:
                return {}
        out: dict[str, str] = {}
        for pair in raw.replace("\n", ",").split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            name, _, key = pair.partition(":")
            name, key = name.strip(), key.strip()
            if name and key:
                out[name] = key
        return out

    @property
    def asyncpg_dsn(self) -> str:
        """Plain libpq DSN for a raw asyncpg LISTEN/NOTIFY connection."""
        return self.database_url.replace("+asyncpg", "")


settings = Settings()
