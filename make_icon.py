# -*- coding: utf-8 -*-
"""纯 Python 生成 Strata 控制台图标（无需 Pillow）。

设计：深色圆角方块（#131A23→#0B0D11 渐变），Strata=地层 → 三条错层圆角色带
（蓝 / 青 / 深蓝），右上角一颗绿色状态点。
输出：strata.ico（PNG 条目，Vista+）与 strata_256.png 预览。
"""
import struct
import zlib
from pathlib import Path

SS = 1536                    # 超采样画布（能被 256/48/32/16 整除）

BG_TOP = (19, 26, 35)        # #131A23
BG_BOT = (9, 11, 15)         # #0B0D11
B1 = (88, 166, 255)          # #58A6FF 蓝
B2 = (126, 224, 194)         # #7EE0C2 青
B3 = (47, 111, 235)          # #2F6FEB 深蓝
DOT = (61, 203, 133)         # #3DCB85 绿

# (kind, x0, y0, x1, y1, colour)  坐标按 512 画布给
SHAPES = [
    ("rrect", 112, 128, 288, 192, B1),     # 顶条：短
    ("rrect", 112, 224, 400, 288, B2),     # 中条：长
    ("rrect", 112, 320, 344, 384, B3),     # 底条：中
    ("dot",   368,  96, 432, 160, DOT),    # 状态点
]


def clamp01(v):
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


def rrect_alpha(px, py, x0, y0, x1, y1, r):
    cx, cy = (x0 + x1) * 0.5, (y0 + y1) * 0.5
    hw = (x1 - x0) * 0.5 - r
    hh = (y1 - y0) * 0.5 - r
    dx = abs(px - cx) - hw
    if dx < 0.0:
        dx = 0.0
    dy = abs(py - cy) - hh
    if dy < 0.0:
        dy = 0.0
    d = (dx * dx + dy * dy) ** 0.5 - r
    return clamp01(1.5 - d)                # 1.5px 抗锯齿过渡


def dot_alpha(px, py, cx, cy, r):
    dx = px - cx
    dy = py - cy
    d = (dx * dx + dy * dy) ** 0.5 - r
    return clamp01(1.5 - d)


def render_ss():
    """在 SS×SS 画布上渲染，返回 flat bytearray(RGBA)。"""
    k = SS / 512.0
    rad = 112 * k
    pad = 8 * k
    bx0, by0, bx1, by1 = pad, pad, SS - pad, SS - pad
    shapes = [(kind, x0 * k, y0 * k, x1 * k, y1 * k, colour)
              for kind, x0, y0, x1, y1, colour in SHAPES]

    buf = bytearray(SS * SS * 4)
    for sy in range(SS):
        ty = sy / SS
        bg0 = BG_TOP[0] + (BG_BOT[0] - BG_TOP[0]) * ty
        bg1 = BG_TOP[1] + (BG_BOT[1] - BG_TOP[1]) * ty
        bg2 = BG_TOP[2] + (BG_BOT[2] - BG_TOP[2]) * ty
        row = sy * SS * 4
        p2 = sy + 0.5
        for sx in range(SS):
            a = rrect_alpha(sx + 0.5, p2, bx0, by0, bx1, by1, rad)
            if a <= 0.0:
                continue
            i = row + sx * 4
            buf[i] = int(bg0 + 0.5)
            buf[i + 1] = int(bg1 + 0.5)
            buf[i + 2] = int(bg2 + 0.5)
            buf[i + 3] = int(a * 255 + 0.5)
        # 形状只扫自己的包围盒
        for kind, x0, y0, x1, y1, colour in shapes:
            ya = max(0, int(y0) - 3)
            yb = min(SS, int(y1) + 4)
            if not (ya <= sy < yb):
                continue
            xa = max(0, int(x0) - 3)
            xb = min(SS, int(x1) + 4)
            if kind == "rrect":
                rr = 32 * k
            for sx in range(xa, xb):
                p1 = sx + 0.5
                if kind == "rrect":
                    sa = rrect_alpha(p1, p2, x0, y0, x1, y1, rr)
                else:
                    sa = dot_alpha(p1, p2, (x0 + x1) * 0.5, (y0 + y1) * 0.5,
                                   (x1 - x0) * 0.5)
                if sa <= 0.0:
                    continue
                i = row + sx * 4
                da = buf[i + 3] / 255.0
                na = sa + da * (1.0 - sa)
                if na <= 0.0:
                    continue
                inv = da * (1.0 - sa)
                buf[i] = int((colour[0] * sa + buf[i] * inv) / na + 0.5)
                buf[i + 1] = int((colour[1] * sa + buf[i + 1] * inv) / na + 0.5)
                buf[i + 2] = int((colour[2] * sa + buf[i + 2] * inv) / na + 0.5)
                buf[i + 3] = int(na * 255 + 0.5)
    return buf


def downsample(buf, size):
    """SS → size 盒式下采样（SS 是 size 的整数倍）。"""
    f = SS // size
    out = bytearray(size * size * 4)
    for oy in range(size):
        for ox in range(size):
            r_s = g_s = b_s = a_s = 0
            for dy in range(f):
                row = ((oy * f + dy) * SS + ox * f) * 4
                for dx in range(f):
                    i = row + dx * 4
                    r_s += buf[i]; g_s += buf[i + 1]
                    b_s += buf[i + 2]; a_s += buf[i + 3]
            n = f * f
            j = (oy * size + ox) * 4
            out[j] = r_s // n; out[j + 1] = g_s // n
            out[j + 2] = b_s // n; out[j + 3] = a_s // n
    return out


def png_bytes(w, h, rgba):
    def chunk(tag, data):
        c = tag + data
        return (struct.pack(">I", len(data)) + c
                + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    raw = b"".join(b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main():
    here = Path(__file__).parent
    print("渲染超采样画布…")
    ss = render_ss()
    sizes = (256, 64, 48, 32, 16)
    pngs = {}
    for s in sizes:
        print(f"  缩到 {s}×{s}")
        pngs[s] = png_bytes(s, s, downsample(ss, s))
    (here / "strata_256.png").write_bytes(pngs[256])
    entries = b""
    blobs = []
    offset = 6 + 16 * len(sizes)
    for s in sizes:
        data = pngs[s]
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32,
                               len(data), offset)
        blobs.append(data)
        offset += len(data)
    (here / "strata.ico").write_bytes(
        struct.pack("<HHH", 0, 1, len(sizes)) + entries + b"".join(blobs))
    print("OK strata.ico", (here / "strata.ico").stat().st_size, "bytes")


if __name__ == "__main__":
    main()
