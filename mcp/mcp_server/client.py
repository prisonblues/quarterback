"""HTTP client for the quarterback board API.

Wraps the coordination endpoints with bearer-token auth and consistent error
handling. The token identifies the agent — its configured name becomes the
author of every post it makes.
"""

from __future__ import annotations

import hashlib

import httpx


class QuarterbackClient:
    """HTTP wrapper around the quarterback board API."""

    def __init__(
        self,
        base_url: str,
        api_token: str,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = http_client or httpx.Client(
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=30,
        )

    def close(self) -> None:
        self._http.close()

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def post(self, body: dict) -> dict:
        resp = self._http.post(self._url("/post"), json=body)
        resp.raise_for_status()
        return resp.json()

    def board(self, params: dict) -> list[dict]:
        resp = self._http.get(self._url("/board"), params=params)
        resp.raise_for_status()
        return resp.json()

    def get_post(self, post_id: int) -> dict:
        resp = self._http.get(self._url(f"/post/{post_id}"))
        resp.raise_for_status()
        return resp.json()

    # -- blobs (v2) ----------------------------------------------------

    def put_blob(self, content: bytes) -> dict:
        sha = hashlib.sha256(content).hexdigest()
        resp = self._http.put(self._url(f"/blob/{sha}"), content=content)
        resp.raise_for_status()
        return resp.json()

    def get_blob(self, sha: str) -> bytes:
        resp = self._http.get(self._url(f"/blob/{sha}"))
        resp.raise_for_status()
        return resp.content

    # -- leases / handoff (v2) -----------------------------------------

    def lease(self, body: dict) -> dict:
        resp = self._http.post(self._url("/lease"), json=body)
        resp.raise_for_status()
        return resp.json()

    def renew_lease(self, lease_id: str) -> dict:
        resp = self._http.post(self._url("/lease/renew"), json={"lease_id": lease_id})
        resp.raise_for_status()
        return resp.json()

    def release_lease(self, lease_id: str) -> dict:
        resp = self._http.post(self._url("/lease/release"), json={"lease_id": lease_id})
        resp.raise_for_status()
        return resp.json()

    def handoff(self, session: str, blob: str) -> dict:
        resp = self._http.post(self._url("/handoff"), json={"session": session, "blob": blob})
        resp.raise_for_status()
        return resp.json()

    def session_state(self, session: str) -> dict:
        resp = self._http.get(self._url(f"/session/{session}"))
        resp.raise_for_status()
        return resp.json()

    # -- worktree registry (v2.1) --------------------------------------

    def put_worktrees(self, body: dict) -> dict:
        resp = self._http.put(self._url("/worktrees"), json=body)
        resp.raise_for_status()
        return resp.json()

    def get_worktrees(self, params: dict) -> list[dict]:
        resp = self._http.get(self._url("/worktrees"), params=params)
        resp.raise_for_status()
        return resp.json()

    # -- publish / sync advisories (v2.8) ------------------------------

    def sync(self, params: dict) -> dict:
        resp = self._http.get(self._url("/sync"), params=params)
        resp.raise_for_status()
        return resp.json()

    # -- coordination: collision index + sub-agents (v2.6) -------------

    def active(self, params: dict) -> dict:
        resp = self._http.get(self._url("/active"), params=params)
        resp.raise_for_status()
        return resp.json()

    def overlap(self, params: dict) -> dict:
        resp = self._http.get(self._url("/overlap"), params=params)
        resp.raise_for_status()
        return resp.json()

    def subagent_start(self, body: dict) -> dict:
        resp = self._http.post(self._url("/subagent"), json=body)
        resp.raise_for_status()
        return resp.json()

    def subagent_end(self, body: dict) -> dict:
        resp = self._http.post(self._url("/subagent/end"), json=body)
        resp.raise_for_status()
        return resp.json()
