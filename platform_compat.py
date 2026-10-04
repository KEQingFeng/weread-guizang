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
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
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


def runtime_file(repo):
    """服务当前在哪个端口上：起来的时候写，别人（MCP 适配器、壳）来查。

    为什么要有这个文件：8770 被占时服务会自己往后找一个空闲端口（最多 20 个），
    而调用方只认 8770。端口一错开，适配器探的就是一个没人应答的口子，
    用户看到的却是「归藏没开着」。写盘的是服务自己，它知道自己最终绑到了哪个口。

    跟 data_dir 走而不是跟源码走：装成 app 之后包体是只读的。
    """
    return os.path.join(data_dir(repo), "runtime.json")


def write_runtime(repo, port, pid=None, version="", code=""):
    """记下当前端口。写失败不影响服务本身，所以只尽力而为，不抛异常。"""
    try:
        os.makedirs(data_dir(repo), exist_ok=True)
        tmp = runtime_file(repo) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"port": int(port), "pid": int(pid or os.getpid()),
                       "version": version, "code": code,
                       "started_at": int(time.time())}, f)
        os.replace(tmp, runtime_file(repo))   # 先写临时再改名：别让读到半截的 JSON
        return True
    except Exception:
        return False


def code_fingerprint(path):
    """这份后端代码的指纹（内容变了就变），用来认出「端口上那个进程是哪份代码」。

    为什么不用版本号比：源码直接跑的人改完代码重启，版本号多半还没跟着改，
    于是旧进程和新代码写着同一个 1.0.1，谁也不认谁是旧的。2026-10-04 用户报
    「画板、思维导图点了没反应」，真因就是这个：界面对象是从磁盘现读的新的，
    路由表是进程起来那刻装进内存的旧的，新接口一律 404。
    """
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()[:12]
    except OSError:
        return ""


def port_is_heard(port, timeout=1.2):
    """127.0.0.1:port 有没有人听。通回 True，不通回 False，别把「连不上」当成「活着」。

    这函数原先叫 _free_port —— 名字说的和做的正好相反（True 是「不 free」）。
    接管端口这种护栏里，一个反着的名字就够把「有人占着」读成「空的，直接起」，
    所以 2026-10-04 顺手改成 port_is_heard：读起来是什么就是什么。
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def backend_identity(port, timeout=1.5):
    """问端口上那个进程「你是哪份代码」：回 {"version","code"}，没答腔回 None。

    旧后端（0.9.8 那批）认不出 `code` 这个字段 —— 它压根没这功能。回空串正好
    让调用方判断成「跟我不一样」，该接管就接管，不会因为对方太老就放过它。
    走无代理的 opener：本机回环不该被 Clash 那类全局代理绕出去（绕出去就连不上，
    然后被误判成「服务没在跑」）。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:%d/api/state" % int(port), timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return {"version": str(data.get("version") or ""),
            "code": str(data.get("code") or ""),
            # 旧后端也认 task 这个字段，顺手把它带回来：接管前得知道有没有活儿在跑
            "running": bool((data.get("task") or {}).get("running"))}


def port_listener_pid(port):
    """谁在听这个端口。认不出来回 0 —— 宁可不动，也不猜着杀。"""
    if IS_WIN:
        # netstat 的第四列是本地地址:端口，第五列是 PID；拿不到就回 0
        try:
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                 capture_output=True, text=True, timeout=10).stdout
        except Exception:
            return 0
        for line in out.splitlines():
            cols = line.split()
            if len(cols) >= 5 and cols[3].startswith("LISTENING") \
                    and cols[1].rsplit(":", 1)[-1] == str(port):
                try:
                    return int(cols[4])
                except ValueError:
                    return 0
        return 0
    try:
        out = subprocess.run(["lsof", "-nP", "-iTCP:%d" % int(port), "-sTCP:LISTEN", "-t"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return 0
    pids = [int(x) for x in out.split() if x.strip().isdigit()]
    return pids[0] if pids else 0


def is_our_backend(pid, script="ui_server.py"):
    """确认那个 PID 真是归藏自己的后端，不是别的应用恰好占着 8770。

    这是接管唯一的护栏：命令行里必须有那个脚本名。查不到就当不是，不动它。
    """
    if not pid or pid == os.getpid():
        return False
    if IS_WIN:
        cmd = ""
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_Process -Filter 'ProcessId=%d').CommandLine" % int(pid)],
                capture_output=True, text=True, timeout=10).stdout
            cmd = out
        except Exception:
            return False
    else:
        try:
            cmd = subprocess.run(["ps", "-o", "command=", "-p", str(int(pid))],
                                 capture_output=True, text=True, timeout=10).stdout
        except Exception:
            return False
    return script in cmd


def stop_backend(pid, wait=6.0):
    """请那个旧后端退出：先 SIGTERM 让它自己收尾，等不到再硬杀。回是否真退了。"""
    if not pid:
        return False
    try:
        os.kill(int(pid), signal.SIGTERM)
    except OSError:
        return False
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            os.kill(int(pid), 0)
        except OSError:
            return True                 # 没了，好好走的
        time.sleep(0.2)
    hard_kill_pid(int(pid))
    return not _pid_alive(int(pid))


def hard_kill_pid(pid):
    """连子孙一起收 —— 只杀 python 不够，它开的浏览器还握着 profile 锁。"""
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                           capture_output=True, timeout=20)
        else:
            os.killpg(os.getpgid(int(pid)), signal.SIGKILL)
    except Exception:
        try:
            os.kill(int(pid), signal.SIGKILL)
        except Exception:
            pass


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def take_over_port(port, my_code, wait=6.0):
    """端口被「别的代码」占着时接管它：认出旧进程 → 确认是自家后端 → 停掉。

    回 (这个端口能不能用, 一句给人看的话)。四种情况绝不动手：端口没人听（直接起）、
    听的就是同一份代码（已经是对的，再起一个只是多开一份）、对方不是归藏（占着端口的
    别的应用）、认不出那是谁的进程（宁可留着旧的，也不能误杀别人）。
    """
    if not port_is_heard(port, 0.6):
        return True, ""                                  # 空的，直接起
    who = backend_identity(port)
    if who is None:
        return False, "端口 %d 被别的程序占着，没动它" % port
    if who.get("code") and who.get("code") == my_code:
        return True, "同一份代码已经在 %d 上跑着（%s），这次只是又开一个" % (
            port, who.get("version") or "版本未报")
    if who.get("running"):
        # 有活儿在跑就不动手：取书一中断开，章节虽然段段落盘，但那本书得重头再点一次。
        # 界面是新的、后端是旧的 —— 页顶那条横幅会让用户等跑完再点「换新后端」。
        return False, "端口 %d 上的旧后端（%s）正在跑任务，不打断它" % (
            port, who.get("version") or "更老的一版")
    pid = port_listener_pid(port)
    if not pid or not is_our_backend(pid):
        return False, "端口 %d 上是 %s，但认不出是哪个进程，没敢动" % (
            port, who.get("version") or "更老的版本")
    stop_backend(pid, wait=wait)
    deadline = time.time() + wait
    while time.time() < deadline and port_is_heard(port, 0.3):
        time.sleep(0.2)                                  # 等它把端口真放下
    if port_is_heard(port, 0.3):
        return False, "停不掉端口 %d 上的旧后端（pid %d），先用着旧的" % (port, pid)
    return True, "已停掉旧后端 %s（pid %d），端口 %d 让出来了" % (
        who.get("version") or "更老的版本", pid, port)


def is_this_code_on(port, script_path):
    """端口上应答的，是不是手上这份代码。启动器轮询「新后端起来了没有」就靠这一句。

    只用 HTTP 通不通来判断是不够的：旧后端也通、也答话 —— 得认指纹。
    """
    mine = code_fingerprint(os.path.abspath(script_path))
    who = backend_identity(port)
    return bool(who and mine and who.get("code") == mine)


def port_verdict(port, script_path):
    """启动器 / MCP 适配器复用端口前该问的一句话：回 (动作, 给人看的说明)。

    动作五种：
      free     —— 没人听，正常起
      reuse    —— 正是手上这份代码，直接开界面
      takeover —— 是自家的旧后端且没在跑任务，让它退、换新进程起（ui_server --takeover 会办）
      busy     —— 是自家的旧后端，但任务正在跑：不起第二个，直接开界面（页顶会提示换后端）
      stranger —— 不是归藏，或者问不出话：绝不动它

    为什么要有这个函数：原来 .command 和 MCP 适配器都是「8770 有人听就当它是我」，
    于是更新过的安装被一个几周前留下的旧进程挡住 —— 界面是新的、后端是旧的，
    新功能全 404，报出来的却是「后端没启动」。2026-10-04 那三条 bug 就是这么来的。
    """
    if not port_is_heard(port, 0.8):
        return "free", ""
    who = backend_identity(port)
    if who is None:
        return "stranger", "端口 %d 被别的程序占着（它不答归藏的话），没动它" % port
    mine = code_fingerprint(os.path.abspath(script_path))
    if not mine:
        # 读不到自己的源码就没法判断，退回老行为：当成同一个，别把好服务赶走
        return "reuse", "读不到 %s，按原样用端口上那个后端" % os.path.basename(script_path)
    if who.get("code") == mine:
        return "reuse", "归藏 %s 已在 %d 上跑着" % (who.get("version") or "这一版", port)
    pid = port_listener_pid(port)
    if pid and is_our_backend(pid):
        if who.get("running"):
            # 旧后端手上还有任务在跑：别起第二个、也别打断它，让用户继续用当前这个，
            # 跑完了页顶那条横幅一点「换新后端」就干净了。
            return "busy", "端口 %d 上的旧后端 %s 正在跑任务，先不打断" % (
                port, who.get("version") or "更老的一版")
        return "takeover", "端口 %d 上是旧后端 %s，换个新进程接手" % (
            port, who.get("version") or "更老的一版")
    return "stranger", "端口 %d 上答的是归藏的话（%s），却认不出是哪个进程，没敢动" % (
        port, who.get("version") or "版本未报")


def wait_pid_gone(pid, wait=12.0):
    """等某个进程退出（重启交接用：接班人得等老人把端口放下）。"""
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            os.kill(int(pid), 0)
        except OSError:
            return True
        time.sleep(0.15)
    return False


def open_in_file_manager(path):
    """在系统文件管理器里打开一个目录。"""
    if IS_WIN:
        os.startfile(path)                     # 仅 Windows 提供
        return
    subprocess.Popen(["open" if IS_MAC else "xdg-open", path])


if __name__ == "__main__":
    # 给启动脚本一个不必拼 `python -c` 的入口（.bat 里拼一行 Python 太容易踩引号和括号）：
    #   python platform_compat.py verdict 8770   → 打印「动作|说明」，动作是
    #       free / reuse / takeover / busy / stranger
    #   python platform_compat.py ready 8770     → 端口上是不是手上这份代码（退出码 0/1）
    argv = sys.argv[1:]
    verb = argv[0] if argv else ""
    try:
        pnum = int(argv[1]) if len(argv) > 1 else 8770
    except ValueError:
        pnum = 8770
    here = os.path.dirname(os.path.abspath(__file__))
    target = argv[2] if len(argv) > 2 else os.path.join(here, "ui_server.py")
    if verb == "verdict":
        act, note = port_verdict(pnum, target)
        sys.stdout.write("%s|%s\n" % (act, note.replace("\n", " ")))
        sys.exit(0 if act in ("free", "reuse", "takeover", "busy") else 3)
    if verb == "ready":
        sys.exit(0 if is_this_code_on(pnum, target) else 1)
    sys.stderr.write("用法: python platform_compat.py verdict|ready <端口> [后端脚本]\n")
    sys.exit(2)
