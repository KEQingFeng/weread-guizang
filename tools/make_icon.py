#!/usr/bin/env python3
"""画一个应用图标（.icns）：蓝色圆角方块 + 白色摊开的书。

和界面上那个标记同一个形状（ui.html 里的 .mark 用的就是这条路径），
只是放大到图标尺寸。纯 CoreGraphics 画，不依赖任何素材文件。

用法: python3 tools/make_icon.py <输出的 .icns 路径>
"""
import os
import subprocess
import sys

import Quartz
from Foundation import NSURL

# 书的轮廓，坐标系与 ui.html 里那个 SVG 一致（viewBox 0 0 20 20，y 向下）
BOOK = [
    ("M", 10.0, 4.6),
    ("C", 8.4, 3.4, 6.3, 3.1, 3.6, 3.4),
    ("L", 3.6, 14.7),
    ("C", 6.3, 14.4, 8.4, 14.7, 10.0, 15.9),
    ("C", 11.6, 14.7, 13.7, 14.4, 16.4, 14.7),
    ("L", 16.4, 3.4),
    ("C", 13.7, 3.1, 11.6, 3.4, 10.0, 4.6),
    ("Z",),
    ("M", 10.0, 4.6),
    ("L", 10.0, 15.9),
]

GLYPH_BOX = (3.6, 3.1, 16.4, 15.9)        # 书占的矩形（x0, y0, x1, y1）
TOP_RGB = (0.294, 0.576, 0.949)           # #4b93f2
BOT_RGB = (0.145, 0.408, 0.800)           # #2568cc


def gradient(stops):
    """按色标（(r,g,b,a) 的序列）造一条渐变，位置平均分。

    为什么不用 CGGradientCreateWithColorComponents：那条路的颜色分量是一个 C 数组
    （`const CGFloat *`），PyObjC 拿到的却是一个 Python 元组 —— 它并不保证把元组
    按浮点数组递过去。实测同一段代码在一台机器上前后两次就画出了不同的东西：一次
    是想要的那条蓝渐变（应用图标的 icns 就是这么来的），一次整条渐变什么都没画
    （背景全透明）、还有一次背景是一层花绿的垃圾字节。也就是说图标长什么样，之前
    是靠运气的。CGColor 数组这条路有明确的对象类型可以映射，实测每次都一样。
    """
    cs = Quartz.CGColorSpaceCreateDeviceRGB()
    colors = [Quartz.CGColorCreateGenericRGB(*c) for c in stops]
    arr = Quartz.CFArrayCreate(None, colors, len(colors), None)
    locs = tuple(i / float(len(stops) - 1) for i in range(len(stops)))
    return Quartz.CGGradientCreateWithColors(cs, arr, locs)


def draw(size):
    """画一张 size×size 的图，返回 CGImage。"""
    cs = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(None, size, size, 8, 0, cs,
                                       Quartz.kCGImageAlphaPremultipliedLast)

    # 圆角方块：留一点边距，圆角比例照 Big Sur 那套
    inset = size * 0.085
    side = size - inset * 2
    rect = Quartz.CGRectMake(inset, inset, side, side)
    squircle = Quartz.CGPathCreateWithRoundedRect(rect, side * 0.2237, side * 0.2237, None)

    Quartz.CGContextSaveGState(ctx)
    Quartz.CGContextAddPath(ctx, squircle)
    Quartz.CGContextClip(ctx)
    grad = gradient([TOP_RGB + (1.0,), BOT_RGB + (1.0,)])
    Quartz.CGContextDrawLinearGradient(
        ctx, grad,
        Quartz.CGPointMake(rect.origin.x, rect.origin.y + rect.size.height),
        Quartz.CGPointMake(rect.origin.x + rect.size.width, rect.origin.y),
        0)

    # 顶部一层很淡的高光，不然纯渐变会显得平
    gloss = gradient([(1.0, 1.0, 1.0, 0.20), (1.0, 1.0, 1.0, 0.0)])
    Quartz.CGContextDrawLinearGradient(
        ctx, gloss,
        Quartz.CGPointMake(rect.origin.x, rect.origin.y + rect.size.height),
        Quartz.CGPointMake(rect.origin.x, rect.origin.y + rect.size.height * 0.42),
        0)
    Quartz.CGContextRestoreGState(ctx)

    # 书：把 SVG 坐标映到图标中间。SVG 的 y 向下，CoreGraphics 的 y 向上，
    # 所以 d 取负号 —— 不翻的话书会倒过来。
    x0, y0, x1, y1 = GLYPH_BOX
    s = side * 0.50 / max(x1 - x0, y1 - y0)
    ox = size / 2 - (x0 + x1) / 2 * s
    oy = size / 2 + (y0 + y1) / 2 * s
    Quartz.CGContextConcatCTM(
        ctx, Quartz.CGAffineTransformMake(s, 0.0, 0.0, -s, ox, oy))

    path = Quartz.CGPathCreateMutable()
    for seg in BOOK:
        if seg[0] == "M":
            Quartz.CGPathMoveToPoint(path, None, seg[1], seg[2])
        elif seg[0] == "L":
            Quartz.CGPathAddLineToPoint(path, None, seg[1], seg[2])
        elif seg[0] == "C":
            Quartz.CGPathAddCurveToPoint(path, None,
                                         seg[1], seg[2], seg[3], seg[4], seg[5], seg[6])
        else:
            Quartz.CGPathCloseSubpath(path)

    Quartz.CGContextSetStrokeColorWithColor(
        ctx, Quartz.CGColorCreateGenericRGB(1.0, 1.0, 1.0, 1.0))
    Quartz.CGContextSetLineWidth(ctx, 1.45)
    Quartz.CGContextSetLineCap(ctx, Quartz.kCGLineCapRound)
    Quartz.CGContextSetLineJoin(ctx, Quartz.kCGLineJoinRound)
    Quartz.CGContextAddPath(ctx, path)
    Quartz.CGContextStrokePath(ctx)

    return Quartz.CGBitmapContextCreateImage(ctx)


def write_png(img, path):
    url = NSURL.fileURLWithPath_(path)
    dest = Quartz.CGImageDestinationCreateWithURL(url, "public.png", 1, None)
    Quartz.CGImageDestinationAddImage(dest, img, None)
    Quartz.CGImageDestinationFinalize(dest)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "icon.icns"
    iconset = os.path.join(os.path.dirname(os.path.abspath(out)), "归藏.iconset")
    os.makedirs(iconset, exist_ok=True)

    # 同一个尺寸画一次、给多个文件名复用，免得重复渲染
    cache = {}
    jobs = [
        (16, "icon_16x16.png"), (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"), (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"), (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"), (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"), (1024, "icon_512x512@2x.png"),
    ]
    for size, name in jobs:
        if size not in cache:
            cache[size] = draw(size)
        write_png(cache[size], os.path.join(iconset, name))

    rc = subprocess.run(["/usr/bin/iconutil", "-c", "icns", iconset, "-o", out],
                        capture_output=True, text=True)
    if rc.returncode != 0:
        print("iconutil 失败：" + (rc.stderr or "").strip(), file=sys.stderr)
        return 1
    print(f"图标已生成：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
