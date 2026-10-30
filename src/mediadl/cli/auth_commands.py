"""Account connect/disconnect commands (spec sections 6.2, 6.3).

Local single-user mode: the operator registers their own Meta app and the
app secret stays on their machine. The secret is needed only to exchange the
code and to obtain the 60-day token; refreshing afterwards uses the access
token alone, so the secret never has to be stored.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer


def register(connect_app, instagram_app, load_settings, echo_error) -> None:
    @connect_app.command('instagram')
    def connect_instagram(
        client_id: Annotated[str, typer.Option('--client-id', help='Instagram App ID.')],
        redirect_uri: Annotated[
            str, typer.Option('--redirect-uri', help='HTTPS redirect URI registered with the app.')
        ],
        client_secret: Annotated[
            str | None,
            typer.Option(
                '--client-secret',
                help='Instagram App Secret. Prompted if omitted; never written to disk.',
            ),
        ] = None,
        auth_mode: Annotated[str, typer.Option('--auth-mode')] = 'instagram_login',
        force_reauth: Annotated[bool, typer.Option('--force-reauth')] = False,
        config: Annotated[Path | None, typer.Option('--config')] = None,
    ) -> None:
        """Connect an Instagram professional account via Business Login."""
        secret = client_secret or typer.prompt('Instagram App Secret', hide_input=True)
        settings = load_settings(config)
        raise SystemExit(
            asyncio.run(
                _connect(settings, client_id, secret, redirect_uri, auth_mode, force_reauth, echo_error)
            )
        )

    @connect_app.command('list')
    def list_connections(
        config: Annotated[Path | None, typer.Option('--config')] = None,
    ) -> None:
        """List connected accounts."""
        settings = load_settings(config)

        async def run() -> int:
            store = _store(settings)
            try:
                connections = await store.list('local')
                if not connections:
                    typer.echo('no connected accounts')
                for connection in connections:
                    expiry = connection.expires_at.date() if connection.expires_at else 'n/a'
                    typer.echo(
                        f'{connection.id[:8]}  {connection.connector_id:<18} '
                        f'{connection.status:<8} user={connection.provider_user_id or "?"} '
                        f'expires={expiry}'
                    )
                return 0
            finally:
                store.close()

        raise SystemExit(asyncio.run(run()))

    @connect_app.command('disconnect')
    def disconnect(
        connection_id: str,
        config: Annotated[Path | None, typer.Option('--config')] = None,
    ) -> None:
        """Delete a connection and its stored token."""
        settings = load_settings(config)

        async def run() -> int:
            from mediadl.auth.credentials import CredentialProvider

            store = _store(settings)
            try:
                await CredentialProvider(store).disconnect('local', connection_id)
                typer.echo('disconnected; the token ciphertext was deleted')
                typer.echo(
                    'You can also remove this app from Instagram settings to revoke it upstream.'
                )
                return 0
            finally:
                store.close()

        raise SystemExit(asyncio.run(run()))

    @instagram_app.command('list')
    def instagram_list(
        connection: Annotated[str | None, typer.Option('--connection')] = None,
        limit: Annotated[int, typer.Option('--limit')] = 25,
        config: Annotated[Path | None, typer.Option('--config')] = None,
    ) -> None:
        """List recent media on the connected professional account."""
        settings = load_settings(config)

        async def run() -> int:
            import time

            from mediadl.app.context import build
            from mediadl.auth.credentials import CredentialProvider
            from mediadl.connectors.instagram_graph import InstagramGraphConnector
            from mediadl.sources.base import AccessContext

            ctx = build(settings)
            store = _store(settings)
            try:
                provider = CredentialProvider(store)
                credential = await provider.acquire(
                    'local', connection, frozenset({'instagram_business_basic'})
                )
                connector = InstagramGraphConnector(
                    api_version=settings.instagram_graph.api_version,
                    auth_mode=settings.instagram_graph.auth_mode,
                )
                access = AccessContext(
                    'local', credential.connection_id, credential, ctx.http, time.monotonic() + 60
                )
                page = await connector.list_media(access)
                for asset in page.items[:limit]:
                    fields = asset.metadata.fields
                    typer.echo(
                        f'{fields.get("shortcode", "?"):<14} {asset.kind:<6} '
                        f'{fields.get("media_product_type", "-"):<6} '
                        f'{fields.get("timestamp", "")[:10]}  {fields.get("permalink", "")}'
                    )
                return 0
            except Exception as exc:
                return echo_error(exc)
            finally:
                store.close()
                await ctx.aclose()

        raise SystemExit(asyncio.run(run()))


def _store(settings):
    from mediadl.auth.connections import ConnectionStore
    from mediadl.auth.vault import KeyProvider, TokenVault

    data_dir = settings.data_dir()
    return ConnectionStore(data_dir / 'connections.sqlite3', TokenVault(KeyProvider(data_dir)))


async def _connect(
    settings, client_id, client_secret, redirect_uri, auth_mode, force_reauth, echo_error
) -> int:
    from mediadl.auth.connections import Connection, ConnectionStatus, new_connection_id
    from mediadl.auth.oauth_instagram import (
        InstagramOAuth,
        OAuthConfig,
        authorize_url,
        validate_redirect_uri,
    )
    from mediadl.config import NetworkConfig
    from mediadl.net.guarded_client import GuardedHttpClient

    if auth_mode != 'instagram_login':
        typer.secho(
            'facebook_login is not implemented. Use --auth-mode instagram_login with an '
            'Instagram Login App ID.',
            fg=typer.colors.RED,
            err=True,
        )
        return 2

    store = _store(settings)
    http = GuardedHttpClient(NetworkConfig())
    try:
        config = OAuthConfig(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=tuple(settings.instagram_graph.scopes),
            auth_mode=auth_mode,
        )
        state = await store.create_state('local', 'instagram_graph', redirect_uri)
        typer.secho('1. Open this URL and approve access:', bold=True)
        typer.echo(authorize_url(config, state, force_reauth=force_reauth))
        typer.secho('\n2. Paste the full URL you were redirected to:', bold=True)
        redirected = typer.prompt('redirect URL')

        import urllib.parse

        parts = urllib.parse.urlsplit(redirected.strip())
        query = urllib.parse.parse_qs(parts.query)
        if 'error' in query:
            typer.secho(
                f'authorization denied: {query.get("error_description", query["error"])[0]}',
                fg=typer.colors.RED,
            )
            return 2
        returned_state = (query.get('state') or [''])[0]
        code = (query.get('code') or [''])[0]
        if not code:
            typer.secho('no authorization code in that URL', fg=typer.colors.RED)
            return 2

        principal_id, expected_redirect = await store.consume_state(returned_state)
        validate_redirect_uri(
            urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, '', '')),
            expected_redirect,
        )

        oauth = InstagramOAuth(http, config)
        short_lived = await oauth.exchange_code(code)
        long_lived = await oauth.exchange_long_lived(short_lived.access_token)

        connection = Connection(
            id=new_connection_id(),
            principal_id=principal_id,
            connector_id='instagram_graph',
            auth_mode=auth_mode,
            status=ConnectionStatus.ACTIVE,
            provider_user_id=short_lived.provider_user_id,
            scopes_granted=short_lived.permissions or tuple(settings.instagram_graph.scopes),
            expires_at=long_lived.expires_at,
            last_refreshed_at=datetime.now(UTC),
        )
        saved = await store.save(connection, long_lived.access_token)
        typer.secho(f'\nconnected: {saved.id}', fg=typer.colors.GREEN)
        typer.echo(f'  account:  {saved.provider_user_id}')
        typer.echo(f'  scopes:   {", ".join(saved.scopes_granted)}')
        typer.echo(f'  expires:  {saved.expires_at.isoformat() if saved.expires_at else "n/a"}')
        typer.echo(
            '  The app secret was not stored; refreshing the token does not need it.'
        )
        return 0
    except Exception as exc:
        return echo_error(exc)
    finally:
        store.close()
        await http.aclose()
