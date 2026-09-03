"""Local SSH credential helpers.

The vault itself lives in :mod:`vaspilot.core.config` and uses Windows DPAPI.
This module contains only non-persistent helpers: TOTP generation and a short
lived ``SSH_ASKPASS`` bridge used by OpenSSH while one command is running.
Secrets are passed to the child process through its environment and are never
put in command arguments, audit rows, or the temporary helper source.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
import shlex
from urllib.parse import parse_qs, urlparse


def normalize_totp_seed(value: str) -> str:
    """Return a canonical Base32 TOTP seed.

    The UI accepts either the value copied from Google Authenticator or an
    ``otpauth://`` URI.  Spaces and dashes are ignored; invalid seeds fail
    before anything is written to the vault.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lower().startswith("otpauth://"):
        parsed = urlparse(text)
        text = (parse_qs(parsed.query).get("secret") or [""])[0]
    text = "".join(text.split()).replace("-", "").upper()
    if not text:
        return ""
    try:
        base64.b32decode(text + "=" * ((8 - len(text) % 8) % 8),
                         casefold=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("Google 验证密钥不是有效的 Base32/TOTP 密钥") from exc
    return text


def totp_code(seed: str, *, at: float | None = None) -> str:
    """Generate the current six-digit RFC 6238 code for a TOTP seed."""
    normalized = normalize_totp_seed(seed)
    if not normalized:
        return ""
    raw = base64.b32decode(normalized + "=" *
                           ((8 - len(normalized) % 8) % 8), casefold=True)
    counter = int((time.time() if at is None else at) // 30)
    digest = hmac.new(raw, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = (int.from_bytes(digest[offset:offset + 4], "big") & 0x7FFFFFFF)
    return f"{number % 1_000_000:06d}"


_ASKPASS_HELPER = r'''#!/usr/bin/env python3
import os
import sys

prompt = " ".join(sys.argv[1:]).lower()
is_otp = any(token in prompt for token in (
    "verification", "one-time", "one time", "otp", "passcode",
    "token", "验证码", "动态码", "code"))
name = "VASPILOT_ASKPASS_TOTP" if is_otp else "VASPILOT_ASKPASS_PASSWORD"
value = os.environ.get(name, "")
sys.stdout.write(value + "\n")
'''


@contextmanager
def askpass_env(password: str, totp_seed: str):
    """Yield an environment that lets one OpenSSH process answer prompts.

    The helper files contain no credentials and are removed on exit.  The
    plaintext values exist only in the child-process environment for the
    duration of the SSH invocation.  ``SSH_ASKPASS_REQUIRE=force`` is
    supported by current OpenSSH builds on Windows, Linux, and macOS.
    """
    password = str(password or "")
    totp_value = totp_code(totp_seed) if totp_seed else ""
    if not password:
        yield None
        return
    root = Path(tempfile.mkdtemp(prefix="vaspilot-askpass-"))
    helper = root / "askpass.py"
    wrapper = root / ("askpass.cmd" if os.name == "nt" else "askpass")
    try:
        helper.write_text(_ASKPASS_HELPER, encoding="utf-8", newline="\n")
        if os.name == "nt":
            # %* preserves the prompt argument supplied by OpenSSH.  The
            # actual secret remains in the inherited environment, not here.
            wrapper.write_text(
                f'@echo off\n"{sys.executable}" "{helper}" %*\n',
                encoding="utf-8", newline="\r\n")
        else:
            wrapper.write_text(
                f"#!/bin/sh\nexec {shlex.quote(sys.executable)} "
                f"{shlex.quote(str(helper))} \"$@\"\n",
                encoding="utf-8", newline="\n")
            wrapper.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        try:
            helper.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        env = os.environ.copy()
        env.update({
            "SSH_ASKPASS": str(wrapper),
            "SSH_ASKPASS_REQUIRE": "force",
            # OpenSSH checks DISPLAY on some Unix builds before invoking an
            # askpass helper.  It is harmless on Windows.
            "DISPLAY": env.get("DISPLAY") or "vaspilot",
            "VASPILOT_ASKPASS_PASSWORD": password,
            "VASPILOT_ASKPASS_TOTP": totp_value,
        })
        yield env
    finally:
        for path in (wrapper, helper):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            root.rmdir()
        except OSError:
            pass
