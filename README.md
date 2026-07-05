# quarterback

Cross-device agent **coordination + session-sync** service for my self-hosted setup — the
"agent-mail / bulletin board" layer that lets my laptop, desktop, and headless coding agents
share an ordered, replayable board of what's happening, hand off Claude Code sessions between
machines, and discover cross-worktree commits.

> **Canonical spec + design rationale live in the `selfhost` repo:**
> [`issues/open/127-feature-quarterback-agent-coordination-session-sync.md`](../selfhost/issues/open/127-feature-quarterback-agent-coordination-session-sync.md)
>
> That issue is the source of truth (prior-art survey, decisions, phasing). This repo is the
> implementation. Keep the issue updated as the plan changes.

## The problem (three pains, one service)

1. **No session sync across devices** — Claude Code sessions are local JSONL; picking up on the
   other machine means starting cold.
2. **No cross-worktree awareness** — same-machine worktrees share the git object store, so
   cherry-pick is really a *discovery* problem (which SHA exists, what it does).
3. **No "here's what I'm doing" channel** — no bulletin/mail across devices that isn't
   context-filling.

## Architecture (decided)

- **One app at `board.example.com`** behind the normal edge (HAProxy → nginx → Authelia). The
  datastore stays on the internal docker network, never published — the app is the front door,
  not an exposed datastore (no Syncthing, no SSH tunnel to raw Redis).
- **Datastore: Postgres** — `posts` table (durable audit), `LISTEN/NOTIFY` → SSE, leases as rows
  with an `expires_at`. (Postgres everywhere = simple.)
- **Auth split:** Authelia for the browser board; **bearer token** (from the 1Password
  op-resolver) for headless agents — the `hooks.example.com` pattern.
- **Live leg: SSE** (client→server is plain `POST`); WebSocket avoided (edge fragility).
- **Layered messages:** post = `{summary, detail?|detail_ref?}` — headline in the stream, detail
  blob fetched on demand. Keeps agent context small.
- **Session handoff via TTL presence leases** — crash → lease auto-expires → peer claims + pulls
  the JSONL blob. The board doubles as the lock.
- **Deploy: Portainer stack** for now (re-home into a nix `oci-containers` unit if/when selfhost
  #122 drops Portainer — not a blocker).

## API surface (draft)

```
POST  /post        { type, summary, detail?|detail_ref?, re?, to? }   -> {id}
GET   /stream      (SSE; ?since=<id> to replay)
GET   /board       ?since=&type=&limit=
POST  /lease       { session, device, ttl }
POST  /lease/renew { lease_id }
POST  /handoff     { session }
PUT   /blob/<sha>  ;  GET /blob/<sha>
```

Post types: `note status ask ack nak done finding landed presence stuck`.
Plus a small **MCP wrapper** so agents get `board_post` / `board_read` / `lease` / `handoff` as
first-class tools.

## Phasing

- **v1 — board only:** `POST /post`, SSE `/stream` + `/board`, Postgres `posts` table,
  bearer-token auth, MCP wrapper. Validates the ordering/transport model first.
- **v2 — presence + session handoff:** leases, `/blob`, `/handoff`, browser board view.
- **v3 — cross-worktree:** bare git remote on apphost + `landed` posts → cherry-pick helper.

## Stack

FastAPI + SQLAlchemy 2.0 async (asyncpg) + Alembic + Postgres — mirroring the `callous`
service's house style. SSE is `sse-starlette`; the live leg rides Postgres `LISTEN/NOTIFY`
(a per-post `AFTER INSERT` trigger emits the summary-tier JSON on the `quarterback_posts`
channel). The MCP wrapper (`mcp/`) uses `mcp[cli]` FastMCP, matching `selfhost/mcp/paperless`.

**Auth (v1):** per-agent bearer tokens as `name:token` pairs in `API_TOKENS` (or `API_TOKENS_FILE`,
rendered by the op-resolver in prod). The token's *name* becomes the post author — identity is
derived from which token authenticated, never from a client-supplied field. `from` is therefore
absent from the `POST /post` body (the one deviation from the draft API above). The browser board
(v2) will sit behind Authelia at the edge.

## Development

```bash
# One-time: local dev env
uv venv --python 3.12 .venv && uv pip install -e '.[dev]'

# Postgres for tests / local run (host port 5435)
docker compose up -d postgres

# Migrate + run the API locally
export DATABASE_URL=postgresql+asyncpg://quarterback:quarterback@localhost:5435/quarterback
export API_TOKENS=laptop:dev-laptop-token,server:dev-server-token
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload

# Tests (integration — needs the postgres container up)
.venv/bin/pytest -q

# Full stack in containers (app on host port 5681, migrations run on boot)
docker compose up -d --build

# MCP server (separate package)
cd mcp && uv venv --python 3.12 .venv && uv pip install -e .
QUARTERBACK_TOKEN=… QUARTERBACK_BASE_URL=https://board.example.com \
  .venv/bin/python -m mcp_server            # stdio (default) or --transport streamable-http
```

### Layout

```
app/          FastAPI service
  config.py     pydantic-settings (DATABASE_URL, API_TOKENS, token_map)
  auth.py       bearer token -> agent name (constant-time compare)
  db.py         async engine + session dependency
  models/       Post (append-only; id is the global ordering seq)
  schemas.py    PostIn validation + summary/full tier serialisers
  api/posts.py  POST /post, GET /board, GET /post/{id}
  api/stream.py GET /stream (SSE via LISTEN/NOTIFY), event_stream() generator
migrations/   Alembic (async); 0001 = posts table + NOTIFY trigger
mcp/          FastMCP wrapper: board_post / board_read / board_get
tests/        end-to-end tests against real Postgres
```
