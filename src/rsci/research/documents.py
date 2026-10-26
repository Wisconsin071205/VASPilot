"""Immutable PDF originals and page-anchored extraction records."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from ..core.errors import ValidationError
from ..core.hashing import file_sha256
from ..core.locking import reentrant_file_lock
from ..core.project_paths import confined_path
from .common import identifier, location, project, read, save, stamp

MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_PAGES = 300


def inspect_pdf(path):
    from pypdf import PdfReader
    try:
        with Path(path).open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                raise ValueError("not a PDF")
            handle.seek(0)
            reader = PdfReader(handle, strict=True)
            if reader.is_encrypted:
                raise ValueError("加密 PDF 请先在本地解密")
            pages = len(reader.pages)
            if not 1 <= pages <= MAX_PAGES:
                raise ValueError("PDF 必须为 1–300 页")
            return pages
    except Exception as exc:
        raise ValidationError(f"无法导入 PDF：{str(exc)[:200]}") from exc


def import_stream(config, name, stream, size, title=""):
    if not isinstance(size, int) or not 1 <= size <= MAX_PDF_BYTES:
        raise ValidationError("PDF 导入上限为 50 MiB")
    store, root, pid = project(config, name)
    with reentrant_file_lock(store.index_path.with_suffix(".lock")):
        base = confined_path(root, "references/papers")
        base.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".import-", dir=base)
        try:
            digest, remaining = hashlib.sha256(), size
            with os.fdopen(fd, "wb") as out:
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValidationError("PDF 上传中断，请重新导入")
                    remaining -= len(chunk)
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            pages, doc_id = inspect_pdf(temporary), digest.hexdigest()
            target = confined_path(base, doc_id + ".pdf")
            metadata = confined_path(base, doc_id + ".json")
            if metadata.exists():
                return get(config, name, doc_id)
            os.replace(temporary, target)
            row = {"schema": "rsci.document/1", "document_id": doc_id, "sha256": doc_id,
                   "project_id": pid, "title": str(title or "未命名论文")[:300],
                   "pages": pages, "bytes": size, "created_at": stamp(), "status": "imported",
                   "source": "references/papers/" + doc_id + ".pdf", "revision": None}
            save(metadata, row)
            return row
        finally:
            Path(temporary).unlink(missing_ok=True)


def get(config, name, doc_id):
    identifier(doc_id)
    _, _, pid = project(config, name)
    row = read(location(config, name, f"references/papers/{doc_id}.json"))
    if row.get("document_id") != doc_id or row.get("project_id") != pid:
        raise ValidationError("文献归属不匹配")
    source = location(config, name, row["source"])
    if not source.is_file() or file_sha256(source) != doc_id:
        raise ValidationError("论文原件已改变或缺失，请重新导入；旧引用未覆盖")
    return row


def list_documents(config, name):
    _, root, _ = project(config, name)
    result = []
    for path in sorted(confined_path(root, "references/papers").glob("*.json")):
        try:
            result.append(get(config, name, path.stem))
        except ValidationError as exc:
            result.append({"document_id": path.stem, "status": "needs_review", "error": str(exc)})
    return result


def evidence(config, name, doc_id, revision=None):
    doc = get(config, name, doc_id)
    revision = identifier(revision or doc.get("revision"))
    result = read(location(config, name, f"references/evidence/{doc_id}/{revision}.json"))
    if result.get("document_id") != doc_id or result.get("revision") != revision:
        raise ValidationError("文献证据版本不匹配")
    return result


def render_page(config, name, doc_id, page):
    import pypdfium2 as pdfium
    doc = get(config, name, doc_id)
    page = int(page)
    if not 1 <= page <= doc["pages"]:
        raise ValidationError("页码超出范围")
    output = confined_path(config.home, f"research-cache/pages/{doc_id}/{page}.png")
    output.parent.mkdir(parents=True, exist_ok=True)
    with reentrant_file_lock(output.with_suffix(".lock")):
        if not output.is_file():
            with pdfium.PdfDocument(location(config, name, doc["source"])) as pdf:
                p = pdf[page - 1]
                scale = min(1.5, 1800 / max(p.get_size()))
                bitmap = p.render(scale=scale)
                # pypdfium2's Pillow adapter is optional; use its native bitmap
                # buffer with a minimal lossless PNG encoder instead.
                import struct, zlib
                width, height, stride = bitmap.width, bitmap.height, bitmap.stride
                raw = bytes(bitmap.buffer)
                channels = bitmap.n_channels
                scan = bytearray()
                for y in range(height):
                    scan.append(0)
                    for x in range(width):
                        pos = y * stride + x * channels
                        if channels >= 3:
                            scan.extend((raw[pos + 2], raw[pos + 1], raw[pos]))
                        else:
                            scan.extend((raw[pos],) * 3)
                def chunk(tag, data):
                    return struct.pack('!I', len(data)) + tag + data + struct.pack('!I', zlib.crc32(tag + data))
                data = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', width, height, 8, 2, 0, 0, 0))
                data += chunk(b'IDAT', zlib.compress(bytes(scan))) + chunk(b'IEND', b'')
                from ..core.project_paths import atomic_bytes
                atomic_bytes(output, data)
                bitmap.close()
                p.close()
    return output
