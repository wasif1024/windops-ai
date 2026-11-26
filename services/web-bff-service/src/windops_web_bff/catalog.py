"""The platform catalog, for the administrator console's MCP integrations and gateway policies
pages (M11.7, SRS 9.4).

Both are platform configuration: changed by pull request, reviewed there, and deployed with a
release, never by a tenant. scripts/generate/platform_catalog.py makes the catalog from their
sources, and a repository test keeps it matching them. Reading it needs audit.read, as reviewing
changes does, which the Authorization Service decides for the signed-in user.
"""

import json
import os
from collections.abc import Awaitable, Callable
from functools import cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from windops.authorization.v1 import authorization_pb2

from windops_web_bff.common import Call
from windops_web_bff.errors import problem
from windops_web_bff.tokens import Caller

CATALOG = Path(__file__).with_name("platform_catalog.json")
REVIEWER = "audit.read"


@cache
def catalog() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(CATALOG.read_text())
    return loaded


def add_catalog_routes(
    router: APIRouter,
    caller: Callable[..., Awaitable[Caller]],
    call_as: Call,
    refused: Callable[[JSONResponse], Exception],
) -> None:
    @router.get(
        "/admin/platform-catalog",
        summary="Every MCP server and tool with its limits and agents, and every gateway policy, "
        "as built into this release",
    )
    async def platform_catalog(who: Annotated[Caller, Depends(caller)]) -> dict[str, Any]:
        check = authorization_pb2.CheckRequest(permission=REVIEWER)
        if not (await call_as("authorization", "Check", check, who)).allowed:
            raise refused(problem(403, "You do not have permission to do this", "forbidden"))
        return {"release": os.environ.get("WINDOPS_VERSION", "unknown"), **catalog()}
