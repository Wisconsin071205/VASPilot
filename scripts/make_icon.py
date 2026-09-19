"""Generate src/vaspilot/desktop/assets/icon.ico with the standard library.

Deterministic: rerunning produces byte-identical output.  The mark is a
rounded deep-blue tile with a white chevron ("V" for VASP) and a small
accent dot — readable down to 16 px.  Each size is rendered directly with
4x4 supersampling, then stored as a PNG inside one ICO container (PNG-in-ICO
is supported by Windows Vista and later for every size).

Usage:  python scripts/make_icon.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = (Path(__file__).resolve().parents[1] / "src" / "vaspilot" / "desktop"
       / "assets" / "icon.ico")
SIZES = (256, 48, 32, 16)
BLUE = (28, 62, 122)      # tile
WHITE = (255, 255, 255)   # chevron
AMBER = (247, 181, 41)    # accent dot
SUPERSAMPLE = 4


def _inside_tile(x: float, y: float) -> bool:
    """Rounded square covering [0.04, 0.96] with corner radius 0.2."""
    left, right, radius = 0.04, 0.96, 0.2
    if not (left <= x <= right and left <= y <= right):
        return False
    cx = min(max(x, left + radius), right - radius)
    cy = min(max(y, left + radius), right - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def _inside_chevron(x: float, y: float) -> bool:
    """Two strokes from the top corners meeting at the bottom centre."""
    half_width = 0.11
    top, bottom = 0.26, 0.74
    if not (top <= y <= bottom):
        return False
    t = (y - top) / (bottom - top)           # 0 at top, 1 at the apex
    left_centre = 0.28 + t * (0.5 - 0.28)
    right_centre = 0.72 - t * (0.72 - 0.5)
    return (abs(x - left_centre) <= half_width
            or abs(x - right_centre) <= half_width)


def _inside_dot(x: float, y: float) -> bool:
    return (x - 0.5) ** 2 + (y - 0.82) ** 2 <= 0.045 ** 2


def _sample(x: float, y: float) -> tuple[int, int, int, int]:
    if not _inside_tile(x, y):
        return (0, 0, 0, 0)
    if _inside_dot(x, y):
        return (*AMBER, 255)
    if _inside_chevron(x, y):
        return (*WHITE, 255)
    return (*BLUE, 255)


def render(size: int) -> bytes:
    """RGBA scanlines (with PNG filter byte 0) for one square image."""
    rows = bytearray()
    steps = SUPERSAMPLE
    for py in range(size):
        rows.append(0)
        for px in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(steps):
                for sx in range(steps):
                    x = (px + (sx + 0.5) / steps) / size
                    y = (py + (sy + 0.5) / steps) / size
                    r, g, b, a = _sample(x, y)
                    acc[0] += r * a
                    acc[1] += g * a
                    acc[2] += b * a
                    acc[3] += a
            n = steps * steps
            alpha = acc[3] // n
            if acc[3]:
                rows += bytes((acc[0] // acc[3], acc[1] // acc[3],
                               acc[2] // acc[3], alpha))
            else:
                rows += b"\x00\x00\x00\x00"
    return bytes(rows)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return (struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", crc))


def png(size: int) -> bytes:
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    body = zlib.compress(render(size), 9)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", body) + _chunk(b"IEND", b""))


def ico(images: list[tuple[int, bytes]]) -> bytes:
    directory = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    offset = 6 + 16 * len(images)
    blobs = bytearray()
    for size, data in images:
        dim = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32,
                                 len(data), offset + len(blobs))
        blobs += data
    return bytes(directory + blobs)


def main() -> None:
    data = ico([(size, png(size)) for size in SIZES])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(data)
    print(f"wrote {OUT} ({len(data)} bytes, sizes {SIZES})")


if __name__ == "__main__":
    main()
