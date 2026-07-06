# Deploying quarterback

Handoff runbook for standing up `quarterback.fo.ls` on **atlas**. The app is built and
verified locally (see README); this covers the edge + secrets + stack wiring only.

> ## Deployment status — 2026-07-05 · ✅ LIVE
>
> Fully deployed and verified on atlas:
> - GitHub repo `prisonblues/quarterback` (private) + CI; image `ghcr.io/prisonblues/quarterback:latest`.
> - **Portainer stack `quarterback` (id 189)** up: op-resolver **Exited 0** (1Password secret good),
>   db healthy, app healthy, Alembic migrations 0001→0003 applied on boot. Image pulled fine.
> - nginx vhosts live: `quarterback.fo.ls` (Authelia 2FA) + `qb.fo.ls` (bearer, strips all `Remote-*`,
>   `limit_req`, 200m body), both proxy to loopback `127.0.0.1:9037`.
> - HAProxy: both hosts in PUBLIC+LOCAL maps, reloaded. **DNS + TLS were no-ops** (`*.fo.ls`
>   wildcard covers both — corrects §3's dynu/per-domain assumptions).
> - Authelia: `quarterback.fo.ls → two_factor` rule inserted into the **live** config in-container
>   (byte-exact head/tail insert — the config holds an inline OIDC key #094, never read), validated,
>   authelia restarted healthy. Backup at `/config/configuration.yml.bak-quarterback`.
>
> **Verification (all pass):** `qb.fo.ls/health` → `{"status":"ok"}`; `qb.fo.ls/board` (no auth) → 401;
> **spoof check** `Remote-User: rich` → **401** (strip works); `quarterback.fo.ls/` → 302→login;
> `rss.fo.ls/` → 302 (fleet auth intact).
>
> **Remaining (per-device, when convenient):**
> 1. **Distribute agent tokens:** `op read op://atlas/quarterback/api_tokens` → give each `name:token`
>    to the matching machine's MCP env (`QUARTERBACK_TOKEN`, `QUARTERBACK_BASE_URL=https://qb.fo.ls`).
> 2. **Smoke-test a write** with your token:
>    `curl -X POST https://qb.fo.ls/post -H "Authorization: Bearer <laptop-token>" -H 'Content-Type: application/json' -d '{"type":"status","summary":"hello from laptop"}'`
> 3. Optional: browser-load `https://quarterback.fo.ls` (2FA) to see the board render + stream live.
>
> **Not committed:** the selfhost artifacts (`stacks/quarterback.yml`, `nginx/sites-available/{quarterback,qb}.fo.ls`,
> the `authelia/configuration.yml` rule) are left uncommitted in `selfhost` since master has other in-flight
> changes — commit them when you tidy up. (The live authelia config was edited directly, not from the repo copy.)

Canonical spec: `selfhost/issues/open/127-…`. Patterns to mirror:
`selfhost/stacks/miniflux.yml` (op-resolver), `selfhost/secrets/README.md`,
`selfhost/nginx/sites-available/hooks.fo.ls` (bearer path), an existing Authelia-protected
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
| `quarterback.fo.ls` | **two_factor** | humans (browser board) | Authelia injects `Remote-User` → `reader` accepts |
| `qb.fo.ls` (agent API) | **bypass** | laptop / desktop / zeus agents | bearer token → `identify` / `reader` accept |

- The browser board is **read-only** — writes need a bearer, which the browser host never has. Fine.
- Agents point `QUARTERBACK_BASE_URL` at **`qb.fo.ls`** and send `Authorization: Bearer …`.
- No app change is needed for this split; both hosts proxy to the same upstream.

> **Security — strip `Remote-User` on BOTH vhosts.** The app trusts `Remote-User` unconditionally.
> Only Authelia (on `quarterback.fo.ls`) may set it; nginx must **strip any client-supplied**
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
code path needed**. Provision in 1Password (vault `atlas`, item `quarterback`):

| op ref | Consumed as | Contents |
|---|---|---|
| `op://atlas/quarterback/api_tokens` | `API_TOKENS` (app) | `laptop:<tok>,desktop:<tok>,zeus:<tok>` — one pair per agent |
| `op://atlas/quarterback/database_url` | `DATABASE_URL` (app) | `postgresql+asyncpg://quarterback:<pw>@db:5432/quarterback` |
| `op://atlas/quarterback/db_password` | `POSTGRES_PASSWORD_FILE` (db) | must equal the password inside `database_url` |

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

- **DNS**: add `quarterback.fo.ls` and `qb.fo.ls` (via the dynu tooling, `apis/dynu.py`).
- **HAProxy**: route both hostnames to the nginx tier.
- **nginx — `quarterback.fo.ls`** (browser): Authelia `auth_request` (two_factor); pass through
  Authelia's `Remote-User`; **strip client-supplied `Remote-*`**; proxy to the app. SSE needs
  `proxy_buffering off;` and a long `proxy_read_timeout` on `/stream`.
- **nginx — `qb.fo.ls`** (agents): **no** Authelia; **strip client-supplied `Remote-*`**; proxy to
  the app; add a `limit_req` zone like `hooks.fo.ls`. Bearer is enforced by the app.
- **Authelia**: `access_control` rule `quarterback.fo.ls → two_factor`; leave `qb.fo.ls`
  unprotected (or an explicit `bypass`).

## 4. Backups

Confirm the new `quarterback` Postgres is picked up by the fleet-wide pg backup (the spec assumes
"already backed up fleet-wide"). If backups enumerate pg containers/volumes, verify this one matches.

---

## 5. Post-deploy verification

```bash
# agent host: bearer required, no 2FA
curl -s https://qb.fo.ls/health                                   # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' https://qb.fo.ls/board   # 401 (no bearer)
curl -s -X POST https://qb.fo.ls/post -H "Authorization: Bearer <tok>" \
     -H 'Content-Type: application/json' -d '{"type":"status","summary":"prod up"}'   # {"id":…}

# spoof check: Remote-User must be stripped on the agent host
curl -s -o /dev/null -w '%{http_code}\n' -H 'Remote-User: rich' https://qb.fo.ls/board  # 401, NOT 200

# browser host: open https://quarterback.fo.ls → Authelia 2FA → board renders + SSE goes live
```

- [ ] Browser board loads behind 2FA and streams live posts.
- [ ] Agent host serves the API with bearer and **without** 2FA.
- [ ] `Remote-User` spoof on the agent host returns 401 (nginx strips it).
- [ ] `BROWSER_DEV_USER` is unset (agent-host `GET /` without auth → 401).
- [ ] Point one real agent's MCP (`QUARTERBACK_BASE_URL=https://qb.fo.ls`, `QUARTERBACK_TOKEN=…`)
      and confirm `board_post` / `board_read`.

---

## 6. After deploy — the remaining v3 piece

Cross-*worktree* discovery already ships (`/worktrees`, `report_git`, `find_commit`). What's left
for full cross-*device* cherry-pick is a **bare git remote on atlas** (SSH, CA-signed — the
low-friction default per the spec's open decision #2) so devices share an object store, then wire
`landed` post refs to a cherry-pick helper. Its own task; not a blocker for the coordination board.
