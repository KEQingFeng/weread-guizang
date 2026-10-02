# -*- coding: utf-8 -*-
"""ffmpeg_tool 的离线自测：一行一条结论，失败就非零退出。

默认什么都不下 —— 真下载走网络、又慢又看运气，不该混进每次都能跑的门禁。
要真验一遍下载源，加 GUIZANG_FFMPEG_LIVE=1，它会把那份下到本次的临时沙盒里
（GUIZANG_DATA 指过去），绝不碰用户真实的 cache/ 和 ~/Library。

跑法：.venv/bin/python tests/check_ffmpeg_tool.py
     GUIZANG_FFMPEG_LIVE=1 .venv/bin/python tests/check_ffmpeg_tool.py   # 顺带真下
"""
import atexit
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ffmpeg_tool  # noqa: E402

FAIL = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if not cond:
        FAIL.append(name)


# 整个套件跑在一个临时沙盒里：GUIZANG_DATA 一指，tools_dir() 就落到沙盒里了，
# 不会去动用户真实的缓存目录。
SANDBOX = tempfile.mkdtemp(prefix="gz-ffmpeg-check-")
atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))
os.environ["GUIZANG_DATA"] = os.path.join(SANDBOX, "data")

# 真实机器上可能装着 ffmpeg（/opt/homebrew 之类）；这一组断言要验的是「没有」，
# 所以先把常见目录清空、PATH 收到一个空目录里，让环境可控。
saved_path = os.environ.get("PATH")
saved_common = ffmpeg_tool.COMMON_DIRS
ffmpeg_tool.COMMON_DIRS = ()
empty_bin = os.path.join(SANDBOX, "empty")
os.makedirs(empty_bin, exist_ok=True)
os.environ["PATH"] = empty_bin


# ── 1. status() 在「哪都没有 ffmpeg」的机器上也不抛，键齐全 ──────────
st = ffmpeg_tool.status()
chk("status() 返回 dict 且不抛", isinstance(st, dict), type(st))
chk("status() 五个键齐全",
    set(st.keys()) == {"found", "path", "ffprobe", "source", "version"}, sorted(st.keys()))
chk("没有 ffmpeg 时 found=False、source 为空",
    st["found"] is False and st["source"] == "" and st["path"] is None, st)


# ── 2. PATH 里放一个假 ffmpeg，应该被认成 system ────────────────────
bindir = os.path.join(SANDBOX, "bin")
os.makedirs(bindir, exist_ok=True)
stub = os.path.join(bindir, "ffmpeg")
with open(stub, "w", encoding="utf-8") as fh:
    fh.write("#!/bin/sh\necho 'ffmpeg version 9.9-stub'\n")
os.chmod(stub, 0o755)
os.environ["PATH"] = bindir + os.pathsep + (saved_path or "")
try:
    st = ffmpeg_tool.status()
    chk("PATH 里的 ffmpeg 能被认出来", st["found"] is True and st["source"] == "system", st)
    chk("认出来的正是摆进去的那个 stub", st["path"] == stub, st["path"])
    chk("版本号是从 -version 输出里读的", st["version"].startswith("9.9"), st["version"])
finally:
    os.environ["PATH"] = empty_bin


# ── 3. 空 PATH + 没下过，ffmpeg_path() 抛中文 ValueError 且说了怎么补 ──
msg = None
try:
    ffmpeg_tool.ffmpeg_path()
    msg = "__没有抛异常__"
except ValueError as e:
    msg = str(e)
except Exception as e:  # noqa: BLE001
    msg = "__抛错了：%s__" % type(e).__name__
chk("没有 ffmpeg 时 ffmpeg_path() 抛 ValueError",
    msg is not None and not msg.startswith("__"), msg)
chk("错话是中文", bool(msg) and bool(re.search(r"[\u4e00-\u9fff]", msg)), msg)
chk("错话说了怎么把 ffmpeg 补上",
    bool(msg) and ("装" in msg or "下载" in msg or "PATH" in msg), msg)


# ── 4. extract_audio() 拼出来的命令行（不跑 ffmpeg，记账桩） ───────────
recorded = {}
saved_ffmpeg_path = ffmpeg_tool.ffmpeg_path
saved_run = ffmpeg_tool._run


def fake_path():
    return "/fake/ffmpeg"


def fake_run(argv):
    recorded["argv"] = list(argv)
    return None


ffmpeg_tool.ffmpeg_path = fake_path
ffmpeg_tool._run = fake_run
try:
    out = ffmpeg_tool.extract_audio("in.mp4", "out.mp3", fmt="mp3", bitrate="128k")
finally:
    ffmpeg_tool.ffmpeg_path = saved_ffmpeg_path
    ffmpeg_tool._run = saved_run

argv = recorded.get("argv") or []
chk("extract_audio() 返回产物路径", out == "out.mp3", out)
chk("命令行首段来自 ffmpeg_path()", bool(argv) and argv[0] == "/fake/ffmpeg", argv)
chk("输入与产物都在命令里", "in.mp4" in argv and argv[-1] == "out.mp3", argv)
chk("丢了视频轨（-vn）", "-vn" in argv, argv)
chk("码率用的就是传进去那个",
    "-b:a" in argv and argv[argv.index("-b:a") + 1] == "128k", argv)
chk("mp3 选了 libmp3lame", "libmp3lame" in argv, argv)


# ── 5. 什么都没下过时 remove() 是空操作，返回 False ──────────────────
shutil.rmtree(ffmpeg_tool.tools_dir(), ignore_errors=True)
chk("没下过时 remove() 返回 False 且不报错", ffmpeg_tool.remove() is False, None)

# 顺手验一下反向：沙盒里摆一份，remove() 应该真删掉并返回 True。
os.makedirs(ffmpeg_tool.tools_dir(), exist_ok=True)
planted = os.path.join(ffmpeg_tool.tools_dir(), "ffmpeg")
with open(planted, "w", encoding="utf-8") as fh:
    fh.write("stub")
chk("下过之后 remove() 真删并返回 True",
    ffmpeg_tool.remove() is True and not os.path.exists(planted), None)


# ── 6. ensure_on_path()：把归藏下那份的目录补进 PATH（给 mlx-whisper 用） ──
# 这条线的由来是一个真实故障：组件装好了、ffmpeg 也能跑，可 mlx-whisper 内部
# shell 的是裸 'ffmpeg'，而 cache/tools/ 不在 PATH 上，于是转写报
# No such file or directory: 'ffmpeg'。这里只验「补 PATH」这个动作本身。
os.environ["PATH"] = empty_bin
chk("没下过时 ensure_on_path() 返回 False", ffmpeg_tool.ensure_on_path() is False, None)

os.makedirs(ffmpeg_tool.tools_dir(), exist_ok=True)
bundled = os.path.join(ffmpeg_tool.tools_dir(), "ffmpeg")
with open(bundled, "w", encoding="utf-8") as fh:
    fh.write("#!/bin/sh\necho 'ffmpeg version 9.8-bundled'\n")
os.chmod(bundled, 0o755)
try:
    chk("有自己那份时 ensure_on_path() 返回 True", ffmpeg_tool.ensure_on_path() is True, None)
    chk("补进去的是 tools 目录，且排在最前面",
        os.environ["PATH"].split(os.pathsep)[0] == ffmpeg_tool.tools_dir(), os.environ["PATH"])
    chk("补完之后 shutil.which 能按名字找到 ffmpeg",
        shutil.which("ffmpeg") == bundled, shutil.which("ffmpeg"))
    chk("再调一次是幂等的（返回 False，不改动 PATH）",
        ffmpeg_tool.ensure_on_path() is False, os.environ["PATH"])
    # 系统里本来就有的那份不该被顶掉：来源是 system 时不动 PATH。
    os.remove(bundled)
    os.environ["PATH"] = empty_bin + os.pathsep + bindir
    before = os.environ["PATH"]
    chk("系统里那份（非 bundled）不动 PATH",
        ffmpeg_tool.ensure_on_path() is False and os.environ["PATH"] == before, os.environ["PATH"])
finally:
    os.environ["PATH"] = empty_bin
    shutil.rmtree(ffmpeg_tool.tools_dir(), ignore_errors=True)


# ── 7. 可选：真下走一遍（联网，默认关闭） ────────────────────────────
if os.environ.get("GUIZANG_FFMPEG_LIVE") == "1":
    print("（GUIZANG_FFMPEG_LIVE=1：真下到沙盒里，不碰用户真实数据目录）")

    def show(stage, pct):
        print("    %s %s" % (stage, "" if pct is None else "%d%%" % pct))

    live = ffmpeg_tool.ensure(progress=show)
    chk("真下：ensure() 返回可执行的 ffmpeg",
        os.path.isfile(live) and os.access(live, os.X_OK), live)
    chk("真下：status() 认这份是 bundled",
        ffmpeg_tool.status()["source"] == "bundled", ffmpeg_tool.status())
    chk("真下：ffprobe 也一起备好了",
        bool(ffmpeg_tool.status()["ffprobe"]), ffmpeg_tool.status())

# 收摊：把 PATH 与常量还原，免得影响同进程里后面的东西。
os.environ["PATH"] = saved_path if saved_path is not None else ""
ffmpeg_tool.COMMON_DIRS = saved_common

print()
if FAIL:
    print("失败 %d 项：%s" % (len(FAIL), "、".join(FAIL)))
    sys.exit(1)
print("全部通过")
