"""Sidecar state for resumable transfers (spec section 7.6).

``hashlib`` state cannot be serialized, so a resume re-hashes the existing
prefix from disk. That is cheap next to re-downloading it.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from mediadl.transfer.base import Validators


@dataclass(frozen=True, slots=True)
class ResumeState:
    url_fingerprint: str
    etag: str | None
    last_modified: str | None
    total_length: int | None
    bytes_written: int
    created_at: str

    @property
    def validators(self) -> Validators:
        return Validators(self.etag, self.last_modified, self.total_length)

    @classmethod
    def new(cls, url_fingerprint: str, validators: Validators, bytes_written: int) -> ResumeState:
        return cls(
            url_fingerprint=url_fingerprint,
            etag=validators.etag,
            last_modified=validators.last_modified,
            total_length=validators.total_length,
            bytes_written=bytes_written,
            created_at=datetime.now(UTC).isoformat(),
        )

    @staticmethod
    def path_for(part_path: Path) -> Path:
        return part_path.with_suffix(part_path.suffix + '.json')

    def write(self, part_path: Path) -> None:
        state_path = self.path_for(part_path)
        tmp = state_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(asdict(self)), encoding='utf-8')
        tmp.replace(state_path)

    @classmethod
    def read(cls, part_path: Path) -> ResumeState | None:
        state_path = cls.path_for(part_path)
        try:
            data = json.loads(state_path.read_text('utf-8'))
        except (OSError, ValueError):
            return None
        try:
            return cls(**data)
        except TypeError:
            return None

    @classmethod
    def clear(cls, part_path: Path) -> None:
        cls.path_for(part_path).unlink(missing_ok=True)
