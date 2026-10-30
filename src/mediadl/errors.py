"""Typed error hierarchy (spec section 9.2).

Every error carries a stable machine code, a retryability flag, and a
user-safe message. ``detail`` is for logs and never rendered to end users
without passing through the redaction filter first.
"""
from __future__ import annotations

import enum
from collections.abc import Mapping
from typing import Any


class ErrorCode(enum.StrEnum):
    INVALID_INPUT = 'INVALID_INPUT'
    UNSUPPORTED_SOURCE = 'UNSUPPORTED_SOURCE'
    AMBIGUOUS_SOURCE = 'AMBIGUOUS_SOURCE'
    POLICY_DENIED = 'POLICY_DENIED'
    AUTH_REQUIRED = 'AUTH_REQUIRED'
    AUTH_EXPIRED = 'AUTH_EXPIRED'
    PERMISSION_MISSING = 'PERMISSION_MISSING'
    NOT_FOUND = 'NOT_FOUND'
    NOT_ACCESSIBLE_WITH_AUTHORIZED_ACCOUNT = 'NOT_ACCESSIBLE_WITH_AUTHORIZED_ACCOUNT'
    NOT_IN_INDEX_WINDOW = 'NOT_IN_INDEX_WINDOW'
    UNAVAILABLE_COPYRIGHT = 'UNAVAILABLE_COPYRIGHT'
    UNAVAILABLE_DRM = 'UNAVAILABLE_DRM'
    RATE_LIMITED = 'RATE_LIMITED'
    UPSTREAM_ERROR = 'UPSTREAM_ERROR'
    NETWORK_ERROR = 'NETWORK_ERROR'
    LOCATOR_EXPIRED = 'LOCATOR_EXPIRED'
    INTEGRITY_ERROR = 'INTEGRITY_ERROR'
    CONTENT_TYPE_MISMATCH = 'CONTENT_TYPE_MISMATCH'
    SIZE_LIMIT_EXCEEDED = 'SIZE_LIMIT_EXCEEDED'
    QUOTA_EXCEEDED = 'QUOTA_EXCEEDED'
    NO_ACCEPTABLE_REPRESENTATION = 'NO_ACCEPTABLE_REPRESENTATION'
    TOOL_MISSING = 'TOOL_MISSING'
    PROCESSING_FAILED = 'PROCESSING_FAILED'
    PROCESSING_TIMEOUT = 'PROCESSING_TIMEOUT'
    MALICIOUS_INPUT = 'MALICIOUS_INPUT'
    PATH_REJECTED = 'PATH_REJECTED'
    STORAGE_FULL = 'STORAGE_FULL'
    CANCELLED = 'CANCELLED'
    RETRIES_EXHAUSTED = 'RETRIES_EXHAUSTED'
    CONNECTOR_CONTRACT_ERROR = 'CONNECTOR_CONTRACT_ERROR'
    INTERNAL_ERROR = 'INTERNAL_ERROR'


class MediaDLError(Exception):
    """Base class. Subclasses fix ``code``/``retryable``/``user_message``."""

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    retryable: bool = False
    user_message: str = 'Something went wrong. It has been reported.'

    def __init__(self, message: str | None = None, **detail: Any) -> None:
        self.detail: dict[str, Any] = dict(detail)
        super().__init__(message or self.user_message)

    def public(self) -> Mapping[str, Any]:
        """Redaction-safe dict for persistence in job records and API bodies."""
        from mediadl.net.redact import redact_mapping

        return {
            'code': str(self.code),
            'retryable': self.retryable,
            'user_message': self.user_message,
            'detail': redact_mapping(self.detail),
        }

    def __str__(self) -> str:  # pragma: no cover - trivial
        base = super().__str__()
        return f'[{self.code}] {base}'


# --- input / routing -------------------------------------------------------
class InputError(MediaDLError):
    code = ErrorCode.INVALID_INPUT
    user_message = "This link or file isn't valid."


class UnsupportedSourceError(MediaDLError):
    code = ErrorCode.UNSUPPORTED_SOURCE
    user_message = "This source isn't supported."


class AmbiguousSourceError(MediaDLError):
    code = ErrorCode.AMBIGUOUS_SOURCE
    user_message = 'This input matched more than one source.'


# --- policy ---------------------------------------------------------------
class PolicyDeniedError(MediaDLError):
    code = ErrorCode.POLICY_DENIED
    user_message = "This request isn't allowed by the current settings."


class QuotaExceededError(MediaDLError):
    code = ErrorCode.QUOTA_EXCEEDED
    user_message = "You've reached your download quota."


class PathRejectedError(MediaDLError):
    code = ErrorCode.PATH_REJECTED
    user_message = "The output location isn't allowed."


class SizeLimitError(MediaDLError):
    code = ErrorCode.SIZE_LIMIT_EXCEEDED
    user_message = 'This file is larger than the allowed limit.'


# --- authorization --------------------------------------------------------
class AuthorizationNeeded(MediaDLError):
    """Job-level condition: the user must act before the job can proceed."""

    code = ErrorCode.AUTH_REQUIRED
    user_message = 'Connect your account to continue.'


class AuthRequiredError(AuthorizationNeeded):
    code = ErrorCode.AUTH_REQUIRED
    user_message = 'Connect your Instagram professional account to continue.'


class AuthExpiredError(AuthorizationNeeded):
    code = ErrorCode.AUTH_EXPIRED
    user_message = 'Your connection expired. Please reconnect.'


class PermissionMissingError(AuthorizationNeeded):
    code = ErrorCode.PERMISSION_MISSING
    user_message = 'The connection is missing the permission needed to read media.'


# --- connector/provider ---------------------------------------------------
class NotFoundError(MediaDLError):
    code = ErrorCode.NOT_FOUND
    user_message = 'This item no longer exists or was deleted.'


class NotAccessibleError(MediaDLError):
    code = ErrorCode.NOT_ACCESSIBLE_WITH_AUTHORIZED_ACCOUNT
    user_message = (
        "This post isn't available through your connected account. "
        "Only your own professional account's media can be downloaded."
    )

    def __init__(self, message: str | None = None, *, code: ErrorCode | None = None, **detail: Any) -> None:
        if code is not None:
            self.code = code
            if code is ErrorCode.NOT_IN_INDEX_WINDOW:
                self.user_message = (
                    'This post is older than the most recent 10,000 items the API provides.'
                )
        super().__init__(message, **detail)


class ContentUnavailableError(MediaDLError):
    code = ErrorCode.UNAVAILABLE_COPYRIGHT
    user_message = "The provider doesn't make this media available for download."


class DrmError(MediaDLError):
    code = ErrorCode.UNAVAILABLE_DRM
    user_message = "This media is protected and can't be downloaded."


class RateLimited(MediaDLError):
    code = ErrorCode.RATE_LIMITED
    retryable = True
    user_message = 'The provider asked us to slow down. The job will continue automatically.'

    def __init__(self, message: str | None = None, *, resume_at: float | None = None, **detail: Any) -> None:
        self.resume_at = resume_at
        super().__init__(message, resume_at=resume_at, **detail)


class UpstreamError(MediaDLError):
    code = ErrorCode.UPSTREAM_ERROR
    retryable = True
    user_message = 'The provider had a temporary problem. Retrying.'


# --- transfer -------------------------------------------------------------
class TransferError(MediaDLError):
    code = ErrorCode.NETWORK_ERROR
    retryable = True
    user_message = 'Network problem. Retrying.'


class NetworkError(TransferError):
    code = ErrorCode.NETWORK_ERROR


class IntegrityError(TransferError):
    code = ErrorCode.INTEGRITY_ERROR
    user_message = 'The download was incomplete. Retrying.'


class LocatorExpiredError(TransferError):
    code = ErrorCode.LOCATOR_EXPIRED
    user_message = 'The download link expired. Refreshing.'


class ContentTypeMismatchError(MediaDLError):
    code = ErrorCode.CONTENT_TYPE_MISMATCH
    user_message = 'The server returned unexpected content.'


class ClientResponseError(MediaDLError):
    """A 4xx that is the request's fault, not a transient condition."""

    code = ErrorCode.UPSTREAM_ERROR
    retryable = False
    user_message = 'The provider rejected the request for this media.'


class RetriesExhaustedError(MediaDLError):
    code = ErrorCode.RETRIES_EXHAUSTED
    user_message = 'The operation failed repeatedly. Please try again later.'


# --- selection / processing ----------------------------------------------
class NoAcceptableRepresentationError(MediaDLError):
    code = ErrorCode.NO_ACCEPTABLE_REPRESENTATION
    user_message = 'No downloadable version of this media is available.'


class ToolMissingError(MediaDLError):
    code = ErrorCode.TOOL_MISSING
    user_message = 'FFmpeg is required for this step.'


class ProcessingError(MediaDLError):
    code = ErrorCode.PROCESSING_FAILED
    user_message = 'Media processing failed.'


class ProcessingTimeoutError(ProcessingError):
    code = ErrorCode.PROCESSING_TIMEOUT
    user_message = 'Processing took too long.'


class MaliciousInputError(MediaDLError):
    code = ErrorCode.MALICIOUS_INPUT
    user_message = 'The file failed safety checks.'


# --- storage / jobs -------------------------------------------------------
class StorageError(MediaDLError):
    code = ErrorCode.STORAGE_FULL
    user_message = 'Not enough storage space.'


class CancelledByUser(MediaDLError):
    code = ErrorCode.CANCELLED
    user_message = 'Cancelled.'


class ConnectorContractError(MediaDLError):
    code = ErrorCode.CONNECTOR_CONTRACT_ERROR
    user_message = 'Something went wrong. It has been reported.'


class InvalidTransitionError(MediaDLError):
    code = ErrorCode.INTERNAL_ERROR
    user_message = 'Something went wrong. It has been reported.'


class JobNotFoundError(MediaDLError):
    code = ErrorCode.NOT_FOUND
    user_message = 'That job does not exist.'


class ConcurrencyConflictError(MediaDLError):
    code = ErrorCode.INTERNAL_ERROR
    retryable = True
    user_message = 'Please try again.'


class InternalError(MediaDLError):
    code = ErrorCode.INTERNAL_ERROR

    @classmethod
    def from_exc(cls, exc: BaseException) -> InternalError:
        return cls(f'{type(exc).__name__}: {exc}', exc_type=type(exc).__name__)
