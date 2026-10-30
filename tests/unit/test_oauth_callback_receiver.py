"""Tests for the isolated, non-persistent OAuth callback listener."""
from __future__ import annotations

import http.client
import threading
from collections.abc import Iterator

import pytest
from typer.testing import CliRunner

from mediadl.auth.callback_receiver import CALLBACK_PATH, LoopbackOnlyError, OAuthCallbackReceiver
from mediadl.cli import app


@pytest.fixture
def callback_receiver() -> Iterator[OAuthCallbackReceiver]:
    server = OAuthCallbackReceiver('127.0.0.1', 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _request(server: OAuthCallbackReceiver, method: str, path: str) -> tuple[int, dict[str, str], str]:
    host, port = server.server_address[:2]
    connection = http.client.HTTPConnection(host, port, timeout=2)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode('utf-8')
    finally:
        connection.close()


def test_callback_page_has_no_reflected_authorization_values(callback_receiver):
    code = 'one-time-code-must-not-be-reflected'
    state = 'csrf-state-must-not-be-reflected'
    status, headers, body = _request(
        callback_receiver, 'GET', f'{CALLBACK_PATH}?code={code}&state={state}'
    )

    assert status == 200
    assert code not in body
    assert state not in body
    assert headers['Cache-Control'] == 'no-store'
    assert headers['Pragma'] == 'no-cache'
    assert headers['Referrer-Policy'] == 'no-referrer'
    assert headers['X-Content-Type-Options'] == 'nosniff'
    assert headers['X-Frame-Options'] == 'DENY'
    assert "default-src 'none'" in headers['Content-Security-Policy']


def test_receiver_exposes_only_the_callback_get_route(callback_receiver):
    code = 'not-in-response'
    not_found_status, _, not_found_body = _request(callback_receiver, 'GET', f'/?code={code}')
    rejected_status, rejected_headers, rejected_body = _request(
        callback_receiver, 'POST', f'{CALLBACK_PATH}?code={code}'
    )

    assert not_found_status == 404
    assert rejected_status == 405
    assert rejected_headers['Allow'] == 'GET'
    assert code not in not_found_body
    assert code not in rejected_body


@pytest.mark.parametrize('host', ['0.0.0.0', '::'])  # noqa: S104 - must be rejected before bind
def test_receiver_refuses_non_loopback_binds(host):
    with pytest.raises(LoopbackOnlyError, match='loopback'):
        OAuthCallbackReceiver(host, 0)


def test_callback_cli_refuses_a_public_bind():
    result = CliRunner().invoke(
        app, ['oauth', 'callback', '--host', '0.0.0.0']  # noqa: S104 - must be rejected
    )

    assert result.exit_code == 2
    assert 'Cannot start OAuth callback receiver' in result.output
    assert 'loopback' in result.output
