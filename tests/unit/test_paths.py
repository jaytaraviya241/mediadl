"""Filesystem safety (spec sections 6.7, 7.9)."""
from __future__ import annotations

import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mediadl.errors import PathRejectedError
from mediadl.policy.paths import (
    MAX_COMPONENT_BYTES,
    enforce_under_root,
    open_exclusive,
    sanitize_component,
    validate_extension,
)


@pytest.mark.parametrize(
    ('raw', 'forbidden'),
    [
        ('../../etc/passwd', '/'),
        ('..\\..\\windows', '\\'),
        ('a/b', '/'),
        ('name:stream', ':'),
        ('a\x00b', '\x00'),
        ('tab\tname', '\t'),
        ('star*name', '*'),
        ('pipe|name', '|'),
        ('quote"name', '"'),
    ],
)
def test_illegal_characters_never_survive(raw, forbidden):
    assert forbidden not in sanitize_component(raw)


@pytest.mark.parametrize('reserved', ['CON', 'PRN', 'AUX', 'NUL', 'COM1', 'LPT9', 'con'])
def test_windows_reserved_names_are_suffixed(reserved):
    assert sanitize_component(reserved) != reserved.upper()
    assert sanitize_component(reserved).endswith('_')


def test_leading_dots_and_trailing_spaces_are_stripped():
    assert not sanitize_component('  .hidden  ').startswith('.')
    assert not sanitize_component('name.  ').endswith(' ')


def test_empty_input_falls_back():
    assert sanitize_component('') == '_'
    assert sanitize_component('...') == '_'


def test_components_are_capped_in_bytes_not_characters():
    long_unicode = 'é' * 200  # 2 bytes each
    result = sanitize_component(long_unicode)
    assert len(result.encode('utf-8')) <= MAX_COMPONENT_BYTES


@settings(max_examples=200, deadline=None)
@given(st.text())
def test_sanitized_component_is_always_a_safe_single_segment(raw):
    result = sanitize_component(raw)
    assert result
    assert '/' not in result and '\\' not in result
    assert '\x00' not in result
    assert result not in ('.', '..')
    assert len(result.encode('utf-8')) <= MAX_COMPONENT_BYTES


def test_extension_allowlist(tmp_path):
    assert validate_extension('JPG', 'image') == 'jpg'
    with pytest.raises(PathRejectedError):
        validate_extension('exe', 'image')
    with pytest.raises(PathRejectedError):
        validate_extension('sh', None)
    with pytest.raises(PathRejectedError):
        validate_extension('mp4', 'image')


def test_enforce_under_root_blocks_escapes(tmp_path):
    root = tmp_path / 'out'
    root.mkdir()
    assert enforce_under_root('a/b.jpg', root) == (root / 'a/b.jpg').resolve()
    for escape in ('../outside.jpg', '../../etc/passwd', '/etc/passwd'):
        with pytest.raises(PathRejectedError):
            enforce_under_root(escape, root)


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX symlink semantics')
def test_symlinked_parent_is_rejected(tmp_path):
    root = tmp_path / 'out'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (root / 'link').symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathRejectedError):
        enforce_under_root('link/file.jpg', root)


def test_open_exclusive_refuses_existing_file(tmp_path):
    import os

    target = tmp_path / 'f.bin'
    fd = open_exclusive(target)
    os.close(fd)
    with pytest.raises(FileExistsError):
        open_exclusive(target)
