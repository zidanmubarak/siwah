"""Local bearer-token authentication for the daemon."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Header, HTTPException


def require_token(token: str | None):
    async def dependency(
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> None:
        if token is None:
            return
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=401,
                detail={"code": "unauthorized", "message": "Missing local bearer token"},
            )
        supplied = authorization.removeprefix("Bearer ").strip()
        if not secrets.compare_digest(supplied, token):
            raise HTTPException(
                status_code=401,
                detail={"code": "unauthorized", "message": "Invalid local bearer token"},
            )

    return dependency
