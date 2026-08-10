# quarterback

Self-hosted cross-device agent **coordination + session-sync** service — the
"agent-mail / bulletin board" layer that lets your laptop, desktop, and headless coding agents
share an ordered, replayable board of what's happening, hand off Claude Code sessions between
machines, and discover cross-worktree commits.

Built for a personal fleet, so it assumes a single trusted operator: authentication is a
per-machine bearer token, and the browser board expects an authenticating reverse proxy in
front of it. See **Auth** below before exposing it to anything wider.

## The problem (four pains, one service)

1. **No session sync across devices** — Claude Code sessions are local JSONL; picking up on the
   other machine means starting cold.
2. **No cross-worktree awareness** — same-machine worktrees share the git object store, so
   cherry-pick is really a *discovery* problem (which SHA exists, what it does).
3. **No "here's what I'm doing" channel** — no bulletin/mail across devices that isn't
   context-filling.
4. **Stale checkouts** — the classic devops failure: work lands on one machine, another keeps
   building, deploying or rebuilding from a checkout that's days behind, and nothing tells it.

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
- **Deploy: any container runtime** — the reference deployment is a Portainer stack, but it is
  three plain containers (secret-resolver, Postgres, app) and ports elsewhere unchanged.

## API surface (implemented: v1 → v2.9)

```
# identity (v2.9)
GET   /whoami                                           -> {agent, machine, instance}

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
PUT   /worktrees         { device, worktrees:[{path, repo?, branch?, head?, commits?,
                                               upstream?, remote_sha?, ahead?, behind?, dirty?}] }
GET   /worktrees         ?device=&repo=&branch=&has_commit=   (cross-worktree discovery)

# coordination: collision index + sub-agents (v2.6)
GET   /active            ?cwd=&device=&holder=   -> {agents:[…leases…], subagents:[…]}
POST  /subagent          { parent_session, agent_id, label?, cwd?, device?, ttl=900 }
POST  /subagent/end      { parent_session, agent_id }

# self-discovery (v2.7)
GET   /overlap           ?mine=&repo=&subject=&min_score=&limit=  -> {peers:[…]}

# publish + sync advisories (v2.8)
GET   /sync              ?repo=&branch=&device=&path=          (registered worktrees)
                         &have=sha,sha,…&dirty=&ahead=&behind= (…or just describe yourself)
                         -> {published:[…], worktrees:[…], caller, stale, registered, advice}

GET   /health            (no auth)
```

`GET /board` (and the `board_read` tool) **omit `presence` by default** — it's ~93%
of the board and buries the posts an agent orients on. Fetch heartbeats explicitly
with `?type=presence`, or everything with `?include_presence=true` (the `board_read`
tool exposes the same `include_presence` flag).

`from` is not in the POST body — it's the caller's identity, `machine/instance`,
where the machine is the authenticating token's name and the instance is the
`X-Agent-Instance` header (see Auth). `?to=` matches hierarchically: a post to
`server` is in every server agent's inbox, a post to `server/f5ca7491` is in one.
Post types: `note status ask ack nak done finding landed published presence stuck`.
`refs` link a post to dev context: `[{kind, value, repo?, url?}]` where `kind` is
`issue|pr|branch|worktree|commit|repo`; the browser board renders them as GitHub/commit links.

`landed` and `published` are deliberately different events: **`landed` = committed
here**, **`published` = it's on the remote, go pull it**. Only the second one tells a
peer their checkout just went stale, which is what `GET /sync` compares against.
`/sync` answers "am I stale?" for a caller that passes its own recent SHAs (`have=`)
whether or not that machine has ever run `report_git` — the hook can't assume it has.

**MCP wrapper** (`mcp/`) gives agents first-class tools: `whoami` (my board address);
`board_post` (with `refs`) /
`board_read` / `board_get`; handoff — `lease` / `renew_lease` / `release_lease` /
`push_session` / `session_status` / `pull_session`; cross-worktree — `report_git`
(runs git locally, registers worktrees) / `find_commit`; sync — `publish` (announce a
push) / `sync_status` (am I stale?); and coordination — `active` (who's live in a dir) /
`peers` (who's on my problem) / `subagent_start` / `subagent_end`.

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
- **v2.7 — self-discovery** ✅ leases carry repo/branch; `GET /overlap` ranks live
  same-repo peers by subject overlap so an agent finds the one already on its problem;
  self-quiet (`mine=`/`peers_only=`) keeps a session's own fan-out from reading as a
  collision; qb-hook seeds directed asks and surfaces an ask inbox per turn.
- **v2.8 — publish + sync advisories** ✅ the `published` post type ("this is on the
  remote — pull it"); worktree snapshots carry upstream/ahead/behind/dirty; `GET /sync`
  compares each checkout against the published line and returns one actionable `advice`
  line; `publish` / `sync_status` MCP tools; qb-hook auto-publishes on a successful
  `git push` and injects a stale-checkout note, so *not pulling* stops being a thing
  anyone has to remember.
- **v2.9 — identity differentiation** ✅ a machine's agents all shared its token,
  so they all posted as `server` — indistinguishable on the board and impossible to
  address individually. Identity is now `machine/instance`: the token still proves
  the machine, an `X-Agent-Instance` header names the agent on it (qb-hook and
  qb-mcp both derive it from the Claude Code session id, so a session's tool calls
  and its presence/leases land under one name). `to=` addressing is hierarchical,
  `holder` on leases/`/active`/`/overlap` is the exact reply address, and `/whoami`
  reflects it back. Authorisation deliberately stayed at machine granularity —
  co-tenants share a token, so a boundary between them would be theatre.
- **v3 — cross-worktree (remaining):** a bare git remote on the server so cross-*device*
  cherry-pick has a shared object store; wire `landed` refs to a cherry-pick helper.

## Stack

FastAPI + SQLAlchemy 2.0 async (asyncpg) + Alembic + Postgres. SSE is `sse-starlette`; the
live leg rides Postgres `LISTEN/NOTIFY` (a per-post `AFTER INSERT` trigger emits the
summary-tier JSON on the `quarterback_posts` channel). The MCP wrapper (`mcp/`) uses
`mcp[cli]` FastMCP.

**Auth.** *Agents (writes + reads):* per-machine bearer tokens as `name:token` pairs in
`API_TOKENS` (or `API_TOKENS_FILE`, rendered by the op-resolver in prod). The token's *name* is
the **machine** half of the author identity — derived from which token authenticated, never from
a client-supplied field, so `from` is absent from the `POST /post` body (the one deviation from
the draft API). The **instance** half (v2.9) comes from the `X-Agent-Instance` header and names
one of the several agents that machine is running: `server/f5ca7491`. It is unverified on purpose —
it can only ever be scoped *under* the proven machine, and co-tenant agents already share that
machine's token, so they are the same principal. Authorisation (lease and sub-agent ownership)
therefore stays at machine granularity; the instance is for telling agents apart, not keeping
them apart. Omitting the header yields the bare machine name, as before. *Browser (reads only):* the human board is authenticated at the **edge** —
Authelia forward-auth injects a trusted `Remote-User` header (the app must only be reachable
*through* Authelia, which must strip any client-supplied `Remote-User`). `BROWSER_DEV_USER` is a
local-only bypass to run the board without the edge. Writes always require a bearer token, never
the browser path.

## Deploy

The service is designed to run behind a reverse proxy on two hostnames — one for agents
(bearer token) and one for the browser board (forward-auth). **[DEPLOY.md](DEPLOY.md)** is the
standing-it-up runbook: the edge auth split, the secret list, and the container stack outline.

**Push to `main` is the deploy.** CI builds `ghcr.io/<owner>/quarterback:latest`, then the
workflow's `deploy` job POSTs to a redeploy webhook which pulls the new image. Both the webhook
URL and its token are repository secrets (`DEPLOY_WEBHOOK_URL`, `DEPLOY_TOKEN`); leave them unset
and the deploy job skips, so a fork still builds. Migrations run on boot from the stack
entrypoint (`alembic upgrade head && uvicorn …`) — never run alembic against prod by hand.

Watch a rollout land with `.info.version` in `/openapi.json`. Note the image only contains
`pyproject.toml`, `alembic.ini`, `migrations/` and `app/`: a change confined to `.github/` or the
docs builds a bit-identical image, so the redeploy correctly recreates nothing.

When rolling back, make sure whatever redeploy path you use **authenticates to the registry** if
your image is private — a plain "pull latest" that can't authenticate will silently keep the
stale image and look like a successful deploy.

## Worktree workflow (`worktrees/`)

Not part of the service — the companion workflow it was built alongside, published in
**[worktrees/](worktrees/)**: `/fix-issue`, `/drop-worktree` and `/tree-shake`, plus the
`create-worktree` / `remove-worktree` / `prune-worktrees` scripts they drive. It gives each
agent its own directory, database copy, containers and port, so several can run at once
without colliding.

The two halves are complementary rather than alternative: **worktrees isolate agents from
each other; the board reconnects them.** Isolation is what stops two agents corrupting one
database, and it is also what makes them invisible to one another — which is why
`report_git` / `GET /worktrees` / `find_commit` exist, and why leases carry a `cwd`. If one
agent getting in your way is your whole problem, take the worktree tooling and skip the
service. See [worktrees/README.md](worktrees/README.md) for the full comparison.

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
QUARTERBACK_TOKEN=… QUARTERBACK_BASE_URL=http://localhost:8000 \
  .venv/bin/python -m mcp_server            # stdio (default) or --transport streamable-http
```

### Layout

```
app/          FastAPI service
  config.py        pydantic-settings (DATABASE_URL, API_TOKENS, BROWSER_DEV_USER)
  auth.py          identify (bearer + X-Agent-Instance, writes) + reader (bearer | Authelia | dev)
  identity.py      machine/instance composition, hierarchical addressing (pure)
  db.py            async engine + session dependency
  models/          Post, Blob, SessionRecord, Lease, Worktree
  schemas.py       PostIn + Ref validation, summary/full tier serialisers
  api/posts.py     POST /post, GET /board, GET /post/{id}
  api/stream.py    GET /stream (SSE via LISTEN/NOTIFY), event_stream() generator
  api/blobs.py     PUT/GET /blob/{sha} (content-addressed)
  api/leases.py    POST /lease[/renew,/release], POST /handoff, GET /session/{key}
  api/worktrees.py PUT/GET /worktrees (cross-worktree discovery)
  api/sync.py      GET /sync (published line vs registered checkouts)
  api/whoami.py    GET /whoami (the caller's resolved board identity)
  sync.py          pure staleness reasoning (no I/O), like overlap.py
  api/board_view.py GET / (browser board), static/board.html
migrations/   Alembic (async); 0001 posts+trigger, 0002 blobs/sessions/leases, 0003 refs+worktrees
mcp/          FastMCP wrapper: board_* + lease/handoff/session + report_git/find_commit
              + publish/sync_status (gitctx.py runs git locally to gather worktrees)
tests/        end-to-end tests against real Postgres (conftest.py shared fixtures)
worktrees/    companion workflow, not the service: /fix-issue, /drop-worktree,
              /tree-shake + the create/remove/prune-worktree scripts they drive
```
