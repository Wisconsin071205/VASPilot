"""Self-evolution: agent-authored skills under ``~/.vaspilot/skills``.

Each skill is one directory holding a ``SKILL.md`` (YAML-ish frontmatter
``name``/``description`` + a markdown body) and optional helper scripts.
The index (name + description) is injected into the system prompt so later
sessions discover skills; the body is fetched on demand via ``skill_read``.

Rules:
  - the agent may create and update skills (audited) but never delete them;
    deletion is a human action exposed only through the UI
  - name ``^[a-z0-9][a-z0-9-]{0,63}$``, body <= 16 KiB, <= 50 skills
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.errors import ValidationError

SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_SKILLS = 50
MAX_BODY_BYTES = 16 * 1024
MAX_DESCRIPTION = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_skill_md(text: str) -> tuple[dict[str, str], str]:
    """Split ``---\\nkey: value\\n---\\n<body`` frontmatter (best effort)."""
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].strip().splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    meta[key.strip().lower()] = value.strip()
            body = parts[2].lstrip("\n")
    return meta, body


def render_skill_md(name: str, description: str, body: str) -> str:
    return (f"---\nname: {name}\ndescription: {description}\n---\n\n"
            f"{body.strip()}\n")


class SkillStore:
    def __init__(self, directory: str | Path, audit: Any = None) -> None:
        self.directory = Path(directory)
        self.audit = audit

    def _record(self, event: str, **fields: Any) -> None:
        if self.audit is not None:
            try:
                self.audit.record(event, **fields)
            except Exception:
                pass

    def _dir(self, name: str) -> Path:
        if not SKILL_NAME_RE.fullmatch(name or ""):
            raise ValidationError(
                "skill name must match [a-z0-9][a-z0-9-]{0,63}")
        return self.directory / name

    def list(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not self.directory.is_dir():
            return out
        for entry in sorted(self.directory.iterdir()):
            skill_md = entry / "SKILL.md"
            if not entry.is_dir() or not skill_md.is_file():
                continue
            meta, _body = parse_skill_md(
                skill_md.read_text(encoding="utf-8", errors="replace"))
            out.append({
                "name": entry.name,
                "description": meta.get("description", "")[:MAX_DESCRIPTION],
                "size": skill_md.stat().st_size,
                "updated_at": datetime.fromtimestamp(
                    skill_md.stat().st_mtime, timezone.utc).isoformat(
                        timespec="seconds"),
            })
        return out

    def read(self, name: str) -> dict[str, Any]:
        skill_md = self._dir(name) / "SKILL.md"
        if not skill_md.is_file():
            raise ValidationError(f"skill {name!r} not found")
        text = skill_md.read_text(encoding="utf-8", errors="replace")
        meta, body = parse_skill_md(text)
        scripts = sorted(p.name for p in self._dir(name).glob("scripts/*")
                         if p.is_file()) if self._dir(name).is_dir() else []
        return {"name": name, "description": meta.get("description", ""),
                "content": body, "scripts": scripts}

    def write(self, name: str, description: str, body: str) -> dict[str, Any]:
        existing = self._dir(name).is_dir() and (self._dir(name) / "SKILL.md").is_file()
        if not existing and len(self.list()) >= MAX_SKILLS:
            raise ValidationError(f"skill cap of {MAX_SKILLS} reached")
        description = str(description or "").strip()[:MAX_DESCRIPTION]
        if not description:
            raise ValidationError("skill description is required")
        body_bytes = len(str(body).encode("utf-8"))
        if body_bytes > MAX_BODY_BYTES:
            raise ValidationError(
                f"skill body exceeds {MAX_BODY_BYTES} bytes")
        directory = self._dir(name)
        directory.mkdir(parents=True, exist_ok=True)
        content = render_skill_md(name, description, str(body))
        target = directory / "SKILL.md"
        fd, tmp = tempfile.mkstemp(prefix=".SKILL.md.", dir=str(directory))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._record("skill.write", outcome="ok", skill=name,
                     created=not existing, size=target.stat().st_size)
        return {"name": name, "description": description,
                "size": target.stat().st_size, "updated": _now()}

    def delete(self, name: str) -> dict[str, Any]:
        """Human-side only (UI settings); never exposed as a model tool."""
        import shutil
        directory = self._dir(name)
        if not directory.is_dir():
            raise ValidationError(f"skill {name!r} not found")
        shutil.rmtree(directory)
        self._record("skill.delete", outcome="ok", skill=name)
        return {"deleted": name}

    # -- prompt injection -----------------------------------------------------------
    def index_prompt(self) -> str:
        skills = self.list()
        if not skills:
            return ""
        lines = ["Available skills (fetch the full guide with skill_read):"]
        for skill in skills:
            lines.append(f"- {skill['name']}: {skill['description']}")
        lines.append("After completing a novel multi-step procedure, consider "
                     "saving the reusable know-how with skill_write.")
        return "\n".join(lines)
