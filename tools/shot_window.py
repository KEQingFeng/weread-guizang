"""截一张某个应用的窗口图（只截它的窗口，不动用户桌面上别的东西）。

screencapture -l<窗口号> 需要一个 CGWindowID。这里按进程名问 WindowServer 要，
然后只截那一个窗口 —— 比全屏截图规矩，不会把用户桌面上的其他内容拍进去。

用法: python3 tools/shot_window.py <进程名> <输出.png>
"""
import subprocess
import sys

import Quartz


def window_id(owner):
    """按 owner 名找最靠前的一个有尺寸的窗口。"""
    infos = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID)
    for w in infos or []:
        if (w.get("kCGWindowOwnerName") or "") == owner:
            b = w.get("kCGWindowBounds") or {}
            if b.get("Width", 0) > 200 and b.get("Height", 0) > 200:
                return w.get("kCGWindowNumber")
    return None


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    owner, out = sys.argv[1], sys.argv[2]
    wid = window_id(owner)
    if wid is None:
        print(f"没找到 {owner} 的窗口")
        return 1
    rc = subprocess.run(["/usr/sbin/screencapture", "-x", "-o", f"-l{wid}", out],
                        capture_output=True, text=True)
    if rc.returncode != 0:
        print("截图失败：" + (rc.stderr or "").strip())
        return 1
    print(f"窗口 {wid} → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
