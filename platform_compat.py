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
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse
from urllib.request import getproxies

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"


def data_dir(repo):
    """数据（导出的书、缓存、虚拟环境）该放哪。

    源码直接跑时就是项目目录本身，行为和以前一模一样。装成 app 之后源码在应用包
    里面 —— 那儿不该被写，放进 /Applications 后还可能真的只读。所以壳会把
    GUIZANG_DATA 指到用户目录，需要写的东西全都跟着它走，包体自始至终是干净的。
    """
    d = (os.environ.get("GUIZANG_DATA") or "").strip()
    return os.path.expanduser(d) if d else repo


APP_TITLE = "归藏"


def books_dir(repo):
    """取回的书落在哪（界面「书架」读的就是这里）。

    默认跟数据目录走：源码直接跑时就是项目下的 output/，行为与从前一模一样。
    装成 app 之后壳会把 GUIZANG_BOOKS 指向用户看得见的「文档/归藏」——
    取过的书、图片、合并稿全在那儿，用户能直接在访达里翻，不用钻进
    ~/Library/Application Support 这种藏起来的地方。
    """
    d = (os.environ.get("GUIZANG_BOOKS") or "").strip()
    if d:
        return os.path.expanduser(d)
    return os.path.join(data_dir(repo), "output")


def default_books_place():
    """首次安装时给你的书库位置：用户文档目录下的「归藏」。

    Windows 与 macOS 都在「文档」里；其余平台退回用户主目录，别乱猜。
    """
    home = os.path.expanduser("~")
    if IS_WIN or IS_MAC:
        return os.path.join(home, "Documents", APP_TITLE)
    return os.path.join(home, APP_TITLE)


def output_dir():
    """引擎（export_precise.py）该把书往哪写。

    引擎里 output/ 是相对 cwd 的，壳/后端通过 GUIZANG_OUTPUT 把它指到
    books_dir；没设就退回原来的相对路径，源码直接跑不受影响。
    """
    d = (os.environ.get("GUIZANG_OUTPUT") or "").strip()
    return os.path.expanduser(d) if d else "output"


def venv_dir(repo):
    """虚拟环境的位置。跟 data_dir 走，不跟源码走。"""
    return os.path.join(data_dir(repo), ".venv")


def venv_python_only(repo):
    """虚拟环境里的解释器；还没建就是 None。

    Windows 的虚拟环境在 `.venv\\Scripts\\python.exe`，其余平台在 `bin/python`。
    只认 `.venv/bin/python` 会让 Windows 上所有「起子进程」的功能一点就哑火。

    与 venv_python 分开，是因为「建环境」那一步想知道的是「有没有」，
    而不是「没有的话拿谁顶上」—— 后者会把 sys.executable 混进来，
    让人误以为环境已经就绪。
    """
    base = venv_dir(repo)
    for parts in (("Scripts", "python.exe"), ("bin", "python")):
        p = os.path.join(base, *parts)
        if os.path.isfile(p):
            return p
    return None


def venv_python(repo):
    """该用哪个解释器跑项目脚本：虚拟环境优先，没有就退回当前解释器。

    环境变量 GUIZANG_PYTHON 最优先（想指定别的解释器时用）。
    """
    override = (os.environ.get("GUIZANG_PYTHON") or "").strip()
    if override and os.path.isfile(override):
        return override
    return venv_python_only(repo) or sys.executable


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


def _proxy_alive(url, timeout=0.7):
    """代理地址的端口现在通不通。"""
    try:
        u = urlparse(url if "://" in url else "http://" + url)
        host = u.hostname
        port = u.port or (443 if u.scheme == "https" else 80)
        if not host:
            return False
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def proxy_env(base=None, note=None):
    """把系统代理翻成 HTTP(S)_PROXY 环境变量，交给子进程。

    为什么非要做这一步：pip 走 requests，在 macOS / Windows 上会自己去读系统代理；
    可 playwright 下载 Chromium 是它自带的 Node 在干活，只认 HTTP_PROXY /
    HTTPS_PROXY 这两个环境变量，压根不看系统设置。于是「浏览器能上网、命令行
    不挂代理」的机器上，pip 装得好好的，轮到 Chromium 就下不动 —— 而这恰好是
    国内最常见的配置（Clash 只开系统代理，不往 shell 里写 export）。

    两个保险：
      · 环境里已经有 HTTP(S)_PROXY 就原样尊重，不覆盖（CI 或用户显式指定时别添乱）；
      · 探一下代理端口通不通，不通就不写。否则用户关了代理却留着系统设置，
        本来直连能下成的东西，会被我们注入的死地址连累到全失败。
    """
    env = dict(os.environ if base is None else base)
    if any((env.get(k) or "").strip()
           for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")):
        return env
    try:
        p = getproxies()
    except Exception:
        return env
    # 只认 http/https。socks 代理 pip 要额外装 PySocks，playwright 更是不支持 ——
    # 塞进去只会把本来能走通的直连也搞坏。
    http = (p.get("http") or "").strip()
    https = (p.get("https") or "").strip()
    if not http and not https:
        return env
    probe = https or http
    if not _proxy_alive(probe):
        if note is not None:
            note.append(f"系统里配了代理 {probe}，但它现在连不上，本次不用它")
        return env
    if http:
        env["HTTP_PROXY"] = http
    if https:
        env["HTTPS_PROXY"] = https
    if note is not None:
        note.append(f"已顺带把系统代理 {probe} 传给下载步骤")
    return env


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
