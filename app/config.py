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

    # A DELEGATED agent's own credential, per machine, in the same `name:secret`
    # shape as `api_tokens` — and read the same two ways, so a deployment that
    # renders one from op-resolver renders both the same way (#478).
    #
    # It is NOT a way to be a person. It authorises a NAMED, narrow set of writes
    # that would otherwise be human-only, and the writes still author as the
    # agent: `human()` is untouched, `/dials`, scope declaration and `exempt`'s
    # grant path stay human-only, and a delegated reorder is recorded as
    # `derived` rather than `ordered`. Keyed per machine so a leaked secret is
    # revoked by editing one line, and so a secret minted for hermes is refused
    # when presented by zeus — see `delegated()` for that check, which is the
    # whole reason this is a map and not a single value.
    #
    # Unset means closed, exactly as `human_edge_secret` is: a deployment that
    # has not configured this refuses every delegated write rather than
    # accepting every one.
    elevated_tokens: str = ""
    # Prod: a file (rendered by the op-resolver) holding the same format.
    elevated_tokens_file: str = ""

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
    def elevated_map(self) -> dict[str, str]:
        """machine -> delegated secret, parsed exactly as :meth:`token_map` is.

        One parser, deliberately duplicated in shape rather than in code: the two
        maps mean different things (who may write at all, versus who may make the
        narrow set of writes `delegated()` names) and collapsing them into one
        table is how a machine that should only have the first quietly gains the
        second.
        """
        raw = self.elevated_tokens
        if self.elevated_tokens_file:
            # Guarded, unlike `token_map`'s, and the asymmetry is deliberate: this
            # is read from inside an auth DEPENDENCY, so an unreadable file would
            # surface as a 500 from `delegated()` — an internal error where the
            # documented behaviour is a closed, actionable refusal. Falling back to
            # the inline value keeps that promise: no secrets, so no delegated
            # write is authorised, and the caller is told which credential is
            # missing rather than that the board is broken.
            try:
                raw = Path(self.elevated_tokens_file).read_text(encoding="utf-8")
            except OSError:
                raw = self.elevated_tokens
        out: dict[str, str] = {}
        for pair in raw.replace("\n", ",").split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            name, _, secret = pair.partition(":")
            name, secret = name.strip(), secret.strip()
            if name and secret:
                out[name] = secret
        return out

    @property
    def asyncpg_dsn(self) -> str:
        """Plain libpq DSN for a raw asyncpg LISTEN/NOTIFY connection."""
        return self.database_url.replace("+asyncpg", "")


settings = Settings()
