from __future__ import annotations

import struct
import zlib
from pathlib import Path

PAPER = (243, 238, 228, 255)
CARD = (255, 253, 248, 255)
INK = (28, 23, 19, 255)
STAMP = (194, 59, 34, 255)


def _png(pixels: list[list[tuple[int, int, int, int]]]) -> bytes:
    height = len(pixels)
    width = len(pixels[0])
    raw = bytearray()
    for row in pixels:
        raw.append(0)
        for r, g, b, a in row:
            raw.extend((r, g, b, a))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            chunk(b"IHDR", ihdr),
            chunk(b"IDAT", zlib.compress(bytes(raw), 9)),
            chunk(b"IEND", b""),
        ]
    )


def _blend(dst, src):
    sr, sg, sb, sa = src
    if sa >= 255:
        return src
    if sa <= 0:
        return dst
    dr, dg, db, da = dst
    a = sa / 255
    return (
        int(dr + (sr - dr) * a),
        int(dg + (sg - dg) * a),
        int(db + (sb - db) * a),
        255,
    )


def _fill_rect(grid, x0, y0, x1, y1, color):
    h = len(grid)
    w = len(grid[0])
    for y in range(max(0, y0), min(h, y1)):
        row = grid[y]
        for x in range(max(0, x0), min(w, x1)):
            row[x] = color


def _stroke_rect(grid, x0, y0, x1, y1, color, width=2):
    _fill_rect(grid, x0, y0, x1, y0 + width, color)
    _fill_rect(grid, x0, y1 - width, x1, y1, color)
    _fill_rect(grid, x0, y0, x0 + width, y1, color)
    _fill_rect(grid, x1 - width, y0, x1, y1, color)


def _fill_circle(grid, cx, cy, radius, color, ring=None):
    h = len(grid)
    w = len(grid[0])
    r2 = radius * radius
    inner2 = (radius - (ring or 0)) ** 2 if ring else None
    for y in range(max(0, int(cy - radius)), min(h, int(cy + radius) + 1)):
        dy = y + 0.5 - cy
        for x in range(max(0, int(cx - radius)), min(w, int(cx + radius) + 1)):
            dx = x + 0.5 - cx
            d2 = dx * dx + dy * dy
            if d2 > r2:
                continue
            if inner2 is not None and d2 < inner2:
                continue
            grid[y][x] = _blend(grid[y][x], color)


def _paint(size: int, maskable: bool) -> list[list[tuple[int, int, int, int]]]:
    grid = [[PAPER for _ in range(size)] for _ in range(size)]
    pad = int(size * (0.18 if maskable else 0.10))
    card_x0, card_y0 = pad + int(size * 0.06), pad + int(size * 0.04)
    card_x1, card_y1 = size - pad - int(size * 0.06), size - pad - int(size * 0.08)
    _fill_rect(grid, card_x0, card_y0, card_x1, card_y1, CARD)
    _stroke_rect(grid, card_x0, card_y0, card_x1, card_y1, INK, max(2, size // 80))
    photo_y1 = card_y0 + int((card_y1 - card_y0) * 0.72)
    _fill_rect(
        grid,
        card_x0 + size // 32,
        card_y0 + size // 32,
        card_x1 - size // 32,
        photo_y1,
        (109, 123, 85, 255),
    )
    bar_h = max(6, size // 18)
    _fill_rect(
        grid,
        card_x0 + size // 16,
        card_y0 + size // 16,
        card_x0 + size // 16 + int(size * 0.28),
        card_y0 + size // 16 + bar_h,
        STAMP,
    )
    cam_w = int(size * (0.34 if maskable else 0.38))
    cam_h = int(size * (0.22 if maskable else 0.24))
    cam_x0 = card_x1 - int(size * 0.08) - cam_w
    cam_y0 = photo_y1 - int(cam_h * 0.35)
    bump_w = int(cam_w * 0.38)
    bump_h = max(4, int(size * 0.055))
    bump_x0 = cam_x0 + (cam_w - bump_w) // 2
    _fill_rect(grid, bump_x0, cam_y0 - bump_h + 2, bump_x0 + bump_w, cam_y0 + 2, STAMP)
    _fill_rect(grid, cam_x0, cam_y0, cam_x0 + cam_w, cam_y0 + cam_h, STAMP)
    flash_w = max(4, int(size * 0.045))
    flash_h = max(3, int(size * 0.032))
    _fill_rect(
        grid,
        cam_x0 + max(4, size // 40),
        cam_y0 + max(4, size // 36),
        cam_x0 + max(4, size // 40) + flash_w,
        cam_y0 + max(4, size // 36) + flash_h,
        CARD,
    )
    lens_cx = cam_x0 + int(cam_w * 0.62)
    lens_cy = cam_y0 + cam_h // 2
    _fill_circle(grid, lens_cx, lens_cy, cam_h * 0.38, CARD)
    _fill_circle(grid, lens_cx, lens_cy, cam_h * 0.22, INK)
    return grid


def ensure_icons(static_dir: Path) -> None:
    folder = static_dir / "icons"
    folder.mkdir(parents=True, exist_ok=True)
    targets = {
        "icon-192.png": (192, False),
        "icon-512.png": (512, False),
        "apple-touch-180.png": (180, False),
        "icon-512-maskable.png": (512, True),
    }
    for name, (size, maskable) in targets.items():
        path = folder / name
        if path.exists() and path.stat().st_size > 100:
            continue
        path.write_bytes(_png(_paint(size, maskable)))
