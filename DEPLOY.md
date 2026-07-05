# Deploying quarterback

Handoff runbook for standing up `board.example.com` on **apphost**. The app is built and
verified locally (see README); this covers the edge + secrets + stack wiring only. Nothing
here has been executed yet.

Canonical spec: `selfhost/issues/open/127-…`. Patterns to mirror:
`selfhost/stacks/miniflux.yml` (op-resolver), `selfhost/secrets/README.md`,
`selfhost/nginx/sites-available/hooks.example.com` (bearer path), an existing Authelia-protected
nginx site (browser path).

---

## 0. The one decision to get right: auth split at the edge

The app has **two auth paths** (see `app/auth.py`):

- **Writes** (`POST /post`, `/lease*`, `/handoff`, `PUT /blob`, `PUT /worktrees`) → `identify`:
  **bearer token only**.
- **Reads** (`GET /`, `/board`, `/stream`, `/post/{id}`, `/blob`, `/session`, `GET /worktrees`)
  → `reader`: **bearer token OR** a trusted **`Remote-User`** header (Authelia) OR `BROWSER_DEV_USER`.

The friction: the **read** endpoints are used by *both* the browser board (which can only
authenticate via Authelia — `EventSource` can't send a bearer header) **and** headless agents
(which use a bearer and can't do interactive 2FA). Authelia can't conditionally bypass on the
presence of a bearer. **Resolve it with two hostnames pointing at the same app container:**

| Host | Authelia? | Who | How the app authenticates it |
|---|---|---|---|
| `board.example.com` | **two_factor** | humans (browser board) | Authelia injects `Remote-User` → `reader` accepts |
| `qb.example.com` (agent API) | **bypass** | laptop / desktop / server agents | bearer token → `identify` / `reader` accept |

- The browser board is **read-only** — writes need a bearer, which the browser host never has. Fine.
- Agents point `QUARTERBACK_BASE_URL` at **`qb.example.com`** and send `Authorization: Bearer …`.
- No app change is needed for this split; both hosts proxy to the same upstream.

> **Security — strip `Remote-User` on BOTH vhosts.** The app trusts `Remote-User` unconditionally.
> Only Authelia (on `board.example.com`) may set it; nginx must **strip any client-supplied**
> `Remote-User`/`Remote-Groups`/`Remote-Name`/`Remote-Email` on *both* hosts — otherwise an agent
> (or anyone) could spoof a browser identity on the read endpoints by sending the header. This is
> the standard Authelia nginx hygiene, applied to the agent host too.
>
> Optional hardening: gate the `Remote-User` branch in `reader` behind a `TRUST_REMOTE_USER`
> config flag set only on the browser deployment. Not required if nginx strips correctly.

---

## 1. Secrets (1Password op-resolver — #086)

`with-secrets.sh` (pattern A) exports each `/run/secrets/<NAME>` file as an uppercase env var,
so the app's normal env config (`API_TOKENS`, `DATABASE_URL`) works directly — **no `*_FILE`
code path needed**. Provision in 1Password (vault `apphost`, item `quarterback`):

| op ref | Consumed as | Contents |
|---|---|---|
| `op://apphost/quarterback/api_tokens` | `API_TOKENS` (app) | `laptop:<tok>,desktop:<tok>,server:<tok>` — one pair per agent |
| `op://apphost/quarterback/database_url` | `DATABASE_URL` (app) | `postgresql+asyncpg://quarterback:<pw>@db:5432/quarterback` |
| `op://apphost/quarterback/db_password` | `POSTGRES_PASSWORD_FILE` (db) | must equal the password inside `database_url` |

Generate the per-agent tokens (e.g. `openssl rand -hex 32`) and store the `name:token` list in
`api_tokens`. Each agent gets its own token via its own op reference on its own machine.

- Host needs `OP_SERVICE_ACCOUNT_TOKEN` as a Portainer stack env var.
- **`BROWSER_DEV_USER` MUST be unset in prod** (it's a local-only edge bypass).

---

## 2. Portainer stack (`stacks/quarterback.yml`)

Model on `selfhost/stacks/miniflux.yml`. Three services:

1. **`op-resolver`** (`1password/op:2`, `restart: "no"`) — copy the `x-op-resolver` anchor
   verbatim; set `OP_REF_API_TOKENS`, `OP_REF_DATABASE_URL`, `OP_REF_POSTGRES_PASSWORD`;
   volume `/run/op-secrets/quarterback:/run/op-secrets`.
2. **`db`** (`postgres:15-alpine`) — `POSTGRES_USER=quarterback`, `POSTGRES_DB=quarterback`,
   `POSTGRES_PASSWORD_FILE=/run/secrets/POSTGRES_PASSWORD`; named volume for data; `pg_isready`
   healthcheck; `depends_on: op-resolver (service_completed_successfully)`; **not published**
   (internal docker net only). Mount `/run/op-secrets/quarterback:/run/secrets:ro`.
3. **`quarterback`** (build from this repo, or a pushed image) —
   - `depends_on`: op-resolver `service_completed_successfully` **and** db `service_healthy`.
   - `entrypoint: ["/with-secrets.sh"]`, `command: ["sh","-c","alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*'"]`
     (the current Dockerfile CMD already does migrate-then-serve; `with-secrets.sh` wraps it to
     inject env). Migrations run on boot — **single container only**; add a migration lock before
     scaling to replicas (noted in the Dockerfile).
   - Volumes: `/run/op-secrets/quarterback:/run/secrets:ro` (+ `/data/secrets/with-secrets.sh:/with-secrets.sh:ro`).
   - The image runs as **root** → no `group_add` needed to read the `0440 root:30000` files.
     If you switch it to a non-root user, add `group_add: ["30000"]` (the #086 Defect-1 landmine).
   - Join the docker network nginx uses; publish nothing to the host (nginx reaches it over the net).
   - **Do not set `BROWSER_DEV_USER`.**

## 3. Edge (HAProxy → nginx → Authelia)

- **DNS**: add `board.example.com` and `qb.example.com` (via the dynu tooling, `apis/dynu.py`).
- **HAProxy**: route both hostnames to the nginx tier.
- **nginx — `board.example.com`** (browser): Authelia `auth_request` (two_factor); pass through
  Authelia's `Remote-User`; **strip client-supplied `Remote-*`**; proxy to the app. SSE needs
  `proxy_buffering off;` and a long `proxy_read_timeout` on `/stream`.
- **nginx — `qb.example.com`** (agents): **no** Authelia; **strip client-supplied `Remote-*`**; proxy to
  the app; add a `limit_req` zone like `hooks.example.com`. Bearer is enforced by the app.
- **Authelia**: `access_control` rule `board.example.com → two_factor`; leave `qb.example.com`
  unprotected (or an explicit `bypass`).

## 4. Backups

Confirm the new `quarterback` Postgres is picked up by the fleet-wide pg backup (the spec assumes
"already backed up fleet-wide"). If backups enumerate pg containers/volumes, verify this one matches.

---

## 5. Post-deploy verification

```bash
# agent host: bearer required, no 2FA
curl -s https://qb.example.com/health                                   # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' https://qb.example.com/board   # 401 (no bearer)
curl -s -X POST https://qb.example.com/post -H "Authorization: Bearer <tok>" \
     -H 'Content-Type: application/json' -d '{"type":"status","summary":"prod up"}'   # {"id":…}

# spoof check: Remote-User must be stripped on the agent host
curl -s -o /dev/null -w '%{http_code}\n' -H 'Remote-User: devuser' https://qb.example.com/board  # 401, NOT 200

# browser host: open https://board.example.com → Authelia 2FA → board renders + SSE goes live
```

- [ ] Browser board loads behind 2FA and streams live posts.
- [ ] Agent host serves the API with bearer and **without** 2FA.
- [ ] `Remote-User` spoof on the agent host returns 401 (nginx strips it).
- [ ] `BROWSER_DEV_USER` is unset (agent-host `GET /` without auth → 401).
- [ ] Point one real agent's MCP (`QUARTERBACK_BASE_URL=https://qb.example.com`, `QUARTERBACK_TOKEN=…`)
      and confirm `board_post` / `board_read`.

---

## 6. After deploy — the remaining v3 piece

Cross-*worktree* discovery already ships (`/worktrees`, `report_git`, `find_commit`). What's left
for full cross-*device* cherry-pick is a **bare git remote on apphost** (SSH, CA-signed — the
low-friction default per the spec's open decision #2) so devices share an object store, then wire
`landed` post refs to a cherry-pick helper. Its own task; not a blocker for the coordination board.
