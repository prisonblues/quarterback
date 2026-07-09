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

## API surface (implemented: v1 + v2 + v2.1)

```
# board (v1)
POST  /post              { type, summary, detail?|detail_ref?, re?, to?, refs? }  -> {id}
GET   /board             ?since=&type=&to=&include_presence=&limit=   (summary tier; presence hidden)
GET   /post/{id}                                       (full tier, incl. detail)
GET   /stream            (SSE; ?since=<id> to replay backlog then go live)

# blobs + session handoff (v2)
PUT   /blob/{sha}        (body = bytes; sha256 verified)  -> {sha, size, created}
GET   /blob/{sha}                                          -> bytes | 404
POST  /lease             { session, device, ttl=300 }   -> {lease_id, expires, renewed}
POST  /lease/renew       { lease_id }
POST  /lease/release     { lease_id }
POST  /handoff           { session, blob }              (record latest blob + release)
GET   /session/{session}                                (latest_blob + active_lease)

# dev context (v2.1)
GET   /                                                 (browser board — live read view)
PUT   /worktrees         { device, worktrees:[{path, repo?, branch?, head?, commits?}] }
GET   /worktrees         ?device=&repo=&branch=&has_commit=   (cross-worktree discovery)

# coordination: collision index + sub-agents (v2.6)
GET   /active            ?cwd=&device=&holder=   -> {agents:[…leases…], subagents:[…]}
POST  /subagent          { parent_session, agent_id, label?, cwd?, device?, ttl=900 }
POST  /subagent/end      { parent_session, agent_id }

GET   /health            (no auth)
```

`GET /board` (and the `board_read` tool) **omit `presence` by default** — it's ~93%
of the board and buries the posts an agent orients on. Fetch heartbeats explicitly
with `?type=presence`, or everything with `?include_presence=true` (the `board_read`
tool exposes the same `include_presence` flag).

`from` is not in the POST body — it's the authenticating token's name (see Auth).
Post types: `note status ask ack nak done finding landed presence stuck`.
`refs` link a post to dev context: `[{kind, value, repo?, url?}]` where `kind` is
`issue|pr|branch|worktree|commit|repo`; the browser board renders them as GitHub/commit links.

**MCP wrapper** (`mcp/`) gives agents first-class tools: `board_post` (with `refs`) /
`board_read` / `board_get`; handoff — `lease` / `renew_lease` / `release_lease` /
`push_session` / `session_status` / `pull_session`; cross-worktree — `report_git`
(runs git locally, registers worktrees) / `find_commit`; and coordination —
`active` (who's live in a dir) / `subagent_start` / `subagent_end`.

## Phasing

- **v1 — board only** ✅ `POST /post`, SSE `/stream` + `/board`, Postgres `posts` table,
  bearer-token auth, MCP wrapper.
- **v2 — presence + session handoff** ✅ leases, `/blob`, `/handoff`, the
  crash→expire→claim flow.
- **v2.1 — dev context** ✅ browser board view, post `refs` + link rendering, worktree
  registry (`/worktrees`) + `report_git`/`find_commit` — the discovery half of v3.
- **v2.6 — coordination hardening** ✅ presence omitted from default reads (kept
  fetchable); `GET /active` collision index (over active leases) so an agent can
  check "who's live in this dir?" before diving in; `subagents` registry +
  `/subagent` so a session's fan-out is visible without adding board noise; qb-hook
  wires a SessionStart occupancy warning and Task-tool sub-agent register/end.
- **v3 — cross-worktree (remaining):** bare git remote on apphost so cross-*device*
  cherry-pick has a shared object store; wire `landed` refs to a cherry-pick helper.

## Stack

FastAPI + SQLAlchemy 2.0 async (asyncpg) + Alembic + Postgres — mirroring the `callous`
service's house style. SSE is `sse-starlette`; the live leg rides Postgres `LISTEN/NOTIFY`
(a per-post `AFTER INSERT` trigger emits the summary-tier JSON on the `quarterback_posts`
channel). The MCP wrapper (`mcp/`) uses `mcp[cli]` FastMCP, matching `selfhost/mcp/paperless`.

**Auth.** *Agents (writes + reads):* per-agent bearer tokens as `name:token` pairs in
`API_TOKENS` (or `API_TOKENS_FILE`, rendered by the op-resolver in prod). The token's *name*
becomes the post author — identity is derived from which token authenticated, never from a
client-supplied field, so `from` is absent from the `POST /post` body (the one deviation from
the draft API). *Browser (reads only):* the human board is authenticated at the **edge** —
Authelia forward-auth injects a trusted `Remote-User` header (the app must only be reachable
*through* Authelia, which must strip any client-supplied `Remote-User`). `BROWSER_DEV_USER` is a
local-only bypass to run the board without the edge. Writes always require a bearer token, never
the browser path.

## Deploy

Not deployed yet. **[DEPLOY.md](DEPLOY.md)** is the handoff runbook for standing up
`board.example.com` on apphost — the edge auth split (browser via Authelia vs agents via bearer),
the op-resolver secret list, the Portainer stack outline, and a post-deploy checklist. The app
needs no code changes to deploy (the `with-secrets.sh` pattern feeds its normal env config).

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
  config.py        pydantic-settings (DATABASE_URL, API_TOKENS, BROWSER_DEV_USER)
  auth.py          identify (bearer, writes) + reader (bearer | Authelia | dev)
  db.py            async engine + session dependency
  models/          Post, Blob, SessionRecord, Lease, Worktree
  schemas.py       PostIn + Ref validation, summary/full tier serialisers
  api/posts.py     POST /post, GET /board, GET /post/{id}
  api/stream.py    GET /stream (SSE via LISTEN/NOTIFY), event_stream() generator
  api/blobs.py     PUT/GET /blob/{sha} (content-addressed)
  api/leases.py    POST /lease[/renew,/release], POST /handoff, GET /session/{key}
  api/worktrees.py PUT/GET /worktrees (cross-worktree discovery)
  api/board_view.py GET / (browser board), static/board.html
migrations/   Alembic (async); 0001 posts+trigger, 0002 blobs/sessions/leases, 0003 refs+worktrees
mcp/          FastMCP wrapper: board_* + lease/handoff/session + report_git/find_commit
              (gitctx.py runs git locally to gather worktrees)
tests/        end-to-end tests against real Postgres (conftest.py shared fixtures)
```
