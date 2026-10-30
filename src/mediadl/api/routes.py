"""Dashboard HTTP API.

Every endpoint reads or drives the same core the CLI uses. Domain errors are
translated to status codes once, here, so the UI only ever sees
``{code, message, detail}``.
"""
from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from mediadl.api import credentials_guide as guide
from mediadl.api.runtime import DashboardRuntime, output_file
from mediadl.auth.connections import Connection, ConnectionStatus, new_connection_id
from mediadl.domain.models import LocalFileLocator
from mediadl.errors import ErrorCode, MediaDLError
from mediadl.jobs.states import JobState
from mediadl.obs.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix='/api')

#: Domain error -> HTTP status. Anything unlisted is a 500.
STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.INVALID_INPUT: 400,
    ErrorCode.UNSUPPORTED_SOURCE: 400,
    ErrorCode.AMBIGUOUS_SOURCE: 400,
    ErrorCode.CONTENT_TYPE_MISMATCH: 400,
    ErrorCode.AUTH_REQUIRED: 401,
    ErrorCode.AUTH_EXPIRED: 401,
    ErrorCode.PERMISSION_MISSING: 403,
    ErrorCode.POLICY_DENIED: 403,
    ErrorCode.NOT_ACCESSIBLE_WITH_AUTHORIZED_ACCOUNT: 403,
    ErrorCode.UNAVAILABLE_COPYRIGHT: 403,
    ErrorCode.UNAVAILABLE_DRM: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.NOT_IN_INDEX_WINDOW: 404,
    ErrorCode.PATH_REJECTED: 404,
    ErrorCode.QUOTA_EXCEEDED: 429,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.SIZE_LIMIT_EXCEEDED: 413,
    ErrorCode.TOOL_MISSING: 503,
    ErrorCode.UPSTREAM_ERROR: 502,
    ErrorCode.NETWORK_ERROR: 502,
    ErrorCode.LOCATOR_EXPIRED: 502,
}


def http_error(exc: MediaDLError) -> HTTPException:
    return HTTPException(
        status_code=STATUS_BY_CODE.get(exc.code, 500),
        detail={
            'code': str(exc.code),
            'message': exc.user_message,
            'retryable': exc.retryable,
            'detail': dict(exc.public().get('detail') or {}),
        },
    )


def runtime_of(request: Request) -> DashboardRuntime:
    return request.app.state.runtime  # type: ignore[no-any-return]


# --- request bodies -------------------------------------------------------
class DownloadOptions(BaseModel):
    """A small, server-revalidated preference selected in the dashboard."""

    model_config = ConfigDict(extra='forbid')

    format_id: str | None = Field(default=None, min_length=1, max_length=512)


class SubmitBody(BaseModel):
    source: str = Field(min_length=1, max_length=4096)
    connection_id: str | None = None
    options: DownloadOptions = Field(default_factory=DownloadOptions)


class InspectBody(SubmitBody):
    """A source to validate and resolve before the user starts a job."""


class ConnectStartBody(BaseModel):
    client_id: str = Field(min_length=1, max_length=256)
    redirect_uri: str = Field(min_length=1, max_length=2048)
    force_reauth: bool = False


class ConnectCompleteBody(BaseModel):
    redirect_url: str = Field(min_length=1, max_length=4096)
    client_id: str = Field(min_length=1, max_length=256)
    client_secret: str = Field(min_length=1, max_length=512)
    redirect_uri: str = Field(min_length=1, max_length=2048)


# --- environment ----------------------------------------------------------
@router.get('/state')
async def get_state(request: Request) -> dict[str, Any]:
    runtime = runtime_of(request)
    settings = runtime.settings
    tools = runtime.ctx.processor.tools
    ffmpeg: dict[str, Any] = {'available': tools is not None, 'path': None, 'license': None}
    if tools is not None:
        from mediadl.processing.license_gate import inspect

        try:
            info = await inspect(tools)
            ffmpeg.update({'path': tools.ffmpeg, 'license': info.summary, 'gpl': info.gpl,
                           'nonfree': info.nonfree})
        except MediaDLError:
            ffmpeg.update({'path': tools.ffmpeg, 'license': 'unknown'})

    return {
        'output_root': str(runtime.ctx.output.root),
        'policy': settings.output.policy,
        'keep_source': settings.output.keep_source,
        'sidecar': settings.output.sidecar,
        'jobs_database': str(settings.jobs_database()),
        'data_dir': str(settings.data_dir()),
        'ffmpeg': ffmpeg,
        'protocols': sorted(runtime.ctx.enabled_protocols),
        'connectors': [
            {
                'id': c.connector_id,
                'kinds': sorted(str(k) for k in c.capabilities.media_kinds),
                'auth_modes': sorted(c.capabilities.auth_modes),
                'needs_auth': 'none' not in c.capabilities.auth_modes,
                'supports_listing': c.capabilities.supports_listing,
                'rendition_basis': c.capabilities.rendition_basis,
            }
            for c in sorted(runtime.ctx.registry.connectors, key=lambda c: c.connector_id)
        ],
        'limits': {
            'max_asset_bytes': settings.limits.max_asset_bytes,
            'max_assets_per_collection': settings.limits.max_assets_per_collection,
            'active_jobs': settings.concurrency.active_jobs_per_user,
        },
    }


@router.get('/credentials')
async def get_credentials(request: Request) -> dict[str, Any]:
    runtime = runtime_of(request)
    connections = await _connection_dicts(runtime)
    requirements = []
    for connector in sorted(runtime.ctx.registry.connectors, key=lambda c: c.connector_id):
        if connector.connector_id == 'instagram_graph':
            requirements.append(
                guide.instagram_requirement(
                    connections=[c for c in connections if c['connector_id'] == 'instagram_graph'],
                    configured={'client_id': runtime.settings.instagram_graph.client_id},
                ).to_dict()
            )
        else:
            requirements.append(guide.no_credential_requirement(connector.connector_id).to_dict())

    tools = runtime.ctx.processor.tools
    license_summary = 'installed'
    if tools is not None:
        from mediadl.processing.license_gate import inspect

        try:
            license_summary = (await inspect(tools)).summary
        except MediaDLError:
            license_summary = 'unknown'
    requirements.append(
        guide.ffmpeg_requirement(
            available=tools is not None,
            path=tools.ffmpeg if tools else '',
            license_summary=license_summary,
        ).to_dict()
    )
    return {
        'requirements': requirements,
        'not_required': list(guide.NOT_REQUIRED),
        'connections': connections,
        'config_file_hint': 'config/mediadl.example.toml',
    }


# --- jobs -----------------------------------------------------------------
@router.post('/inspect')
async def inspect_source(request: Request, body: InspectBody) -> dict[str, Any]:
    """Resolve a source into a safe preview without creating a download job."""
    runtime = runtime_of(request)
    try:
        return await runtime.inspect(body.source, body.connection_id)
    except MediaDLError as exc:
        raise http_error(exc) from exc


@router.get('/previews/{preview_id}')
async def get_preview(request: Request, preview_id: str) -> Response:
    """Serve an inspected image without revealing its provider locator."""
    runtime = runtime_of(request)
    try:
        content, media_type = await runtime.preview(preview_id)
    except MediaDLError as exc:
        raise http_error(exc) from exc
    return Response(
        content=content,
        media_type=media_type,
        headers={'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'},
    )


@router.get('/jobs')
async def list_jobs(request: Request, limit: int = 30) -> dict[str, Any]:
    runtime = runtime_of(request)
    return {'jobs': await runtime.job_summaries(min(limit, 200))}


@router.post('/jobs', status_code=202)
async def submit_job(request: Request, body: SubmitBody) -> dict[str, Any]:
    runtime = runtime_of(request)
    try:
        job = await runtime.submit(
            body.source.strip(),
            body.connection_id,
            options=body.options.model_dump(exclude_none=True),
        )
    except MediaDLError as exc:
        raise http_error(exc) from exc
    return await runtime.job_detail(job.id)


@router.get('/jobs/{job_id}')
async def job_detail(request: Request, job_id: str) -> dict[str, Any]:
    runtime = runtime_of(request)
    try:
        return await runtime.job_detail(job_id)
    except MediaDLError as exc:
        raise http_error(exc) from exc


@router.post('/jobs/{job_id}/retry', status_code=202)
async def retry_job(request: Request, job_id: str) -> dict[str, Any]:
    runtime = runtime_of(request)
    try:
        job = await runtime.ctx.jobs.get(job_id)
        if job.state not in (JobState.FAILED, JobState.PARTIAL_SUCCESS,
                             JobState.AUTHORIZATION_REQUIRED, JobState.RATE_LIMITED,
                             JobState.PAUSED, JobState.CANCELLED):
            raise HTTPException(
                status_code=409,
                detail={'code': 'INVALID_STATE',
                        'message': f'A job in {job.state} cannot be retried.'},
            )
        requeued = await runtime.ctx.jobs.transition(job_id, JobState.QUEUED, actor='user')
        runtime.start(requeued)
        return await runtime.job_detail(job_id)
    except MediaDLError as exc:
        raise http_error(exc) from exc


@router.post('/jobs/{job_id}/cancel', status_code=202)
async def cancel_job(request: Request, job_id: str) -> dict[str, Any]:
    runtime = runtime_of(request)
    cancelled = await runtime.cancel(job_id)
    if not cancelled:
        try:
            job = await runtime.ctx.jobs.get(job_id)
            if job.state in (JobState.QUEUED, JobState.PAUSED, JobState.RATE_LIMITED,
                             JobState.AUTHORIZATION_REQUIRED):
                await runtime.ctx.jobs.transition(job_id, JobState.CANCELLED, actor='user')
        except MediaDLError as exc:
            raise http_error(exc) from exc
    return await runtime.job_detail(job_id)


@router.get('/jobs/{job_id}/outputs')
async def job_outputs(request: Request, job_id: str) -> dict[str, Any]:
    runtime = runtime_of(request)
    try:
        items = await runtime.ctx.jobs.items(job_id)
    except MediaDLError as exc:
        raise http_error(exc) from exc
    outputs = []
    for item in items:
        record = item.output_record or {}
        relative = record.get('relative_path')
        if not relative:
            continue
        absolute = runtime.ctx.output.root / relative
        mime = mimetypes.guess_type(relative)[0] or 'application/octet-stream'
        outputs.append({
            'position': item.position,
            'asset_id': item.asset_id,
            'kind': item.kind,
            'relative_path': relative,
            'sha256': record.get('sha256'),
            'selected_rep_id': record.get('selected_rep_id'),
            'mime_type': mime,
            'size_bytes': absolute.stat().st_size if absolute.exists() else None,
            'exists': absolute.exists(),
            'sidecar': f'{relative}.meta.json',
        })
    return {'outputs': outputs, 'root': str(runtime.ctx.output.root)}


@router.post('/selftest', status_code=202)
async def selftest(request: Request) -> dict[str, Any]:
    """Generate a local sample and run it through the real pipeline."""
    from mediadl.api.selftest import prepare_sample

    runtime = runtime_of(request)
    directory, created = prepare_sample(
        runtime.settings.data_dir(), runtime.ctx.processor.tools
    )
    # The sample lives outside any configured import root, so allow it
    # explicitly for this run rather than loosening the connector's policy.
    connector = runtime.ctx.registry.get('user_files')
    roots = getattr(connector, 'import_roots', ())
    if roots and not any(directory.is_relative_to(root) for root in roots):
        connector.import_roots = (*roots, directory)  # type: ignore[attr-defined]
    engine = runtime.ctx.transfers.engines.get(LocalFileLocator)
    if engine is not None and getattr(engine, 'import_roots', None):
        engine.import_roots = (*engine.import_roots, directory)  # type: ignore[attr-defined]

    try:
        job = await runtime.submit(str(directory))
    except MediaDLError as exc:
        raise http_error(exc) from exc
    return {
        'job_id': job.id,
        'sample_dir': str(directory),
        'created': created,
        'explains': 'If this finishes green, the pipeline works on this machine: '
                    'resolve, select, transfer, probe, finalize and sidecar.',
    }


@router.get('/events')
async def events(request: Request) -> StreamingResponse:
    runtime = runtime_of(request)
    return StreamingResponse(
        runtime.stream(),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'},
    )


# --- files ----------------------------------------------------------------
@router.get('/files')
async def get_file(request: Request, path: str, download: bool = False) -> FileResponse:
    runtime = runtime_of(request)
    try:
        absolute: Path = output_file(runtime, path)
    except MediaDLError as exc:
        raise http_error(exc) from exc
    if not absolute.is_file():
        raise HTTPException(status_code=404, detail={'code': 'NOT_FOUND', 'message': 'No such file.'})
    return FileResponse(
        absolute,
        filename=absolute.name if download else None,
        media_type=mimetypes.guess_type(absolute.name)[0] or 'application/octet-stream',
    )


# --- connections ----------------------------------------------------------
async def _connection_dicts(runtime: DashboardRuntime) -> list[dict[str, Any]]:
    from datetime import UTC, datetime

    connections = await runtime.connections.list('local')
    now = datetime.now(UTC)
    result = []
    for connection in connections:
        expires_in_days = None
        if connection.expires_at is not None:
            expires_in_days = round((connection.expires_at - now).total_seconds() / 86400, 1)
        result.append({
            'id': connection.id,
            'connector_id': connection.connector_id,
            'auth_mode': connection.auth_mode,
            'status': str(connection.status),
            'provider_user_id': connection.provider_user_id,
            'scopes_granted': list(connection.scopes_granted),
            'expires_at': connection.expires_at.isoformat() if connection.expires_at else None,
            'expires_in_days': expires_in_days,
            'last_refreshed_at': (
                connection.last_refreshed_at.isoformat()
                if connection.last_refreshed_at else None
            ),
            'created_at': connection.created_at,
        })
    return result


@router.get('/connections')
async def list_connections(request: Request) -> dict[str, Any]:
    return {'connections': await _connection_dicts(runtime_of(request))}


@router.post('/connections/instagram/start')
async def connect_start(request: Request, body: ConnectStartBody) -> dict[str, Any]:
    runtime = runtime_of(request)
    from mediadl.auth.oauth_instagram import OAuthConfig, authorize_url, normalize_redirect_uri

    try:
        redirect_uri = normalize_redirect_uri(body.redirect_uri)
        config = OAuthConfig(
            client_id=body.client_id,
            client_secret='',  # not needed to build the authorization URL
            redirect_uri=redirect_uri,
            scopes=tuple(runtime.settings.instagram_graph.scopes),
            auth_mode=runtime.settings.instagram_graph.auth_mode,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                'code': 'INVALID_INPUT',
                'message': str(exc),
            },
        ) from exc
    state = await runtime.connections.create_state(
        'local', 'instagram_graph', config.redirect_uri
    )
    return {
        'authorize_url': authorize_url(config, state, force_reauth=body.force_reauth),
        # Echo the validated, exact value back to the client.  Step two must
        # never invent a different redirect URI for the token exchange.
        'redirect_uri': config.redirect_uri,
        'scopes': list(config.scopes),
        'expires_in_s': 600,
        'next': 'Approve access, then paste the full URL you land on back here '
                'together with your app secret.',
    }


@router.post('/connections/instagram/complete')
async def connect_complete(request: Request, body: ConnectCompleteBody) -> dict[str, Any]:
    import urllib.parse
    from datetime import UTC, datetime

    from mediadl.auth.oauth_instagram import (
        InstagramOAuth,
        OAuthConfig,
        normalize_redirect_uri,
        validate_redirect_uri,
    )

    runtime = runtime_of(request)
    try:
        parts = urllib.parse.urlsplit(body.redirect_url.strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                'code': 'INVALID_INPUT',
                'message': 'The returned redirect URL is not valid. Paste the complete browser address.',
            },
        ) from exc
    query = urllib.parse.parse_qs(parts.query)

    state = (query.get('state') or [''])[0]
    if 'error' in query:
        description = (query.get('error_description') or query['error'])[0]
        raise HTTPException(
            status_code=400,
            detail={'code': 'AUTH_REQUIRED', 'message': f'Authorization was denied: {description}'},
        )
    code = (query.get('code') or [''])[0]
    if not code or not state:
        raise HTTPException(
            status_code=400,
            detail={'code': 'INVALID_INPUT',
                    'message': 'That URL has no ?code= and &state=. Paste the full '
                               'address bar contents from after you approved access.'},
        )

    callback_base = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
    try:
        submitted_redirect = normalize_redirect_uri(body.redirect_uri)
        # Reject a changed Step 1 value before consuming the state.  This is
        # the actionable version of Meta's otherwise opaque invalid
        # redirect_uri response, and lets the user correct a typo without
        # restarting the whole authorization.
        validate_redirect_uri(callback_base, submitted_redirect)
    except (ValueError, MediaDLError) as exc:
        raise HTTPException(
            status_code=400,
            detail={
                'code': 'INVALID_INPUT',
                'message': (
                    'The returned URL and Redirect URI do not match. Use the exact URI '
                    'from step 1, including any trailing slash.'
                ),
            },
        ) from exc

    try:
        principal_id, expected_redirect = await runtime.connections.consume_state(state)
        # The expected URI is read only: the token request must use the same
        # string Meta saw in the authorization request, rather than a value
        # that can be edited in the second form.
        validate_redirect_uri(callback_base, expected_redirect)
        config = OAuthConfig(
            client_id=body.client_id,
            client_secret=body.client_secret.strip(),
            redirect_uri=expected_redirect,
            scopes=tuple(runtime.settings.instagram_graph.scopes),
            auth_mode=runtime.settings.instagram_graph.auth_mode,
        )
        oauth = InstagramOAuth(runtime.ctx.http, config)
        short_lived = await oauth.exchange_code(code)
        long_lived = await oauth.exchange_long_lived(short_lived.access_token)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={'code': 'INVALID_INPUT', 'message': str(exc)},
        ) from exc
    except MediaDLError as exc:
        raise http_error(exc) from exc

    connection = Connection(
        id=new_connection_id(),
        principal_id=principal_id,
        connector_id='instagram_graph',
        auth_mode=config.auth_mode,
        status=ConnectionStatus.ACTIVE,
        provider_user_id=short_lived.provider_user_id,
        scopes_granted=short_lived.permissions or config.scopes,
        expires_at=long_lived.expires_at,
        last_refreshed_at=datetime.now(UTC),
    )
    saved = await runtime.connections.save(connection, long_lived.access_token)
    runtime.bus.publish({'type': 'connection.changed', 'connection_id': saved.id})
    return {
        'connection': {
            'id': saved.id,
            'provider_user_id': saved.provider_user_id,
            'scopes_granted': list(saved.scopes_granted),
            'expires_at': saved.expires_at.isoformat() if saved.expires_at else None,
        },
        'note': 'The app secret was used once and was not stored. Refreshing this '
                'token later needs only the token itself.',
    }


@router.delete('/connections/{connection_id}', status_code=200)
async def disconnect(request: Request, connection_id: str) -> dict[str, Any]:
    runtime = runtime_of(request)
    try:
        await runtime.credentials.disconnect('local', connection_id)
    except MediaDLError as exc:
        raise http_error(exc) from exc
    runtime.bus.publish({'type': 'connection.changed', 'connection_id': connection_id})
    return {
        'disconnected': connection_id,
        'note': 'The encrypted token row was deleted. You can also remove this app '
                'under Instagram → Settings → Apps and websites to revoke it upstream.',
    }
