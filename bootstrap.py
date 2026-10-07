#!/usr/bin/env python3
"""首次运行：把缺的东西一次补齐。

界面中间那个「我思故我在」按下去，跑的就是这个脚本。分两段，按顺序来：

  前段（缺了就进不了门，失败即停）：
    1. 建虚拟环境（.venv）—— 不动系统里的 Python，装的东西全在项目目录内
    2. 装 Python 依赖（playwright、genanki、pypdf、feedparser、yt-dlp）
    3. 装 Chromium（Playwright 的浏览器，约 368MB）—— 取正文靠它

  后段（视频转笔记那条线的组件，尽力而为，失败不拦门）：
    4. ffmpeg —— 按需下一份静态的放在数据目录里，不装进系统

转写不再有本地模型：1.0.8 起「视频转笔记」只走云端，接口地址与 Key 在设置的
「视频转写」里填，归藏不代管、也不下任何 GB 级权重（见 video_note.py）。
只补缺的那一步，已经有就跳过，所以第二次点它是一秒过。这个脚本自己**只用标准库**
（外加同样只用标准库的 platform_compat），因为第一次跑的时候项目里
什么都还没有，它必须在裸系统 Python 上也能起来。

虚拟环境建在数据目录里（源码直接跑就是项目目录，装成 app 就是用户目录 —— 由壳通过
GUIZANG_DATA 指定），不往应用包里写东西。它不删不改任何已有文件；输出全部走 stdout，
界面那条进展栏会实时显示。
"""
import glob
import os
import subprocess
import sys

import platform_compat as pc

HERE = os.path.dirname(os.path.abspath(__file__))
CHROME_MB = 368

_ENV = None


def say(text=""):
    print(text, flush=True)


def child_env():
    """子进程（pip、playwright）的环境。顺带把系统代理翻成环境变量。

    pip 自己会读系统代理，playwright 不会（它的下载器是 Node，只看
    HTTP_PROXY / HTTPS_PROXY）——见 platform_compat.proxy_env 的说明。
    """
    global _ENV
    if _ENV is None:
        notes = []
        _ENV = pc.proxy_env({**os.environ, "PYTHONUNBUFFERED": "1"}, note=notes)
        for n in notes:
            say(f"      · {n}")
    return _ENV


def run(argv, quiet=False):
    """跑一条命令，把它的输出原样接到自己的 stdout 上，返回退出码。

    用的是流式读而不是 subprocess.run(capture_output)：pip 装 Chromium 要几分钟，
    攒到最后一次性吐出来，界面在那几分钟里会像卡死了。
    """
    if not quiet:
        say(f"$ {' '.join(argv)}")
    env = child_env()
    try:
        proc = subprocess.Popen(
            argv, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, errors="replace", env=env,
        )
    except Exception as e:
        say(f"起不来：{type(e).__name__}: {e}")
        return -1
    for raw in iter(proc.stdout.readline, ""):
        line = raw.rstrip("\n")
        if quiet and line.strip() == "":
            continue
        if not quiet:
            say(line)
    proc.wait()
    return proc.returncode


def python_works(exe, code):
    """拿解释器试一段代码，能跑通返回 True。用来判断依赖装没装。"""
    return run([exe, "-c", code], quiet=True) == 0


def browser_ready():
    """Chromium 装没装。与 ui_server.chromium_ready 同一套判断（认官方环境变量）。"""
    base = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    if not base or base == "0":
        if os.name == "nt":
            base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ms-playwright")
        elif sys.platform == "darwin":
            base = os.path.expanduser("~/Library/Caches/ms-playwright")
        else:
            base = os.path.expanduser("~/.cache/ms-playwright")
    for pat in ("chromium-*", "chromium_headless_shell-*"):
        for d in glob.glob(os.path.join(base, pat)):
            if os.path.isdir(d):
                return True
    return False


def main():
    say("归藏 · 配置运行环境")
    say(f"项目目录：{HERE}")
    say(f"当前解释器：{sys.executable}")
    child_env()          # 先把「用不用系统代理」这条信息亮出来，别等下载卡住才说
    say()

    # ── 1. 虚拟环境 ────────────────────────────────────────────────
    venv = pc.venv_dir(HERE)
    exe = pc.venv_python_only(HERE)
    if exe:
        say("[1/4] 虚拟环境：已有，跳过")
    else:
        say("[1/4] 虚拟环境：正在创建（装的东西都落在这里，不碰系统 Python）")
        say(f"      {venv}")
        run([sys.executable, "-m", "venv", venv])
        exe = pc.venv_python_only(HERE)
        if not exe:
            say()
            say("建虚拟环境没成功。多半是这台机器上的 Python 缺 venv 模块：")
            say("  · Debian / Ubuntu：sudo apt install python3-venv")
            say("  · 其它情况：换成 python.org 下载的官方 Python 再试")
            return 1

    # ── 2. Python 依赖 ────────────────────────────────────────────
    if python_works(exe, "import playwright, genanki, pypdf, feedparser, yt_dlp"):
        say("[2/4] Python 依赖：已装齐，跳过")
    else:
        say("[2/4] Python 依赖：正在安装（playwright、genanki、feedparser、yt-dlp）")
        run([exe, "-m", "pip", "install", "--upgrade", "pip"], quiet=True)
        if run([exe, "-m", "pip", "install", "-r", "requirements.txt"]) != 0:
            say()
            say("装依赖失败，多半是网络。按顺序试：")
            say("  1. 打开你的代理软件，并确认它开着「系统代理」（Clash 里那个开关）")
            say("  2. 再点一次「我思故我在」，它会接着从断的地方装")
            say("  3. 还是不行就手动指定代理，然后用同一条命令自己装：")
            say("     HTTPS_PROXY=http://127.0.0.1:端口 <上面的虚拟环境>/bin/python -m pip install -r requirements.txt")
            return 1

    # ── 3. Chromium ───────────────────────────────────────────────
    if browser_ready():
        say("[3/4] Chromium：已有，跳过")
    else:
        say(f"[3/4] Chromium：正在下载安装（约 {CHROME_MB}MB，这里要几分钟）")
        say("      中途别关这个页面。下载慢的话，挂上代理再点一次会接着下。")
        if run([exe, "-m", "playwright", "install", "chromium"]) != 0:
            say()
            say(f"Chromium 没装上（{CHROME_MB}MB，是这三步里最容易卡的一步）。按顺序试：")
            say("  1. 打开你的代理软件，并确认它开着「系统代理」（Clash 里那个开关）")
            say("  2. 再点一次「我思故我在」，会接着下，不用从头来")
            say("  3. 还是不行就换个下载源，或者手动指定代理后自己跑：")
            say("     HTTPS_PROXY=http://127.0.0.1:端口 <上面的虚拟环境>/bin/python -m playwright install chromium")
            return 1

    # ── 4. ffmpeg ─────────────────────────────────
    # 这是「视频转笔记」的组件。它**不拦门**：前一步的取书、剪藏、订阅、阅读器
    # 全都不需要它，为一次下载失败把人堵在配置页上不划算。缺了就在末尾说清楚，
    # 进门之后在设置里还能补。
    miss = []
    say()
    say("[4/4] ffmpeg：检查中")
    if not pc.IS_MAC:
        say("      跳过（自动下载只服务 macOS，别的系统请自己装一份放进 PATH）")
    else:
        try:
            import ffmpeg_tool
            if ffmpeg_tool.status().get("found"):
                say("      已有，跳过")
            else:
                say("      正在下一份静态的（不装进系统，放在数据目录里）")
                seen = {"s": None}

                def ff_prog(stage, pct=None):
                    if stage != seen["s"]:          # 只在下到新阶段时报一行，别刷屏
                        seen["s"] = stage
                        say("      %s%s" % (stage, ("  %d%%" % pct) if isinstance(pct, int) else ""))

                ffmpeg_tool.ensure(progress=ff_prog)
                say("      就绪")
        except Exception as e:
            miss.append("ffmpeg")
            say("      没下来：%s" % str(e)[:200])

    say()
    if miss:
        say("核心功能已经就绪，可以进去了。「%s」这次没配上（多半是网络）——"
            % "、".join(miss))
        say("进门后在「设置 → 视频转写」里点一下还能补。")
    else:
        say("环境已就绪。转写走云端，接口地址与 Key 在「设置 → 视频转写」里填。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
