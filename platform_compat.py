#!/usr/bin/env python3
"""平台差异都收在这里，别散落到业务代码里。

这个项目最初只在 macOS 上跑，几处「平台唯一解」是写死的：虚拟环境路径
`.venv/bin/python`、浏览器缓存 `~/Library/Caches/ms-playwright`、「在访达打开」用
`open`、清残留浏览器用 `pkill`、强制结束用 `os.killpg`。

它们在别的系统上不是「报错」，而是**静默什么都不做**（异常被 try 吞掉）——
用户看到的只有「点了按钮没反应」，连个红字都没有。2026-09-30 在 Windows 上
诊断出的就是这个症状：点「连接」→ 后端 Popen 找不到 `.venv/bin/python` →
异常逃出请求处理函数 → 连接被掐断 → 前端 await 直接 reject → 页面毫无反应。
"""
import os
import signal
import subprocess
import sys
import time

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"


def venv_python(repo):
    """项目虚拟环境里的解释器；没有虚拟环境就退回当前解释器。

    环境变量 GUIZANG_PYTHON 最优先（想指定别的解释器时用）。
    Windows 的虚拟环境在 `.venv\\Scripts\\python.exe`，只认 `.venv/bin/python`
    就会让「连接账号 / 取书 / 修复组件」全部起不来子进程。
    """
    override = (os.environ.get("GUIZANG_PYTHON") or "").strip()
    if override and os.path.isfile(override):
        return override
    for parts in (("Scripts", "python.exe"), ("bin", "python")):
        p = os.path.join(repo, ".venv", *parts)
        if os.path.isfile(p):
            return p
    return sys.executable


def ms_playwright_dir():
    """Playwright 浏览器的缓存目录（各平台默认位置不同，也认官方环境变量）。"""
    env = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    if env and env != "0":
        return os.path.expanduser(env)
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "ms-playwright")
    if IS_MAC:
        return os.path.expanduser("~/Library/Caches/ms-playwright")
    return os.path.expanduser("~/.cache/ms-playwright")


def spawn_kwargs():
    """让任务子进程独立成组：中止时只对任务说话，不连服务自己一起打断。

    POSIX 用 start_new_session。Windows 上 start_new_session 会被静默忽略
    （CPython 只在传 preexec_fn 时才报错），要能发 CTRL_BREAK 必须用
    CREATE_NEW_PROCESS_GROUP；子进程共享父进程的控制台，不会多弹一个黑窗口。
    想让任务子进程连控制台都不要，启动时设 GUIZANG_HIDE_WINDOW=1
    （代价见 send_stop 的说明）。
    """
    if not IS_WIN:
        return {"start_new_session": True}
    flags = subprocess.CREATE_NEW_PROCESS_GROUP
    if os.environ.get("GUIZANG_HIDE_WINDOW") == "1":
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return {"creationflags": flags}


def send_stop(proc):
    """先好好说：请它自己收尾（引擎里装了协作式停止，会把在手的章节落盘再退）。

    POSIX 走 SIGTERM。Windows 上 terminate() 等于直接杀进程，没有可捕获的
    SIGTERM；能讲道理的只有同控制台的 CTRL_BREAK（需要子进程建在独立进程组里，
    见 spawn_kwargs）。若控制台事件发不出去（例如设了 GUIZANG_HIDE_WINDOW），
    就退化为 terminate —— 那一下是硬杀，在手未落盘的内容会丢，
    好在引擎每攒够一段就自动落盘，损失有上界。
    """
    sig = getattr(signal, "CTRL_BREAK_EVENT", None) if IS_WIN else signal.SIGTERM
    if sig is not None:
        try:
            proc.send_signal(sig)
            return True
        except Exception:
            pass
    try:
        proc.terminate()
        return True
    except Exception:
        return False


def hard_kill(proc):
    """软的不行才来硬的，而且连子孙一起收。

    只杀 python 不够：浏览器是它的子进程，会继续占着 profile 锁，
    下一轮会话反而起不来。
    """
    if IS_WIN:
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=20)
            return
        except Exception:
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def kill_stray_browsers(profile_dir, wait=1.0):
    """清掉仍占着本项目 profile 的残留浏览器。

    会话卡死或异常退出时，旧浏览器可能没被关掉，而它握着 profile 锁，
    会让下一轮 launch_persistent_context 起不来。按 profile 绝对路径精确匹配，
    不会误伤用户自己开着的浏览器。

    原实现是 `pkill -f`：Windows 没有这个命令，异常被 try 吞掉之后看着「正常」，
    实际锁一直没解开 —— 失败必须能被执行，不能只是不报错。
    """
    target = os.path.abspath(profile_dir)
    try:
        if IS_WIN:
            # 用 .Contains（字面匹配）而不是 -like，免得路径里的 [ ] 被当通配符
            script = ("Get-CimInstance Win32_Process | "
                      "Where-Object { $_.CommandLine -and "
                      "$_.CommandLine.Contains('" + target.replace("'", "''") + "') } | "
                      "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                      "-ErrorAction SilentlyContinue }")
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, timeout=25)
        else:
            subprocess.run(["pkill", "-f", target], capture_output=True, timeout=10)
    except Exception:
        pass
    if wait:
        time.sleep(wait)


def open_in_file_manager(path):
    """在系统文件管理器里打开一个目录。"""
    if IS_WIN:
        os.startfile(path)                     # 仅 Windows 提供
        return
    subprocess.Popen(["open" if IS_MAC else "xdg-open", path])
