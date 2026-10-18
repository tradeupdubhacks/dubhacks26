"""HTTP client for the documented model API. No session or watch routes."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, payload: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.payload = payload or {}


class ModelClient:
    """Calls only the routes in docs/model-api.md.

    The default is 127.0.0.1, not localhost. The server listens on IPv4 only, and
    Windows resolves localhost to ::1 first, then waits about 2s on the refused
    connection before trying 127.0.0.1. That 2s lands on every request.
    """

    def __init__(self, base_url: str = "http://127.0.0.1:3000", timeout: float = 10.0, session_id: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # The server runs one market per live session. With an id, every session-scoped
        # call (account, bars, stream) goes to that session; without one, to the latest.
        self.session_id = session_id

    def _scoped(self, path: str) -> str:
        if not self.session_id:
            return path
        return path + ("&" if "?" in path else "?") + "sessionId=" + urllib.parse.quote(self.session_id)

    def request(self, method: str, path: str, body: dict | None = None) -> Any:
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            raw = error.read()
            payload = {}
            if raw:
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = {}
            info = payload.get("error") or {}
            raise ApiError(
                error.code,
                str(info.get("code") or f"http_{error.code}"),
                str(info.get("message") or error.reason),
                payload,
            ) from error

    def health(self) -> dict:
        body = self.request("GET", "/v1/health")
        if body.get("execution") not in ("fake", "solana"):
            raise RuntimeError(
                "Refusing to trade because GET /v1/health execution is not 'fake' or 'solana'."
            )
        return body

    def instruments(self) -> dict:
        return self.request("GET", "/v1/instruments")

    def account(self) -> dict:
        return self.request("GET", self._scoped("/v1/account"))

    def bars(self, limit: int = 601) -> dict:
        """Recent 200ms bars, including the two minutes the server plays before a session."""
        return self.request("GET", self._scoped(f"/v1/bars?limit={limit}"))

    def reset(self, body: dict | None = None) -> dict:
        return self.request("POST", "/v1/reset", body or {})

    def step(self, body: dict | None = None) -> dict:
        return self.request("POST", "/v1/step", body or {})

    def ws_url(self) -> str:
        return self.base_url.replace("https://", "wss://").replace("http://", "ws://") + self._scoped("/v1/stream")

    def sessions(self) -> list[dict]:
        """Every live session on the server: sessionId, status, strategy, instrument."""
        return self.request("GET", "/v1/sessions")["sessions"]

    def session(self) -> dict:
        return self.request("GET", self._scoped("/v1/session"))
