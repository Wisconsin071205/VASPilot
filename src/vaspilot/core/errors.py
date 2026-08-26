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


class VaspilotError(Exception):
    """Base class. ``code`` is the machine-readable error id."""

    exit_code = EXIT_ERROR
    code = "error"

    def __init__(self, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **self.detail}


class ValidationError(VaspilotError):
    """An argument or path failed strict validation."""

    exit_code = EXIT_VALIDATION
    code = "validation_error"


class UsageError(VaspilotError):
    """Bad CLI usage (wrong combination of flags)."""

    exit_code = EXIT_USAGE
    code = "usage_error"


class AuthRequiredError(VaspilotError):
    """The reusable SSH session expired or was never established.

    The CLI must surface ``auth_required`` and never attempt to fill in a
    password or TOTP itself; the user re-authenticates in a visible terminal
    via ``vaspilot server connect``.
    """

    exit_code = EXIT_AUTH_REQUIRED
    code = "auth_required"


class ApprovalError(VaspilotError):
    """A plan approval is missing, invalid, expired or already consumed."""

    exit_code = EXIT_APPROVAL_REQUIRED
    code = "approval_error"


class RemoteError(VaspilotError):
    """A named gateway operation failed on the remote side."""

    exit_code = EXIT_ERROR
    code = "remote_error"


class SchedulerError(VaspilotError):
    """A Slurm/PBS operation failed or returned an unparseable response."""

    code = "scheduler_error"


class ProviderError(VaspilotError):
    """A model provider call failed (network, protocol, quota...)."""

    code = "provider_error"


class ToolNotAllowedError(VaspilotError):
    """The active provider is analysis_only and may not call a write tool."""

    exit_code = EXIT_ERROR
    code = "tool_not_allowed"


class ConfigError(VaspilotError):
    """Local configuration is missing or malformed."""

    code = "config_error"
