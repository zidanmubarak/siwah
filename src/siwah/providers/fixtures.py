"""Record/replay fixtures for provider HTTP exchanges.

Fixtures contain only method, URL, request body, response status/headers/body.
They never contain request headers (and therefore never credentials).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx


class FixtureTransport(httpx.AsyncBaseTransport):
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data = _load(path)
        self._cursor = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = (await request.aread()).decode("utf-8", errors="replace")
        exchanges = self._data.get("exchanges", [])
        for index in range(self._cursor, len(exchanges)):
            exchange = exchanges[index]
            recorded = exchange.get("request", {})
            if recorded.get("method", "POST").upper() != request.method.upper():
                continue
            if recorded.get("url") != str(request.url):
                continue
            recorded_body = recorded.get("body")
            if recorded_body is not None and not _same_body(recorded_body, body):
                continue
            self._cursor = index + 1
            response = exchange.get("response", {})
            return httpx.Response(
                status_code=int(response.get("status", 200)),
                headers=response.get("headers", {}),
                content=str(response.get("body", "")).encode("utf-8"),
                request=request,
            )
        raise httpx.ConnectError(
            f"No recorded fixture matched {request.method} {request.url}", request=request
        )


def append_exchange(
    path: Path,
    *,
    url: str,
    request_body: dict[str, Any],
    status: int,
    response_headers: dict[str, str],
    response_body: str,
) -> None:
    data = _load(path)
    data.setdefault("exchanges", []).append(
        {
            "request": {
                "method": "POST",
                "url": url,
                "body": json.dumps(request_body, ensure_ascii=False, sort_keys=True),
            },
            "response": {
                "status": status,
                "headers": {
                    key: value
                    for key, value in response_headers.items()
                    if key.lower()
                    in {"content-type", "retry-after", "x-opencode-log-id", "x-zen-model"}
                },
                "body": response_body,
            },
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _same_body(recorded: str, actual: str) -> bool:
    try:
        return json.loads(recorded) == json.loads(actual)
    except json.JSONDecodeError:
        return recorded == actual


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exchanges": []}
    return json.loads(path.read_text(encoding="utf-8"))
