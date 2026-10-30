"""SSRF address policy (spec sections 6.7, 9.4)."""
from __future__ import annotations

import pytest

from mediadl.policy.url_guard import UrlPolicyError, host_allowed, is_public_ip, vet_url


def resolver(table):
    def _resolve(host, port):
        return table.get(host, set())

    return _resolve


PUBLIC = resolver({'example.com': {'93.184.216.34'}})

DENIED = [
    'https://127.0.0.1/a',
    'https://[::1]/a',
    'https://[::ffff:127.0.0.1]/a',
    'https://169.254.169.254/latest/meta-data/',
    'https://10.0.0.5/a',
    'https://172.16.0.1/a',
    'https://192.168.1.1/a',
    'https://[fd00:ec2::254]/a',
    'https://[fc00::1]/a',
    'https://[fe80::1]/a',
    'https://2130706433/a',
    'https://0x7f.1/a',
    'https://017700000001/a',
    'https://0.0.0.0/a',
    'https://100.64.1.1/a',
    'https://[64:ff9b::7f00:1]/a',
    'https://[2002:7f00:1::]/a',
    'https://192.0.0.1/a',
    'https://198.18.0.1/a',
    'https://224.0.0.1/a',
]


@pytest.mark.parametrize('url', DENIED)
def test_private_and_special_addresses_are_denied(url):
    with pytest.raises(UrlPolicyError):
        vet_url(url, resolver=PUBLIC)


def test_public_host_is_allowed_and_pinned():
    assert vet_url('https://example.com/a', resolver=PUBLIC) == ['93.184.216.34']


def test_http_scheme_is_denied_by_default():
    with pytest.raises(UrlPolicyError, match='scheme'):
        vet_url('http://example.com/a', resolver=PUBLIC)


def test_userinfo_is_denied():
    with pytest.raises(UrlPolicyError, match='userinfo'):
        vet_url('https://user:pw@example.com/a', resolver=PUBLIC)


def test_nonstandard_port_is_denied():
    with pytest.raises(UrlPolicyError, match='port'):
        vet_url('https://example.com:8443/a', resolver=PUBLIC)


def test_any_private_answer_poisons_the_whole_host():
    mixed = resolver({'rebind.test': {'93.184.216.34', '10.0.0.5'}})
    with pytest.raises(UrlPolicyError, match='non-public'):
        vet_url('https://rebind.test/a', resolver=mixed)


def test_dns_failure_is_a_policy_error():
    with pytest.raises(UrlPolicyError, match=r'no addresses|DNS'):
        vet_url('https://nonexistent.test/a', resolver=resolver({}))


def test_host_allowlist_requires_a_dot_boundary():
    assert host_allowed('scontent-lhr8-1.cdninstagram.com', ['cdninstagram.com'])
    assert host_allowed('cdninstagram.com', ['cdninstagram.com'])
    assert not host_allowed('evilcdninstagram.com', ['cdninstagram.com'])
    assert not host_allowed('cdninstagram.com.evil.test', ['cdninstagram.com'])


def test_allowlist_is_enforced_by_vet_url():
    table = resolver({'evil.test': {'93.184.216.34'}})
    with pytest.raises(UrlPolicyError, match='allowlist'):
        vet_url('https://evil.test/a', resolver=table, allowed_hosts=['cdninstagram.com'])


@pytest.mark.parametrize(
    'address', ['8.8.8.8', '1.1.1.1', '93.184.216.34', '2606:4700:4700::1111']
)
def test_public_addresses_pass(address):
    assert is_public_ip(address)
