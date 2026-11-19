"""hydrowf-registry: legos over HTTP, on FastAPI.

    uvicorn --factory hydrowf_registry.app:app_from_env --host 0.0.0.0 --port 8000

    GET  /legos?kind=tool|data|pipeline   the legos the caller may see (public ones and their own)
    GET  /legos/{id}                      that lego's versions the caller may see, oldest first
    GET  /legos/{id}/{version}            one lego; someone else's private lego is 404, like a missing one
    POST /legos[?owner_group=<group>]     publish one lego version (a bearer token; may_add and the
                                          id's owners decide, see hydrowf_registry.store)
    GET  /healthz                         200 once the app is up

Bearer tokens are Keycloak access tokens (hydrowf_core.auth: HYDROWF_OIDC_ISSUER, HYDROWF_OIDC_AUDIENCE),
the same as hydrowf-ogc's. Reads need none, but a bad token is a 401 even on a read: it never falls back
to anonymous. The lego's `owner` is always the token's `sub`.

A request body is at most MAX_BODY bytes, counted as it streams in (BodyLimit), whatever its headers say.
Every 422 has FastAPI's shape: {"detail": [{"loc": [...], "msg": "...", "type": "..."}]}.
"""
# No `from __future__ import annotations` here: FastAPI reads these annotations to wire dependencies,
# and string annotations could not name the dependencies local to create_app.
from typing import Annotated, Literal

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from hydrowf_core.auth import BearerAuth, bearer_from_env, bearer_token, groups_from
from hydrowf_core.models import DataLego, Lego, ToolLego
from hydrowf_core.models.lego import LegoId, SemVer

from hydrowf_registry.db import engine_from_env
from hydrowf_registry.store import Duplicate, Invalid, NotAllowed, Publisher, Store

MAX_BODY = 1024 * 1024                    # bytes; a lego is a few KB
TOOL_AUTHORS = "tool-authors"             # the Keycloak group that deploys tools and managed data
# A Keycloak group name, or a path of names as "Full group path" writes it without the leading "/"
GROUP = r"^[A-Za-z0-9][A-Za-z0-9 ._-]*(/[A-Za-z0-9][A-Za-z0-9 ._-]*)*$"
# Responses are the JSON stored at publish time, returned as is (never validated again on the way out,
# so a stricter hydrowf-core can't break reads); these only describe their shape in /docs.
ONE_LEGO = {200: {"model": Lego, "description": "The lego, as published"}}
LEGO_LIST = {200: {"model": list[Lego], "description": "Legos, as published"}}


def may_add(lego: Lego, caller: Publisher) -> bool:
    """Which kinds a caller may publish at all (decided 2026-11-14): tools and managed or output data
    deploy code and data for everyone, so only tool-authors publish them; external data is a pointer to
    something public, and a pipeline only wires existing legos, so any signed-in user may. Whose id it
    is comes after, in the store."""
    if isinstance(lego, ToolLego):
        return TOOL_AUTHORS in caller.groups
    if isinstance(lego, DataLego):
        return lego.data_kind == "external" or TOOL_AUTHORS in caller.groups
    return True                           # a pipeline


def unauthorized(message: str, token_sent: bool) -> HTTPException:
    challenge = 'Bearer error="invalid_token"' if token_sent else "Bearer"
    return HTTPException(401, message, headers={"WWW-Authenticate": challenge})


def unprocessable(problems: list[str]) -> HTTPException:
    """The store's problems in FastAPI's own 422 shape, so clients read one shape."""
    return HTTPException(422, [{"loc": ["body"], "msg": problem, "type": "registry"} for problem in problems])


class BodyLimit:
    """ASGI middleware: reads every request's body before the app does, counting it, and answers 413 once
    it passes `max_body` (at once when Content-Length already says so). The app then gets the body replayed.
    Counting is what limits memory: a chunked body has no Content-Length, and a declared one can be wrong."""

    def __init__(self, app: ASGIApp, max_body: int = MAX_BODY):
        self.app, self.max_body = app, max_body

    async def too_large(self, scope: Scope, receive: Receive, send: Send) -> None:
        await JSONResponse({"detail": f"the body is over {self.max_body} bytes"}, status_code=413)(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > self.max_body:
            return await self.too_large(scope, receive, send)
        chunks, size, more = [], 0, True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_body:
                return await self.too_large(scope, receive, send)
            chunks.append(chunk)
            more = message.get("more_body", False)
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if replayed:
                return await receive()        # after the body: disconnect, as the server reports it
            replayed = True
            return {"type": "http.request", "body": b"".join(chunks), "more_body": False}

        await self.app(scope, replay, send)


def create_app(store: Store, bearer: BearerAuth | None) -> FastAPI:
    app = FastAPI(title="hydrowf-registry", description="Versioned tool, data and pipeline legos",
                  version="0.1.0")

    def caller(authorization: Annotated[str | None, Header()] = None) -> Publisher | None:
        """The token's caller, None without a bearer credential, 401 for any bearer credential not
        valid for us. A plain def: FastAPI runs it in a worker thread, where the key fetch may block."""
        token = bearer_token(authorization)
        if token is None:
            return None
        claims = bearer.claims(token) if bearer and token else None
        sub = claims.get("sub") if claims else None
        if not isinstance(sub, str) or not sub:
            raise unauthorized("Invalid bearer token", token_sent=True)
        return Publisher(sub, groups_from(claims))

    def signed_in(who: Annotated[Publisher | None, Depends(caller)]) -> Publisher:
        if who is None:
            raise unauthorized("Authentication required", token_sent=False)
        return who

    def sub_of(who: Publisher | None) -> str | None:
        return who.sub if who else None

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/legos", responses=LEGO_LIST)
    def list_legos(who: Annotated[Publisher | None, Depends(caller)],
                   kind: Literal["tool", "data", "pipeline"] | None = None) -> list[dict]:
        return store.visible(sub_of(who), kind=kind)

    @app.get("/legos/{id}", responses=LEGO_LIST)
    def lego_versions(id: LegoId, who: Annotated[Publisher | None, Depends(caller)]) -> list[dict]:
        found = store.visible(sub_of(who), id=id)
        if not found:
            raise HTTPException(404, f"no lego {id}")
        return found

    @app.get("/legos/{id}/{version}", responses=ONE_LEGO)
    def get_lego(id: LegoId, version: SemVer, who: Annotated[Publisher | None, Depends(caller)]) -> dict:
        lego = store.get(id, version, sub_of(who))
        if lego is None:
            raise HTTPException(404, f"no lego {id}@{version}")
        return lego

    @app.post("/legos", status_code=201, responses={201: {"model": Lego, "description": "The lego, as stored"}})
    async def add_lego(lego: Annotated[Lego, Body()], who: Annotated[Publisher, Depends(signed_in)],
                       request: Request, response: Response,
                       owner_group: Annotated[str | None, Query(max_length=255, pattern=GROUP)] = None) -> dict:
        """Publish one lego version. On an id's first version, `owner_group` (a Keycloak group you are
        in) makes the id the group's: its members publish the next versions."""
        if not may_add(lego, who):
            raise HTTPException(403, f"publishing a {lego.kind} lego needs the {TOOL_AUTHORS} group")
        try:
            stored = await run_in_threadpool(store.add, lego, who, owner_group)
        except Duplicate as err:
            raise HTTPException(409, str(err)) from err
        except NotAllowed as err:
            raise HTTPException(403, str(err)) from err
        except Invalid as err:
            raise unprocessable(err.problems) from err
        response.headers["Location"] = str(request.url_for("get_lego", id=stored.id, version=stored.version))
        return stored.model_dump(mode="json")

    app.add_middleware(BodyLimit)
    return app


def app_from_env() -> FastAPI:
    """uvicorn's factory: DATABASE_URL (hydrowf_registry.db) and the OIDC settings."""
    return create_app(Store(engine_from_env()), bearer_from_env())
