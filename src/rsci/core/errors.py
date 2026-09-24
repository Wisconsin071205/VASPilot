"""Exception hierarchy with stable exit codes.

Every CLI command maps an exception to ``{"ok": false, "error": {...}}`` plus a
documented process exit code so scripts can branch without parsing text.
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_AUTH_REQUIRED = 3
EXIT_APPROVAL_REQUIRED = 4
EXIT_VALIDATION = 5


class rsciError(Exception):
    """Base class. ``code`` is the machine-readable error id."""

    exit_code = EXIT_ERROR
    code = "error"

    def __init__(self, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **self.detail}


class ValidationError(rsciError):
    """An argument or path failed strict validation."""

    exit_code = EXIT_VALIDATION
    code = "validation_error"


class UsageError(rsciError):
    """Bad CLI usage (wrong combination of flags)."""

    exit_code = EXIT_USAGE
    code = "usage_error"


class AuthRequiredError(rsciError):
    """The reusable SSH session expired or was never established.

    The CLI surfaces ``auth_required``. Either server route may use a
    user-authorized DPAPI credential until its periodic retention confirmation
    is due; otherwise the user re-authenticates in a visible terminal via
    ``rsci server connect``.
    """

    exit_code = EXIT_AUTH_REQUIRED
    code = "auth_required"


class ApprovalError(rsciError):
    """A plan approval is missing, invalid, expired or already consumed."""

    exit_code = EXIT_APPROVAL_REQUIRED
    code = "approval_error"


class RemoteError(rsciError):
    """A named gateway operation failed on the remote side."""

    exit_code = EXIT_ERROR
    code = "remote_error"


class SchedulerError(rsciError):
    """A Slurm/PBS operation failed or returned an unparseable response."""

    code = "scheduler_error"


class ProviderError(rsciError):
    """A model provider call failed (network, protocol, quota...)."""

    code = "provider_error"


class ToolNotAllowedError(rsciError):
    """The active provider is analysis_only and may not call a write tool."""

    exit_code = EXIT_ERROR
    code = "tool_not_allowed"


class ConfigError(rsciError):
    """Local configuration is missing or malformed."""

    code = "config_error"
