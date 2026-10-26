"""Optional CPU OCR component. Installation is explicit, isolated and resumable."""
from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from ..core.errors import ValidationError
from ..core.hashing import file_sha256
from ..core.locking import try_file_lock
from ..core.project_paths import confined_path
from .common import fingerprint, location, read, save

VERSIONS = {"docling": "2.142.1", "rapidocr": "3.10.0", "onnxruntime": "1.31.0"}


def status(config):
    marker = config.home / "research-runtime" / "active.json"
    if marker.is_file():
        row = read(marker)
        base = (config.home / "research-runtime").resolve()
        executable = (base / row["python"]).resolve()
        if base not in executable.parents:
            raise ValidationError("OCR Python 路径超出组件目录")
        if executable.is_file() and row.get("versions") == VERSIONS and row.get("dependency_lock_sha256") == file_sha256(Path(__file__).with_name("ocr-requirements.lock")):
            return {**row, "ready": True, "executable": str(executable)}
    return {"ready": False, "versions": VERSIONS, "message": "OCR 组件尚未安装；请安装组件后解析扫描件。",
            "disk_required_bytes": 8 * 1024 ** 3}


def _run(command, config, task_id, *, env=None):
    from .tasks import check_cancel, path
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    with path(config, task_id).with_suffix(".component.log").open("ab") as log:
        child = subprocess.Popen([str(x) for x in command], stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=log, env=env, **kwargs)
        try:
            while child.poll() is None:
                check_cancel(config, task_id)
                time.sleep(.3)
            if child.returncode:
                raise ValidationError(f"组件进程退出码 {child.returncode}；查看任务组件日志后重试。")
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


def install(config, task_id, offline_bundle=None):
    from .tasks import update
    base = confined_path(config.home, "research-runtime")
    base.mkdir(parents=True, exist_ok=True)
    with try_file_lock(base / "install.lock") as acquired:
        if not acquired:
            raise ValidationError("另一进程正在安装 OCR 组件，请等待该任务")
        if status(config)["ready"]:
            return status(config)
        if shutil.disk_usage(base).free < 8 * 1024 ** 3:
            raise ValidationError("OCR 安装至少需要 8 GiB 可用空间")
        version = fingerprint(VERSIONS)[:16]
        target = confined_path(base, version)
        target.mkdir(exist_ok=True)
        if offline_bundle:
            update(config, task_id, phase="installing", message="校验并导入离线组件")
            _import_bundle(Path(offline_bundle), target)
            candidates = sorted(set(p.resolve() for p in (target / "python").glob("*/python.exe")))
            if os.name == "nt" and len(candidates) == 1:
                cfg = target / "env/pyvenv.cfg"
                lines = cfg.read_text(encoding="utf-8").splitlines()
                lines = ["home = " + str(candidates[0].parent) if line.startswith("home =") else line for line in lines]
                cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            update(config, task_id, phase="installing", message="下载独立 Python、CPU 依赖和 OCR 模型；首次可能耗时较长")
            uv = _uv(base)
            env = dict(os.environ, UV_PYTHON_INSTALL_DIR=str(target / "python"),
                       UV_CACHE_DIR=str(base / "download-cache"), UV_PYTHON_INSTALL_REGISTRY="0",
                       HF_HOME=str(base / "model-download-cache"), HF_HUB_DISABLE_TELEMETRY="1")
            _run([uv, "python", "install", "3.12.15", "--no-bin", "--no-registry"], config, task_id, env=env)
            candidates = list((target / "python").glob("*/python.exe")) + list((target / "python").glob("*/bin/python3.12"))
            candidates = sorted(set(p.resolve() for p in candidates))
            if len(candidates) != 1:
                raise ValidationError("无法定位独立 Python 3.12")
            python = target / "env" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            if not python.is_file():
                _run([uv, "venv", str(target / "env"), "--python", candidates[0]], config, task_id, env=env)
            requirements = Path(__file__).with_name("ocr-requirements.lock")
            _run([uv, "pip", "sync", "--python", python, "--require-hashes", "--extra-index-url",
                  "https://download.pytorch.org/whl/cpu", "--index-strategy", "unsafe-best-match", requirements], config, task_id, env=env)
            # Model download is deliberate installation work; parsing itself is offline.
            script = Path(__file__).with_name("ocr_bridge.py")
            _run([python, script, "install", target / "models"], config, task_id, env=env)
            manifest = {str(p.relative_to(target)).replace("\\", "/"): file_sha256(p)
                        for p in (target / "models").rglob("*") if p.is_file()
                        and not any(part.startswith('.') for part in p.relative_to(target).parts)}
            save(target / "models.json", manifest)
        python = target / "env" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not python.is_file() or not (target / "models.json").is_file():
            raise ValidationError("组件不完整，请重新安装")
        _run([python, Path(__file__).with_name("ocr_bridge.py"), "probe", target / "models"], config, task_id)
        row = {"versions": VERSIONS, "python": str(python.relative_to(base)).replace("\\", "/"),
               "models": str((target / "models").relative_to(base)).replace("\\", "/"),
               "model_hash": fingerprint(read(target / "models.json")),
               "dependency_lock_sha256": file_sha256(Path(__file__).with_name("ocr-requirements.lock"))}
        save(base / "active.json", row)
        return status(config)


def _uv(base):
    """Bootstrap a pinned upstream uv wheel, verifying its published SHA-256."""
    import urllib.request
    executable = base / ("uv.exe" if os.name == "nt" else "uv")
    if executable.is_file():
        return executable
    info = json.load(urllib.request.urlopen("https://pypi.org/pypi/uv/0.13.5/json", timeout=30))
    import platform
    arch = platform.machine().lower()
    tag = "win_amd64" if os.name == "nt" else ("macosx_11_0_arm64" if sys.platform == "darwin" and arch == "arm64" else "manylinux_2_17_x86_64")
    wheels = [r for r in info["urls"] if r["filename"].endswith(".whl") and tag in r["filename"]]
    if len(wheels) != 1:
        raise ValidationError("此平台需要手动导入对应平台的离线 OCR 组件包")
    wheel = base / "uv.whl"
    with urllib.request.urlopen(wheels[0]["url"], timeout=60) as response, wheel.open("wb") as out:
        shutil.copyfileobj(response, out)
    if file_sha256(wheel) != wheels[0]["digests"]["sha256"]:
        raise ValidationError("组件下载校验失败")
    with zipfile.ZipFile(wheel) as archive:
        member = next(n for n in archive.namelist() if n.endswith("/" + executable.name))
        executable.write_bytes(archive.read(member))
    executable.chmod(0o755)
    return executable


def _import_bundle(source, target):
    with zipfile.ZipFile(source) as archive:
        manifest = json.loads(archive.read("component.json"))
        if manifest.get("versions") != VERSIONS or manifest.get("platform") != sys.platform:
            raise ValidationError("离线组件版本或平台不匹配")
        members = manifest.get("files", {})
        if not members or len(members) > 100000:
            raise ValidationError("无效组件清单")
        if sum(i.file_size for i in archive.infolist()) > 12 * 1024 ** 3:
            raise ValidationError("离线组件过大")
        for name, digest in members.items():
            dest = confined_path(target, name)
            info = archive.getinfo(name)
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValidationError("组件中不允许符号链接")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, dest.open("wb") as out:
                shutil.copyfileobj(src, out)
            if file_sha256(dest) != digest:
                raise ValidationError("离线组件文件校验失败")


def parse(config, name, doc_id, task_id):
    from . import documents
    from .tasks import update, check_cancel
    doc = documents.get(config, name, doc_id)
    runtime = status(config)
    if not runtime["ready"]:
        raise ValidationError(runtime["message"])
    revision = fingerprint({"sha256": doc_id, "versions": VERSIONS, "models": runtime["model_hash"],
                            "options": "cpu-local-page-v1"})
    output = location(config, name, f"references/evidence/{doc_id}/{revision}.json")
    if output.is_file():
        return documents.evidence(config, name, doc_id, revision)
    cache = confined_path(config.home, f"research-cache/extraction/{doc_id}/{revision}")
    cache.mkdir(parents=True, exist_ok=True)
    update(config, task_id, phase="parsing", revision=revision)
    models = confined_path(config.home / "research-runtime", runtime["models"])
    manifest = read(models.parent / "models.json")
    if fingerprint(manifest) != runtime["model_hash"] or any(
            not confined_path(models.parent, n).is_file() or file_sha256(confined_path(models.parent, n)) != digest
            for n, digest in manifest.items()):
        raise ValidationError("OCR 模型文件已改变，请重新安装组件后解析")
    env = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "HOME", "USERPROFILE", "LOCALAPPDATA"}}
    env.update(HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", OMP_NUM_THREADS="2")
    _run([runtime["executable"], Path(__file__).with_name("ocr_bridge.py"), "parse", models,
          location(config, name, doc["source"]), cache, config.home / "research-cache/ocr.lock"], config, task_id, env=env)
    check_cancel(config, task_id)
    documents.get(config, name, doc_id)  # detect replacement while converting
    pages = [read(cache / f"{p}.json") for p in range(1, doc["pages"] + 1)]
    result = {"schema": "rsci.evidence/1", "document_id": doc_id, "revision": revision,
              "pages": pages, "versions": VERSIONS, "model_hash": runtime["model_hash"],
              "limitations": ["OCR 可能误读，引用请对照原页；公式和图表不可直接当作已验证数值。"]}
    for page in pages:
        for index, block in enumerate(page["blocks"]):
            block["citation_id"] = f"{doc_id[:16]}-{revision[:16]}-p{page['page']}-b{index}"
            block["text_sha256"] = fingerprint(block["text"])
    save(output, result)
    doc.update(revision=revision, status="needs_review" if any(p["warnings"] for p in pages) else "ready")
    save(location(config, name, f"references/papers/{doc_id}.json"), doc)
    return {"document_id": doc_id, "revision": revision, "pages": len(pages), "status": doc["status"]}
