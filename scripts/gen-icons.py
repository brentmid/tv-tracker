#!/usr/bin/env python3
"""Generate server/assets/apple-touch-icon.png (180x180) with stdlib only.

iOS home-screen icons must be PNG (SVG isn't supported for
apple-touch-icon), and this repo deliberately has no image dependencies —
so this draws the same flat TV glyph as assets/favicon.svg into a minimal
hand-rolled PNG. Rerun after changing the design; the output is
deterministic and committed.
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

SIZE = 180
BG = (0, 0, 0)
CYAN = (76, 194, 255)      # --accent from style.css
SCREEN = (13, 27, 36)      # near-black blue screen inset

OUT = Path(__file__).resolve().parent.parent / "server" / "assets" / "apple-touch-icon.png"


def chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data)))


def write_png(path: Path, pixels: list[list[tuple[int, int, int]]]) -> None:
    height = len(pixels)
    width = len(pixels[0])
    raw = b"".join(
        b"\x00" + bytes(c for px in row for c in px) for row in pixels)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b""))


def draw() -> list[list[tuple[int, int, int]]]:
    px = [[BG] * SIZE for _ in range(SIZE)]

    def rect(x0, y0, x1, y1, color, radius=0):
        for y in range(y0, y1):
            for x in range(x0, x1):
                if radius:
                    # rounded corners: outside the corner circles -> skip
                    cx = min(max(x, x0 + radius), x1 - radius - 1)
                    cy = min(max(y, y0 + radius), y1 - radius - 1)
                    if (x - cx) ** 2 + (y - cy) ** 2 > radius ** 2:
                        continue
                px[y][x] = color

    # antennas: stepped diagonals from the top toward the body
    for t in range(32):
        y = 26 + t
        lx = 66 + (20 * t) // 31
        rx = 106 - (20 * t) // 31 + 8
        for x in range(lx, lx + 8):
            px[y][x] = CYAN
        for x in range(rx, rx + 8):
            px[y][x] = CYAN
    # TV body with a screen inset (leaves a 14px cyan bezel)
    rect(24, 58, 156, 148, CYAN, radius=14)
    rect(38, 72, 142, 134, SCREEN)
    # feet
    rect(44, 148, 64, 158, CYAN)
    rect(116, 148, 136, 158, CYAN)
    return px


if __name__ == "__main__":
    write_png(OUT, draw())
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    sys.exit(0)
