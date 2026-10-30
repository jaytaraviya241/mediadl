"""Loader for the sanitized Graph API fixtures."""
from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).parent / 'fixtures'

#: Not a real credential: a placeholder used to assert it never leaks.
TOKEN = 'IGAA-SANITIZED-TEST-TOKEN-0123456789'


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f'{name}.json').read_text())
