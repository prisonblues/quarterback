from __future__ import annotations

import logging
import sys

from fastapi import FastAPI

from app.api.blobs import router as blobs_router
from app.api.blockers import router as blockers_router
from app.api.board_view import router as board_view_router
from app.api.claims import router as claims_router
from app.api.dials import router as dials_router
from app.api.landing import router as landing_router
from app.api.leases import router as leases_router
from app.api.merge_queue import router as merge_queue_router
from app.api.plan import router as plan_router
from app.api.posts import router as posts_router
from app.api.review_ledger import router as review_ledger_router
from app.api.review_queue import router as review_queue_router
from app.api.review_refutations import router as review_refutations_router
from app.api.reviews import router as reviews_router
from app.api.stream import router as stream_router
from app.api.subagents import router as subagents_router
from app.api.sync import router as sync_router
from app.api.whoami import router as whoami_router
from app.api.worktrees import router as worktrees_router

# Explicit "app" logger handler — uvicorn's dictConfig at startup drops root
# handlers, so a dedicated namespace handler survives.
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
_app_logger = logging.getLogger("app")
_app_logger.setLevel(logging.INFO)
_app_logger.addHandler(_handler)

app = FastAPI(title="quarterback", version="3.23.0")
app.include_router(whoami_router)
app.include_router(posts_router)
app.include_router(stream_router)
app.include_router(blobs_router)
app.include_router(leases_router)
app.include_router(subagents_router)
# #772's finding ledger. Its own module and its own router, beside `reviews`
# rather than inside it: one table, two endpoints and one rule set, importing
# `reviews` in one direction only so the two never form a cycle.
#
# BEFORE `reviews_router`, and that ordering is load-bearing rather than
# tidiness. `reviews` ends with `GET /review/{run_id}`, a catch-all one segment
# deep, and Starlette matches routes in registration order — so included after
# it, `GET /review/ledger` is swallowed by that route and answers "ledger is not
# a valid integer". Registered first, the literal path wins and `{run_id}` still
# catches every numeric id, which is all it was ever for.
app.include_router(review_ledger_router)
# #773's refutation set, and BEFORE `reviews_router` for the reason directly
# above: `GET /review/refutations` is a literal path one segment deep, and
# registered after the catch-all it would answer "refutations is not a valid
# integer" — the exact failure the ledger's comment records.
app.include_router(review_refutations_router)
app.include_router(reviews_router)
app.include_router(review_queue_router)
app.include_router(worktrees_router)
app.include_router(sync_router)
app.include_router(claims_router)
app.include_router(merge_queue_router)
app.include_router(board_view_router)
app.include_router(plan_router)
app.include_router(landing_router)
app.include_router(blockers_router)
app.include_router(dials_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
