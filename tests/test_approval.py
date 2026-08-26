"""Approval token security: bindings, replay, expiry, tamper."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from vaspilot.core.errors import ApprovalError
from vaspilot.workflow.approval import (decode_token, issue_token,
                                        verify_token)

KEY = b"k" * 48
SERVER = "cl9"
PLAN = "a" * 64
FILES = "b" * 64
PARAMS = "c" * 64


def make(validity_hours=24, now=None, **over):
    return issue_token(KEY, server=over.get("server", SERVER),
                       plan_hash=over.get("plan_hash", PLAN),
                       files_hash=over.get("files_hash", FILES),
                       action=over.get("action", "workflow_run"),
                       parameter_hash=over.get("parameter_hash", PARAMS),
                       validity_hours=validity_hours, now=now)


def _verify(encoded, **over):
    kwargs = {"server": SERVER, "plan_hash": PLAN, "files_hash": FILES,
              "action": "workflow_run", "parameter_hash": PARAMS}
    kwargs.update(over)
    return verify_token(KEY, encoded, **kwargs)


class TestIssueVerify:
    def test_roundtrip(self):
        token = make()
        verified = _verify(token.encode())
        assert verified.token_id == token.token_id

    def test_signature_tamper_detected(self):
        token = make()
        forged = token.encode()[:-4] + "beef"
        with pytest.raises(ApprovalError, match="signature"):
            _verify(forged)

    def test_wrong_key_rejected(self):
        token = make()
        with pytest.raises(ApprovalError, match="signature"):
            verify_token(b"j" * 48, token.encode(), server=SERVER,
                         plan_hash=PLAN, files_hash=FILES,
                         action="workflow_run", parameter_hash=PARAMS)

    @pytest.mark.parametrize("field,value", [
        ("server", "other"), ("plan_hash", "d" * 64),
        ("files_hash", "e" * 64), ("action", "job_submit"),
        ("parameter_hash", "f" * 64),
    ])
    def test_binding_mismatch(self, field, value):
        token = make()
        with pytest.raises(ApprovalError, match="binding"):
            _verify(token.encode(), **{field: value})

    def test_expiry(self):
        now = datetime.now(timezone.utc)
        token = issue_token(KEY, server=SERVER, plan_hash=PLAN, files_hash=FILES,
                            action="workflow_run", parameter_hash=PARAMS,
                            validity_hours=1, now=now)
        later = now + timedelta(hours=2)
        with pytest.raises(ApprovalError, match="expired"):
            _verify(token.encode(), now=later)

    def test_valid_before_expiry(self):
        now = datetime.now(timezone.utc)
        token = issue_token(KEY, server=SERVER, plan_hash=PLAN, files_hash=FILES,
                            action="workflow_run", parameter_hash=PARAMS,
                            validity_hours=1, now=now)
        _verify(token.encode(), now=now + timedelta(minutes=30))

    def test_malformed_tokens(self):
        for bad in ("", "not-a-token", "e30.bee", "####.aaaa"):
            with pytest.raises(ApprovalError):
                decode_token(bad)

    def test_model_cannot_mint_without_key(self):
        """A forged unsigned payload fails signature verification."""
        from vaspilot.core.hashing import canonical_json
        import base64
        payload = {"token_id": "0" * 16, "server": SERVER, "plan_hash": PLAN,
                   "files_hash": FILES, "action": "workflow_run",
                   "parameter_hash": PARAMS,
                   "expires_at": "2099-01-01T00:00:00+00:00"}
        b64 = base64.urlsafe_b64encode(
            canonical_json(payload).rstrip("\n").encode()).decode()
        forged = f"{b64}.{'0' * 64}"
        with pytest.raises(ApprovalError):
            _verify(forged)
