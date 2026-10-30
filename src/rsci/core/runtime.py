"""Identify the local app sources loaded by a UI instance."""
from hashlib import sha256
from pathlib import Path


def source_fingerprint(root: Path | None = None) -> str:
    root = root or Path(__file__).resolve().parents[1]
    digest = sha256()
    for path in sorted(root.rglob("*")):
        if path.suffix not in {".py", ".html", ".js"} or not path.is_file():
            continue
        stat = path.stat()
        digest.update(f"{path.relative_to(root)}:{stat.st_size}:{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()
