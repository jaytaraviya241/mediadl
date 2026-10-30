"""FastAPI application for the mediadl dashboard.

Binds to loopback by default and carries no authentication: it is a local
single-user control panel over a local job store. Exposing it on a network
interface would need the multi-tenant controls described in the spec
(per-principal authorization, sessions, signed download URLs).
"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from mediadl.api.routes import STATUS_BY_CODE, router
from mediadl.api.runtime import DashboardRuntime
from mediadl.config import Settings
from mediadl.errors import MediaDLError
from mediadl.obs.logging import get_logger

log = get_logger(__name__)

STATIC_DIR = Path(__file__).parent / 'static'


def create_app(
    settings: Settings | None = None, *, runtime: DashboardRuntime | None = None
) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.runtime = runtime or DashboardRuntime(settings)
        log.info(
            'dashboard_started',
            output_root=str(app.state.runtime.ctx.output.root),
            connectors=[c.connector_id for c in app.state.runtime.ctx.registry.connectors],
        )
        try:
            yield
        finally:
            await app.state.runtime.aclose()

    app = FastAPI(
        title='mediadl dashboard',
        version='0.1.0',
        summary='Local control panel for authorized media downloads',
        lifespan=lifespan,
    )

    @app.exception_handler(MediaDLError)
    async def _domain_error(request: Request, exc: MediaDLError) -> JSONResponse:
        # A domain error that escaped a route still reaches the UI as a
        # typed, redacted payload rather than a stack trace.
        return JSONResponse(
            status_code=STATUS_BY_CODE.get(exc.code, 500),
            content={
                'detail': {
                    'code': str(exc.code),
                    'message': exc.user_message,
                    'retryable': exc.retryable,
                    'detail': dict(exc.public().get('detail') or {}),
                }
            },
        )

    app.include_router(router)

    @app.get('/healthz')
    async def healthz() -> dict[str, Any]:
        return {'ok': True}

    if STATIC_DIR.is_dir():
        app.mount('/assets', StaticFiles(directory=STATIC_DIR), name='assets')

        @app.get('/', include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(STATIC_DIR / 'index.html')

    return app


__all__ = ['DashboardRuntime', 'create_app']
