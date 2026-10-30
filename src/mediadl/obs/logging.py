"""Structured, redacted logging (spec section 9.5)."""
from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from mediadl.net.redact import redact_mapping, redact_text

_RESERVED = frozenset(logging.LogRecord('', 0, '', 0, '', None, None).__dict__) | {
    'message', 'asctime', 'taskName',
}


class RedactionFilter(logging.Filter):
    """Masks secrets in the message and in every structured extra field."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_text(record.getMessage())
            record.args = ()
        except Exception:  # pragma: no cover - never break logging
            record.msg = '«unformattable log record»'
            record.args = ()
        extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED}
        for key, value in redact_mapping(extras).items():
            record.__dict__[key] = value
        if record.exc_info:
            # The traceback text itself can carry a token from an exception message.
            record.__dict__['exc_redacted'] = True
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            'ts': datetime.fromtimestamp(record.created, UTC).isoformat(),
            'level': record.levelname.lower(),
            'event': record.getMessage(),
            'logger': record.name,
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key == 'exc_redacted':
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                value = str(value)
            payload[key] = value
        if record.exc_info:
            payload['exception'] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure(level: str = 'INFO', *, json_output: bool = False, stream: Any = None) -> None:
    """Install the redaction filter on the root handler. Safe to call twice."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    for existing in list(root.handlers):
        root.removeHandler(existing)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.addFilter(RedactionFilter())
    handler.setFormatter(
        JsonFormatter() if json_output else logging.Formatter('%(levelname)-7s %(name)s %(message)s')
    )
    root.addHandler(handler)


class StructuredLogger:
    """Thin adapter so call sites can pass structured fields as kwargs.

    ``logging`` only accepts extras through ``extra=``, and silently refuses
    keys that collide with ``LogRecord`` attributes, so collisions are
    renamed rather than dropped.
    """

    __slots__ = ('_logger',)

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    @staticmethod
    def _extra(fields: dict[str, Any]) -> dict[str, Any]:
        return {
            (f'field_{key}' if key in _RESERVED else key): value
            for key, value in fields.items()
        }

    def debug(self, event: str, **fields: Any) -> None:
        self._logger.debug(event, extra=self._extra(fields))

    def info(self, event: str, **fields: Any) -> None:
        self._logger.info(event, extra=self._extra(fields))

    def warning(self, event: str, **fields: Any) -> None:
        self._logger.warning(event, extra=self._extra(fields))

    def error(self, event: str, **fields: Any) -> None:
        self._logger.error(event, extra=self._extra(fields))

    def exception(self, event: str, **fields: Any) -> None:
        self._logger.exception(event, extra=self._extra(fields))

    def isEnabledFor(self, level: int) -> bool:
        return self._logger.isEnabledFor(level)


def get_logger(name: str) -> StructuredLogger:
    return StructuredLogger(logging.getLogger(name))
