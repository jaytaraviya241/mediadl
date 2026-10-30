"""Command-line interface (spec section 11).

The CLI parses intent and renders results. All behaviour lives in the core,
so the same operations are available from the library facade.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from mediadl.config import Settings
from mediadl.obs.logging import configure, get_logger

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help='Download media you are authorized to access, preserving the source bytes.',
)
connect_app = typer.Typer(no_args_is_help=True, help='Connect and disconnect accounts.')
jobs_app = typer.Typer(no_args_is_help=True, help='Inspect and control jobs.')
instagram_app = typer.Typer(no_args_is_help=True, help='Instagram-specific commands.')
oauth_app = typer.Typer(no_args_is_help=True, help='Run narrowly scoped OAuth helpers.')
app.add_typer(connect_app, name='connect')
app.add_typer(jobs_app, name='jobs')
app.add_typer(instagram_app, name='instagram')
app.add_typer(oauth_app, name='oauth')

log = get_logger(__name__)

VERSION = '0.1.0'


def _load_settings(config: Path | None, **overrides) -> Settings:
    settings = Settings.load(config) if config else Settings()
    output = overrides.pop('output', None)
    policy = overrides.pop('policy', None)
    zip_output = overrides.pop('zip_output', None)
    if output:
        settings.output.root = str(output)
    if policy:
        settings.output.policy = policy  # type: ignore[assignment]
    if zip_output is not None:
        settings.output.zip = zip_output
    return settings


def _echo_error(exc: Exception) -> int:
    from mediadl.errors import MediaDLError

    if isinstance(exc, MediaDLError):
        typer.secho(f'{exc.code}: {exc.user_message}', fg=typer.colors.RED, err=True)
        detail = dict(exc.public().get('detail') or {})
        if detail:
            typer.secho(f'  detail: {json.dumps(detail, default=str)}', fg=typer.colors.YELLOW, err=True)
        return 2
    typer.secho(f'unexpected error: {exc}', fg=typer.colors.RED, err=True)
    return 1


@app.callback()
def main_callback(
    verbose: Annotated[bool, typer.Option('--verbose', '-v', help='Debug logging.')] = False,
    json_logs: Annotated[bool, typer.Option('--json-logs', help='Structured JSON logs.')] = False,
) -> None:
    configure('DEBUG' if verbose else 'WARNING', json_output=json_logs)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(f'mediadl {VERSION}')


@app.command()
def get(
    source: Annotated[str, typer.Argument(help='URL, local path, or ig-media:<id>.')],
    out: Annotated[Path | None, typer.Option('--out', '-o', help='Output directory.')] = None,
    policy: Annotated[
        str | None, typer.Option('--policy', help='source|compatible|efficient')
    ] = None,
    connection: Annotated[
        str | None, typer.Option('--connection', help='Which connected account to use.')
    ] = None,
    zip_output: Annotated[bool, typer.Option('--zip', help='Also write a ZIP package.')] = False,
    config: Annotated[Path | None, typer.Option('--config', help='TOML config file.')] = None,
) -> None:
    """Download one item or collection."""
    policy_name = {
        'source': 'source_preserving',
        'compatible': 'broadly_compatible',
        'efficient': 'storage_efficient',
    }.get(policy or '', policy)
    settings = _load_settings(config, output=out, policy=policy_name, zip_output=zip_output)
    raise SystemExit(asyncio.run(_run_get(settings, source, connection)))


async def _run_get(settings: Settings, source: str, connection: str | None) -> int:
    from mediadl.app.context import build
    from mediadl.app.service import MediaDLService
    from mediadl.jobs.states import JobState
    from mediadl.transfer.base import ProgressReporter

    ctx = build(settings)
    service = MediaDLService(ctx, principal_id='local', credentials=_credentials(ctx))
    last: dict[str, int] = {}

    def on_progress(asset_id: str, done: int, total: int | None) -> None:
        if not sys.stderr.isatty():
            return
        last[asset_id] = done
        share = f'{done * 100 // total}%' if total else f'{done / 1_048_576:.1f} MiB'
        print(f'\r  {asset_id[:40]:<40} {share:>8}', end='', file=sys.stderr, flush=True)

    try:
        outcome = await service.get(
            source, connection_id=connection, progress=ProgressReporter(callback=on_progress)
        )
    except Exception as exc:
        await ctx.aclose()
        return _echo_error(exc)

    if sys.stderr.isatty():
        print('\r' + ' ' * 60 + '\r', end='', file=sys.stderr)

    for result in outcome.results:
        if result.ok and result.record is not None:
            typer.secho(f'  [{result.position}] {result.record.relative_path}', fg=typer.colors.GREEN)
        else:
            typer.secho(
                f'  [{result.position}] {result.status}: {result.error_code or ""} '
                f'{result.error_message or ""}'.rstrip(),
                fg=typer.colors.YELLOW if result.status == 'unavailable' else typer.colors.RED,
            )
    typer.echo(f'{outcome.state} -> {ctx.output.root}')
    if outcome.manifest_path:
        typer.echo(f'manifest: {outcome.manifest_path}')
    await ctx.aclose()
    return {
        JobState.COMPLETED: 0,
        JobState.PARTIAL_SUCCESS: 3,
        JobState.AUTHORIZATION_REQUIRED: 4,
        JobState.RATE_LIMITED: 5,
    }.get(outcome.state, 1)


@app.command()
def formats(
    source: Annotated[str, typer.Argument(help='URL, local path, or ig-media:<id>.')],
    connection: Annotated[str | None, typer.Option('--connection')] = None,
    config: Annotated[Path | None, typer.Option('--config')] = None,
) -> None:
    """List the representations of an item and which one would be chosen."""
    settings = _load_settings(config)
    raise SystemExit(asyncio.run(_run_formats(settings, source, connection)))


async def _run_formats(settings: Settings, source: str, connection: str | None) -> int:
    import time

    from mediadl.app.context import build
    from mediadl.domain.invariants import as_collection, validate
    from mediadl.selection import choose
    from mediadl.sources.base import AccessContext, SourceRequest

    ctx = build(settings)
    try:
        request = SourceRequest(raw=source, connection_id=connection)
        connector = ctx.registry.select(request)
        credential = None
        if 'none' not in connector.capabilities.auth_modes:
            provider = _credentials(ctx)
            if provider is None:
                raise typer.Exit(4)
            credential = await provider.acquire(
                'local', connection, connector.required_scopes(request)
            )
        access = AccessContext('local', connection, credential, ctx.http, time.monotonic() + 60)
        collection = as_collection(validate(await connector.resolve(request, access)))

        for asset in collection.assets:
            typer.secho(
                f'[{asset.position}] {asset.kind} {asset.asset_id} ({asset.availability})',
                bold=True,
            )
            for rep in asset.representations:
                typer.echo(
                    f'    {rep.rep_id:<12} {rep.role:<8} {rep.protocol:<6} '
                    f'{rep.label or "-":<24} drm={rep.drm}'
                )
            try:
                selected = choose(
                    asset, ctx.selection_policy, enabled_protocols=ctx.enabled_protocols
                )
                typer.secho(f'    -> {"; ".join(selected.reasons)}', fg=typer.colors.GREEN)
            except Exception as exc:
                typer.secho(f'    -> not selectable: {exc}', fg=typer.colors.YELLOW)
        return 0
    except Exception as exc:
        return _echo_error(exc)
    finally:
        await ctx.aclose()


@jobs_app.command('list')
def jobs_list(
    limit: Annotated[int, typer.Option('--limit')] = 20,
    config: Annotated[Path | None, typer.Option('--config')] = None,
) -> None:
    """List recent jobs."""
    settings = _load_settings(config)

    async def run() -> int:
        from mediadl.app.context import build

        ctx = build(settings)
        try:
            for job in await ctx.jobs.list_jobs('local', limit):
                typer.echo(
                    f'{job.id[:8]}  {job.state!s:<22} {job.connector_id or "-":<16} '
                    f'{job.request.get("raw", "")[:48]}'
                )
            return 0
        finally:
            await ctx.aclose()

    raise SystemExit(asyncio.run(run()))


@jobs_app.command('show')
def jobs_show(
    job_id: str,
    config: Annotated[Path | None, typer.Option('--config')] = None,
) -> None:
    """Show a job, its items, and its event history."""
    settings = _load_settings(config)

    async def run() -> int:
        from mediadl.app.context import build

        ctx = build(settings)
        try:
            job = await ctx.jobs.get(job_id)
            typer.secho(f'{job.id}  {job.state}', bold=True)
            typer.echo(f'  request: {job.request.get("raw")}')
            typer.echo(f'  policy:  {job.policy_name}')
            if job.error_code:
                typer.secho(f'  error:   {job.error_code}', fg=typer.colors.RED)
            for item in await ctx.jobs.items(job_id):
                line = f'  [{item.position}] {item.state} {item.asset_id}'
                if item.error_code:
                    line += f' ({item.error_code})'
                typer.echo(line)
            for event in await ctx.jobs.events(job_id):
                typer.echo(f'  {event["at"]}  {event["from_state"]} -> {event["to_state"]}')
            return 0
        except Exception as exc:
            return _echo_error(exc)
        finally:
            await ctx.aclose()

    raise SystemExit(asyncio.run(run()))


@jobs_app.command('retry')
def jobs_retry(
    job_id: str,
    config: Annotated[Path | None, typer.Option('--config')] = None,
) -> None:
    """Re-queue a failed or partially successful job."""
    settings = _load_settings(config)

    async def run() -> int:
        from mediadl.app.context import build
        from mediadl.app.service import MediaDLService

        ctx = build(settings)
        service = MediaDLService(ctx, principal_id='local', credentials=_credentials(ctx))
        try:
            outcome = await service.retry_failed_items(job_id)
            typer.echo(f'{outcome.state}')
            return 0 if outcome.state.name == 'COMPLETED' else 3
        except Exception as exc:
            return _echo_error(exc)
        finally:
            await ctx.aclose()

    raise SystemExit(asyncio.run(run()))


@jobs_app.command('cancel')
def jobs_cancel(
    job_id: str,
    config: Annotated[Path | None, typer.Option('--config')] = None,
) -> None:
    """Cancel a queued or running job."""
    settings = _load_settings(config)

    async def run() -> int:
        from mediadl.app.context import build
        from mediadl.jobs.states import JobState

        ctx = build(settings)
        try:
            await ctx.jobs.transition(job_id, JobState.CANCELLED, actor='user')
            typer.echo('cancelled')
            return 0
        except Exception as exc:
            return _echo_error(exc)
        finally:
            await ctx.aclose()

    raise SystemExit(asyncio.run(run()))


@app.command()
def serve(
    host: Annotated[str, typer.Option('--host', help='Bind address.')] = '127.0.0.1',
    port: Annotated[int, typer.Option('--port', help='Port to listen on.')] = 8765,
    open_browser: Annotated[bool, typer.Option('--open/--no-open')] = True,
    config: Annotated[Path | None, typer.Option('--config')] = None,
) -> None:
    """Run the dashboard: a local control panel over this same job store."""
    settings = _load_settings(config)
    try:
        import uvicorn

        from mediadl.api import create_app
    except ImportError:
        typer.secho(
            'The dashboard needs the "server" extra: pip install -e ".[server]"',
            fg=typer.colors.RED, err=True,
        )
        raise SystemExit(2) from None

    if host not in ('127.0.0.1', 'localhost', '::1'):
        # The dashboard has no authentication; it assumes a single local user.
        typer.secho(
            f'Refusing to bind {host}: the dashboard has no authentication and is '
            'meant for loopback only.',
            fg=typer.colors.RED, err=True,
        )
        raise SystemExit(2)

    url = f'http://{host}:{port}'
    from mediadl.processing.ffmpeg_runner import FfmpegTools

    typer.echo()
    typer.secho(f'  mediadl dashboard   {url}', fg=typer.colors.GREEN, bold=True)
    typer.echo('  Open that address in your browser.')
    typer.echo()
    typer.secho('  First time here?', bold=True)
    typer.echo('    The "Start here" tab explains what works without any setup, and the')
    typer.echo('    "Run a self-test" button proves the install by downloading a sample')
    typer.echo('    it generates locally. No account or internet needed.')
    typer.echo()
    typer.echo(f'  Saving to    {settings.output.resolved_root()}')
    typer.echo(f'  Job store    {settings.jobs_database()}')
    if FfmpegTools.try_discover(settings.ffmpeg.path or None) is None:
        typer.secho(
            '  FFmpeg       not found - single files still download, but merging,\n'
            '               stream assembly and probing are unavailable.\n'
            '               Install it (macOS: brew install ffmpeg) and restart.',
            fg=typer.colors.YELLOW,
        )
    typer.echo()
    typer.echo('  Ctrl+C to stop')
    typer.echo()
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        create_app(settings), host=host, port=port,
        log_level=settings.logging.level.lower(), access_log=False,
    )


@oauth_app.command('callback')
def oauth_callback(
    host: Annotated[
        str,
        typer.Option('--host', help='Loopback address only; public interfaces are refused.'),
    ] = '127.0.0.1',
    port: Annotated[int, typer.Option('--port', help='Local callback port.')] = 8766,
) -> None:
    """Run an isolated receiver for an Instagram OAuth callback.

    This is deliberately separate from ``mediadl serve``.  If a temporary
    HTTPS tunnel is needed, point it at this port only, never at the dashboard.
    """
    from mediadl.auth.callback_receiver import (
        CALLBACK_PATH,
        LoopbackOnlyError,
        OAuthCallbackReceiver,
    )

    try:
        server = OAuthCallbackReceiver(host, port)
    except (LoopbackOnlyError, OSError) as exc:
        typer.secho(f'Cannot start OAuth callback receiver: {exc}', fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from None

    bound_host, bound_port = server.server_address[:2]
    display_host = f'[{bound_host}]' if ':' in str(bound_host) else str(bound_host)
    local_url = f'http://{display_host}:{bound_port}{CALLBACK_PATH}'
    typer.echo()
    typer.secho('  mediadl OAuth callback receiver', fg=typer.colors.GREEN, bold=True)
    typer.echo(f'  Listening on  {local_url}')
    typer.echo()
    typer.echo('  This service accepts only GET /oauth/instagram/callback on loopback.')
    typer.echo('  It does not expose the dashboard or store authorization codes or state.')
    typer.echo('  If you use an HTTPS tunnel, tunnel this listener only, not port 8765.')
    typer.echo('  After Meta redirects here, copy the complete browser URL into mediadl.')
    typer.echo()
    typer.echo('  Ctrl+C to stop')
    typer.echo()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


@app.command()
def doctor(
    config: Annotated[Path | None, typer.Option('--config')] = None,
) -> None:
    """Report the environment: connectors, FFmpeg build, output root."""
    settings = _load_settings(config)

    async def run() -> int:
        from mediadl.app.context import build
        from mediadl.processing.ffmpeg_runner import FfmpegTools
        from mediadl.processing.license_gate import inspect

        ctx = build(settings)
        try:
            typer.secho('connectors', bold=True)
            for connector in ctx.registry.connectors:
                caps = connector.capabilities
                typer.echo(
                    f'  {connector.connector_id:<18} '
                    f'kinds={",".join(sorted(str(k) for k in caps.media_kinds))} '
                    f'auth={",".join(sorted(caps.auth_modes))}'
                )
            typer.secho('ffmpeg', bold=True)
            tools = FfmpegTools.try_discover(settings.ffmpeg.path or None)
            if tools is None:
                typer.secho('  not found: merge, remux, transcode and probing are unavailable',
                            fg=typer.colors.YELLOW)
            else:
                info = await inspect(tools)
                colour = typer.colors.RED if info.nonfree else typer.colors.GREEN
                typer.echo(f'  path:    {tools.ffmpeg}')
                typer.secho(f'  license: {info.summary}', fg=colour)
                if info.gpl:
                    typer.secho(
                        '  note:    a GPL build cannot be redistributed under LGPL terms; '
                        'ship an LGPL build if you bundle FFmpeg.',
                        fg=typer.colors.YELLOW,
                    )
            typer.secho('output', bold=True)
            typer.echo(f'  root:    {ctx.output.root}')
            typer.echo(f'  policy:  {settings.output.policy}')
            typer.echo(f'  jobs db: {settings.jobs_database()}')
            return 0
        finally:
            await ctx.aclose()

    raise SystemExit(asyncio.run(run()))


def _credentials(ctx):
    """Build a credential provider if an auth store exists for this install."""
    from mediadl.auth.connections import ConnectionStore
    from mediadl.auth.credentials import CredentialProvider
    from mediadl.auth.vault import KeyProvider, TokenVault

    data_dir = ctx.settings.data_dir()
    store = ConnectionStore(data_dir / 'connections.sqlite3', TokenVault(KeyProvider(data_dir)))
    return CredentialProvider(store)


from mediadl.cli.auth_commands import register as _register_auth  # noqa: E402

_register_auth(connect_app, instagram_app, _load_settings, _echo_error)


def main() -> None:
    app()


if __name__ == '__main__':  # pragma: no cover
    main()
