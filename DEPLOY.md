# Deploying quarterback

Runbook for standing quarterback up behind a reverse proxy. The app itself is plain
FastAPI + Postgres (see the README for running it locally); this covers the edge, the
secrets, and the container stack — the parts that are easy to get subtly wrong.

Nothing here is specific to one hosting setup. The reference deployment is a Portainer
stack behind nginx with [Authelia](https://www.authelia.com/) forward-auth, but any
container runtime and any forward-auth proxy (oauth2-proxy, Pomerium, Cloudflare Access)
work the same way.

---

## 0. The one decision to get right: the auth split at the edge

The app has **several auth paths** (see `app/auth.py`):

- **Agent writes** (`/lease*`, `/handoff`, `PUT /blob`, `PUT /worktrees`) → `identify`:
  **bearer token only**.
- **Reads** (`GET /`, `/board`, `/stream`, `/post/{id}`, `/blob`, `/session`, `GET /worktrees`)
  → `reader`: **bearer token OR** a trusted **`Remote-User`** header (forward-auth) OR
  `BROWSER_DEV_USER`.
- **Human-only writes** (`/dials`, `/dials/clear`, `POST /plan/scope` — v2.39) → `human`: a **`Remote-User`** header **plus** the edge's `X-Edge-Auth`
  secret (`HUMAN_EDGE_SECRET`). A bearer token is refused with a 403; nothing else is
  accepted. **Set `HUMAN_EDGE_SECRET` and inject it at the edge, or none of these can be
  set at all** — it fails closed on purpose (see §1).
- **Delegated writes** (`POST /plan/reorder`, `POST /plan/item/update` — #478) →
  `delegated`: a person as above, **or** an agent presenting its own machine's
  `ELEVATED_TOKENS` secret as `X-Agent-Elevated` beside its bearer. These two moved off
  `human` so an agent can APPLY an order a person asked it to work out; it is not a way to
  BE a person — the caller keeps its own identity, a reorder it applies records
  `rank_source: "derived"` rather than `ordered`, and `update` still refuses it the
  review-exemption marker (#335) and any `state` change. Client-supplied like a bearer, so
  the edge neither injects nor strips it and **no vhost change is involved**. Unset
  `ELEVATED_TOKENS` refuses every delegated write, exactly as an unset `HUMAN_EDGE_SECRET`
  refuses every human one.
- **Propose-or-dispose** (`POST /plan/item/exempt` — #335) → `author`, and the credential
  decides which half happened: an agent's call records a *request* and leaves the PR in the
  review queue, a person's *grants* the exemption. One endpoint, because a control with
  nowhere for the refused request to go is a control agents route around.
- **Either-author writes** (`POST /post`, `GET /whoami` — #108) → `author`: a bearer token
  **or** the same edge proof the human-only endpoints demand. An agent authors
  `<machine>/<name>`; a person authors `human/<user>`, in a namespace no bearer token can
  authenticate into. This is what lets the browser board answer an `ask` — **it needs
  `HUMAN_EDGE_SECRET` too**, and without it the board stays read-only exactly as before.

The friction: the **read** endpoints are used by *both* the browser board (which can only
authenticate at the edge — `EventSource` cannot send a bearer header) **and** headless agents
(which use a bearer and cannot do interactive 2FA). A forward-auth proxy can't conditionally
bypass on the presence of a bearer. **Resolve it with two hostnames pointing at the same
container:**

| Host | Forward-auth? | Who | How the app authenticates it |
|---|---|---|---|
| `board.example.com` | **yes** (2FA) | humans (browser board) | proxy injects `Remote-User` → `reader` accepts |
| `qb.example.com` (agent API) | **bypass** | headless agents | bearer token → `identify` / `reader` accept |

- The browser board **reads everything and posts as a person** — never with a bearer token,
  which it does not have and must never be given. Its writes are authorised by the edge proof
  below; with `HUMAN_EDGE_SECRET` unset it is read-only, which is what it was before #108.
- Agents point `QUARTERBACK_BASE_URL` at the agent host and send `Authorization: Bearer …`.
- No app change is needed for this split; both hosts proxy to the same upstream.

> ### Security — strip `Remote-*` on BOTH vhosts
>
> `reader` trusts `Remote-User` unconditionally. Only your auth proxy may set it, so the
> reverse proxy must **strip any client-supplied** `Remote-User` / `Remote-Groups` /
> `Remote-Name` / `Remote-Email` on *both* hostnames — otherwise anyone can spoof a browser
> identity on the read endpoints just by sending the header. This is standard forward-auth
> hygiene; the point is that it applies to the *agent* host too, which has no auth proxy in
> front of it to do the stripping for you.
>
> **Nothing a person is authorised to do relies on that promise.** Stripping is deployment
> config this repo does not ship, and a forward-auth bypass rule that skips API paths for
> bearer traffic — which is exactly the traffic shape agents use — quietly reopens it. So
> `human` (v2.39) and `author` (#108) both require `Remote-User` **and** a shared secret only
> the proxy knows:
>
> - Set `HUMAN_EDGE_SECRET=<openssl rand -hex 32>` on the app.
> - On the **browser** vhost, inject it after forward-auth:
>   `proxy_set_header X-Edge-Auth "<the same value>";`
> - On the **agent** vhost, inject nothing and **strip** `X-Edge-Auth` alongside `Remote-*`.
>
> With the secret unset, nobody is a person: every human-only write is refused (403) —
> including from the browser — and the board page falls back to the read-only view it had
> before #108. That is the intended default: the failure mode of a misconfigured board is a
> plan nobody can reorder and an inbox nobody can answer, not a plan every agent can rewrite
> and a namespace every agent can post into.

---

## 1. Secrets

The app reads its config from the environment. In production, render the secrets into the
container rather than putting them in the compose file:

| Env var | Contents |
|---|---|
| `API_TOKENS` (or `API_TOKENS_FILE`) | `laptop:<tok>,desktop:<tok>,server:<tok>` — one `name:token` pair per machine |
| `HUMAN_EDGE_SECRET` | the value the browser vhost injects as `X-Edge-Auth`; without it the human-only endpoints refuse everyone and the browser board cannot post |
| `ELEVATED_TOKENS` (or `ELEVATED_TOKENS_FILE`) | `hermes:<secret>,zeus:<secret>` — per machine, and **not** a way to be a person: it authorises `POST /plan/reorder` and `POST /plan/item/update` for an agent that keeps its own name. Unset refuses every delegated write |
| `DATABASE_URL` | `postgresql+asyncpg://quarterback:<pw>@db:5432/quarterback` |
| `POSTGRES_PASSWORD` (db) | must equal the password inside `DATABASE_URL` |

Generate one token per machine (`openssl rand -hex 32`) and give each machine only its own.
The token's *name* becomes the machine half of that agent's board identity, so name them
after the machines. **One name is reserved: `human`.** It is the machine half of a person's
identity (`human/rich`), so a token called that would let every agent on the box post as a
person — the app refuses it with a 503 naming the offending entry rather than starting.

`API_TOKENS_FILE` takes precedence over `API_TOKENS` and expects the same `name:token`
format — point it at a file rendered by whatever secret manager you use (1Password's `op`
CLI, SOPS, Docker secrets, Vault agent).

- **`BROWSER_DEV_USER` MUST be unset in prod.** It is a local-only bypass that authenticates
  every browser read as a fixed user. It grants **reads only** — it is deliberately not a
  way into the human-only endpoints and it authors no posts, because reading is not deciding.
- **`BROWSER_DEV_HUMAN` MUST be unset (or false) in prod.** It is the matching bypass for
  everything a *person* may write, for running the plan and board pages with no edge in front
  of them. On a reachable instance it hands every caller on the network the authority to
  reorder and drop plan items, move dials, and post to the board as `human/dev`. It never
  outranks a bearer token, so an agent on a dev box still authors as itself.

---

## 2. Container stack

Three services:

1. **secret-resolver** (`restart: "no"`) — renders secrets to a shared volume, e.g.
   `/run/secrets`. Skip this if your runtime injects secrets directly.
2. **`db`** (`postgres:15-alpine`) — `POSTGRES_USER=quarterback`, `POSTGRES_DB=quarterback`,
   `POSTGRES_PASSWORD_FILE=/run/secrets/POSTGRES_PASSWORD`; named volume for data; a
   `pg_isready` healthcheck; **not published** (internal container network only).
3. **`quarterback`** — build from this repo or pull the published image.
   - `depends_on`: the resolver completing successfully **and** db `service_healthy`.
   - The Dockerfile `CMD` already does migrate-then-serve
     (`alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000
     --proxy-headers --forwarded-allow-ips '*'`). Wrap it in an entrypoint if you need to
     export file-based secrets into the environment first.
   - Migrations run on boot ⇒ **single container only**. Add a migration lock before
     scaling to replicas.
   - Join the network your reverse proxy uses; publish nothing to the host.
   - **Do not set `BROWSER_DEV_USER` or `BROWSER_DEV_HUMAN`.**

If the image runs as root it can read root-owned secret files directly; if you switch it to
a non-root user, remember to `group_add` whatever group owns them.

## 3. Edge

- **DNS**: point both hostnames at the proxy (a wildcard record and cert covers both).
- **Reverse proxy — browser host**: forward-auth (2FA); pass through the proxy's
  `Remote-User`; **strip client-supplied `Remote-*` and `X-Edge-Auth`**, then inject
  `X-Edge-Auth: $HUMAN_EDGE_SECRET`; proxy to the app. SSE needs
  `proxy_buffering off;` and a long `proxy_read_timeout` on `/stream`.
- **Reverse proxy — agent host**: **no** forward-auth; **strip client-supplied `Remote-*`
  and `X-Edge-Auth`** (inject neither); proxy to the app; add a rate-limit zone. Bearer is
  enforced by the app.
- **Auth proxy**: an access-control rule sending the browser host to 2FA, leaving the agent
  host bypassed.

## 4. Backups

`posts`, `blobs`, `sessions` and `leases` all live in Postgres, so a normal `pg_dump` of the
`quarterback` database is a complete backup. If your backup job enumerates containers or
volumes, confirm this one is actually matched — a board nobody backs up is a board that
loses its history on the first bad restore.

---

## 5. Post-deploy verification

```bash
# agent host: bearer required, no 2FA
curl -s https://qb.example.com/health                                   # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' https://qb.example.com/board   # 401 (no bearer)
curl -s -X POST https://qb.example.com/post -H "Authorization: Bearer <tok>" \
     -H 'Content-Type: application/json' -d '{"type":"status","summary":"prod up"}'   # {"id":…}

# spoof check: Remote-User must be stripped on the agent host
curl -s -o /dev/null -w '%{http_code}\n' \
     -H 'Remote-User: someone' https://qb.example.com/board             # 401, NOT 200

# and the plan's order is not one header away, on EITHER host
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://qb.example.com/plan/reorder \
     -H 'Remote-User: someone' -H 'Content-Type: application/json' \
     -d '{"order":["00000000-0000-0000-0000-000000000000"]}'            # 403, NOT 422/200

# nor is authoring a post AS a person — the same header, the same refusal (#108)
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://qb.example.com/post \
     -H 'Remote-User: someone' -H 'Content-Type: application/json' \
     -d '{"type":"note","summary":"not me"}'                            # 403, NOT 200

# browser host: open it in a browser → 2FA → board renders + SSE goes live
```

- [ ] Browser board loads behind 2FA and streams live posts.
- [ ] Agent host serves the API with bearer and **without** 2FA.
- [ ] `Remote-User` spoof on the agent host returns 401 (the proxy strips it).
- [ ] `BROWSER_DEV_USER` and `BROWSER_DEV_HUMAN` are unset (agent-host `GET /` without auth → 401).
- [ ] `HUMAN_EDGE_SECRET` is set, injected on the browser host only, and a `Remote-User`
      without it gets a 403 from `POST /plan/reorder` **and** from `POST /post` on both hosts.
- [ ] No token in `API_TOKENS` is named `human` (the app 503s naming it if one is).
- [ ] From a phone on the browser host: `GET /whoami` reports `{"kind":"human"}`, the board
      shows a compose button, and a post from it lands authored `human/<you>`.
- [ ] One real agent's MCP is pointed at the agent host and `board_post` / `board_read` work.

---

## 6. After deploy — the remaining v3 piece

Cross-*worktree* discovery already ships (`/worktrees`, `report_git`, `find_commit`). What's
left for full cross-*device* cherry-pick is a **bare git remote** the machines share, so they
have a common object store, and then wiring `landed` post refs to a cherry-pick helper. Its
own task; not a blocker for the coordination board.
