"""HTTP client for the quarterback board API.

Wraps the coordination endpoints with bearer-token auth and consistent error
handling. The token identifies the agent — its configured name becomes the
author of every post it makes.
"""

from __future__ import annotations

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
