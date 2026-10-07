#!/usr/bin/env python3
"""把应用图标再画一份成 Windows 的 .ico（同一个形状，只是换个容器）。

为什么单开一个：macOS 认 .icns，Windows 认 .ico，两边不通用。形状不该画两遍，
所以这里直接 import make_icon 里那套画法（同一个圆角方块 + 同一本白书），
只是把结果装进 ICO 容器。

容器本身很短，手写就够了 —— 不为一个图标引 Pillow：ICO 从 Vista 起就允许
每个尺寸直接塞一张 PNG，而 PNG 的字节我们手上本来就有（make_icon.write_png）。

用法: python3 tools/make_ico.py <输出的 .ico 路径>
"""
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_icon  # noqa: E402  (同目录，先补 path 再 import)

# Windows 会挑最接近的一档来用：任务栏 32、桌面 48/256、文件列表 16。
# 256 是资源管理器「大图标」那一档，少了它大图上会发糊。
SIZES = (16, 32, 48, 64, 128, 256)


def build(pngs, out):
    """按 ICO 的规矩把若干张 PNG 拼成一个文件。

    结构：6 字节文件头 + 每张图 16 字节日录项 + 接着各张图的原始字节。
    目录项里的宽高是单字节，所以 256 只能写成 0 —— 这是格式定的，不是笔误。
    """
    head = struct.pack("<HHH", 0, 1, len(pngs))          # 保留位、类型=图标、张数
    offset = 6 + 16 * len(pngs)
    entries, blobs = b"", b""
    for size, data in pngs:
        side = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        blobs += data
    with open(out, "wb") as f:
        f.write(head + entries + blobs)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "归藏.ico"
    pngs = []
    with tempfile.TemporaryDirectory() as tmp:
        for size in SIZES:
            path = os.path.join(tmp, "i%d.png" % size)
            make_icon.write_png(make_icon.draw(size), path)
            with open(path, "rb") as f:
                pngs.append((size, f.read()))
    build(pngs, out)
    print("图标已生成：%s（%d 档：%s）"
          % (out, len(pngs), "、".join(str(s) for s in SIZES)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
