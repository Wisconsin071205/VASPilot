"""Bounded log discovery; filenames are hints, job-bound receipts are evidence."""
from __future__ import annotations

import hashlib
import re
import shlex
from pathlib import PurePosixPath

from ..core.validation import remote_path, valid_job_id
from .vasp import denied_text_file

MAX_LOGS = 8
MAX_CHARS = 12000
MAX_TOTAL = 60000
DEFAULT_LOGS = {"job_info.log", "summary.log", "stdout", "stderr", "job.out",
                "job.err", "output.log", "error.log", "run.log"}
VASP_LOGS = {"OUTCAR", "OSZICAR", "vasp.out", "vasp.err", "vasp.time.log"}
JOB_MARKER = re.compile(r"^\s*(?:jobid|job[_ ]id|PBS_JOBID|SLURM_JOB_ID)\s*[:=]\s*(\d+)(?:\.[\w.-]+)?\s*$", re.M | re.I)


def script_outputs(content: str, directory: str, job_id: str) -> tuple[list[str], list[str]]:
    """Parse directive operands only. Never execute or expand shell expressions."""
    outputs, warnings = [], []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            break  # schedulers stop scanning at the first command
        match = re.match(r"^#(?:PBS|SBATCH)\s+(.+)", stripped)
        if not match:
            continue
        try:
            args = shlex.split(match[1])
        except ValueError:
            continue
        for index, token in enumerate(args):
            value = ""
            if token in {"-o", "-e", "--output", "--error"} and index + 1 < len(args):
                value = args[index + 1]
            elif token.startswith(("--output=", "--error=")):
                value = token.split("=", 1)[1]
            elif token.startswith(("-o", "-e")) and len(token) > 2:
                value = token[2:]
            if not value:
                continue
            value = value.replace("%j", job_id)
            if any(char in value for char in ("$", "`", "%", ":", "~")):
                warnings.append(f"脚本日志路径需要运行环境展开，未猜测：{value[:160]}")
                continue
            path = value if value.startswith("/") else str(PurePosixPath(directory) / value)
            try:
                outputs.append(remote_path(path, remote_root=directory))
            except Exception:
                warnings.append(f"脚本日志位于本次作业目录之外，未自动读取：{value[:160]}")
    return list(dict.fromkeys(outputs)), warnings


def _filename_job(name: str) -> str:
    match = re.match(r"^(?:slurm-|rsci-)(\d+)(?:\.[\w.-]+)?\.(?:out|err)$", name)
    if not match:
        match = re.search(r"\.[oe](\d+)(?:\.[\w.-]+)?$", name)
    return match[1] if match else ""


def collect_logs(client, *, server: str, job_id: str, directory: str,
                 script: str = "", scheduler_state: str = "UNKNOWN",
                 submitted_at: str = "") -> dict:
    valid_job_id(job_id)
    warnings, excluded = [], []
    listing = client.find(directory, pattern="*", max_depth=2, limit=301, server=server)
    files = {}
    for item in listing.get("files", [])[:300]:
        path = str(item.get("path") or "")
        try:
            remote_path(path, remote_root=directory)
        except Exception:
            continue
        if denied_text_file(PurePosixPath(path).name):
            continue
        files[path] = item
    if listing.get("truncated") or len(listing.get("files", [])) > 300:
        warnings.append("仅检查作业目录及下一层的前 300 个文件。")
    domain = "vasp" if any(PurePosixPath(p).name in {"INCAR", "OUTCAR", "OSZICAR"} for p in files) else "general"
    candidates: dict[str, tuple[int, str]] = {}
    # The submitted script has priority. Otherwise inspect at most two local
    # scripts as hints, without assuming they were actually submitted.
    preferred = str(PurePosixPath(directory) / script) if script else ""
    scripts = ([preferred] if preferred in files else
               sorted(p for p in files if p.endswith((".sh", ".pbs", ".slurm")))[:2])
    for path in scripts:
        try:
            if int(files[path].get("size") or 0) > 32768:
                warnings.append(f"脚本过大，未扫描日志指令：{PurePosixPath(path).name}")
                continue
            content = client.read(path, server=server, max_bytes=32768).get("content", "")
            outputs, notes = script_outputs(str(content), directory, job_id)
            warnings.extend(notes)
            for output in outputs:
                if denied_text_file(PurePosixPath(output).name):
                    continue
                # Explicit paths may be deeper than the bounded directory walk.
                if output not in files:
                    try:
                        info = client.stat(output, server=server)
                        if info.get("kind") not in {"file", "regular file"}:
                            continue
                        files[output] = {**info, "path": output}
                    except Exception:
                        warnings.append(f"脚本指定的日志尚不可读：{output}")
                        continue
                candidates[output] = (0, "提交脚本日志路径" if path == preferred else "目录内脚本日志路径（待核对）")
        except Exception as exc:
            warnings.append(f"脚本读取失败：{PurePosixPath(path).name}：{str(exc)[:160]}")
    for path, item in files.items():
        name = PurePosixPath(path).name
        bound = _filename_job(name)
        if bound and bound != job_id:
            excluded.append({"path": path, "reason": "文件名属于其他作业"})
            candidates.pop(path, None)
            continue
        if bound:
            candidates.setdefault(path, (1, "作业号匹配的调度输出"))
        elif name in {"job_info.log", "summary.log"}:
            candidates.setdefault(path, (2, "默认运行记录"))
        elif name in VASP_LOGS and domain == "vasp":
            candidates.setdefault(path, (3, "VASP 默认日志"))
        elif name in DEFAULT_LOGS or name.endswith((".log", ".out", ".err", ".pbsout")):
            candidates.setdefault(path, (4, "常见日志名（归属待核对）"))
    logs, receipts, remaining = [], [], MAX_TOTAL
    for path, (_, reason) in sorted(candidates.items(), key=lambda pair: (pair[1][0], pair[0]))[:MAX_LOGS]:
        try:
            raw = str(client.tail(path, lines=160, server=server).get("content") or "")
            if "\x00" in raw:
                warnings.append(f"跳过疑似二进制文件：{path}")
                continue
            content = raw[-min(MAX_CHARS, remaining):]
            markers = set(JOB_MARKER.findall(content))
            if markers and markers != {job_id}:
                excluded.append({"path": path, "reason": "日志中的作业号不匹配或混有其他作业"})
                continue
            relation = "job_id_verified" if markers == {job_id} else "filename_job_id" if _filename_job(PurePosixPath(path).name) == job_id else "directory_only"
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            logs.append({"path": path, "reason": reason, "relation": relation,
                         "content": content, "sha256": digest,
                         "size": files[path].get("size"), "mtime": files[path].get("mtime"),
                         "scope": "末尾最多 160 行，每文件最多 12000 字符",
                         "truncated": len(raw) > len(content)})
            # A program result can be attributed only within the SAME log
            # carrying this job ID; never combine an old summary with a new header.
            if relation == "job_id_verified":
                code = re.search(r"^\s*(vasp_exit_code|exit_code)\s*[:=]\s*(-?\d+)\s*$", content, re.M)
                end = re.search(r"^\s*end\s*:\s*(.+)$", content, re.M)
                if code and end:
                    receipts.append({"path": path, "sha256": digest, "job_id": job_id,
                                     "program": "VASP" if code[1] == "vasp_exit_code" else "program",
                                     "exit_code": int(code[2]), "end_text": end[1].strip()})
            remaining -= len(content)
            if remaining <= 0:
                break
        except Exception as exc:
            warnings.append(f"日志读取失败：{path}：{str(exc)[:180]}")
    if len(candidates) > MAX_LOGS:
        warnings.append("按脚本路径、作业号、默认日志排序，最多读取 8 个文件；其他文件可在远端目录查看。")
    if any(log["relation"] == "directory_only" for log in logs):
        warnings.append("部分日志只匹配目录和文件名，可能来自之前的运行；不能直接归属于本次作业。")
    return {"ok": True, "server": server, "job_id": job_id, "directory": directory,
            "scheduler_state": scheduler_state, "domain": domain, "submitted_at": submitted_at,
            "logs": logs, "execution_receipts": receipts, "excluded": excluded[:20],
            "warnings": warnings, "skill": "job-log-analysis", "candidate_count": len(candidates),
            "limits": {"files": MAX_LOGS, "total_characters": MAX_TOTAL},
            "submission_performed": False}
