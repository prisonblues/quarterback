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
    # NOT `elevated_tokens` widened. That one authorises an AGENT for a named,
    # narrow set of writes and deliberately excludes `/dials` (#479); this
    # authorises a PERSON, so it opens what a person opens. Two credentials, two
    # blast radii, rather than one wider one.
    #
    # THE RESIDUAL IS KNOWN AND ACCEPTED (#479): the key sits on a workstation
    # readable by the processes running there, so an agent that goes looking can
    # find it and author as a person. Bounded by being per person and revocable in
    # one line. Narrowing it further is deferred deliberately, not overlooked.
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

        A third map rather than a flag on either of the others, and the shape is
        duplicated rather than shared for `elevated_map`'s reason: the three mean
        different things — who may write at all, who may make the narrow set
        `delegated()` names, and who counts as a PERSON — and collapsing any two
        of them is how a caller quietly gains the wrong one.

        Guarded like `elevated_map` and unlike `token_map`: it is read from inside
        an auth dependency, so an unreadable file must not surface as a 500 from
        `human()`. Empty is the closed answer — no human keys, so no key
        authorises anything — which is the same door an unset `HUMAN_TOKENS`
        leaves. `UnicodeDecodeError` is caught with `OSError` for `elevated_map`'s
        reason: it is a `ValueError`, so a half-written or binary file would
        otherwise escape as the 500 the guard exists to prevent.
        """
        raw = self.human_tokens
        if self.human_tokens_file:
            try:
                raw = Path(self.human_tokens_file).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                raw = ""
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
            except (OSError, UnicodeDecodeError):
                # CLOSED, not "fall back to the inline value" — which is what this
                # did and it was the wrong door. An operator who set the FILE has
                # said where the secrets live; silently reverting to a stale
                # inline `ELEVATED_TOKENS` would authorise a set of machines
                # nobody meant to authorise, at the moment the intended source
                # broke. Empty means no delegated write is authorised and the
                # caller is told which credential is missing.
                #
                # `UnicodeDecodeError` is caught with it because it is a
                # `ValueError`, not an `OSError`: a half-written or binary file
                # would otherwise escape an auth dependency as a 500, which is
                # precisely the failure the guard exists to prevent, arriving
                # through the one exception it did not name.
                raw = ""
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
