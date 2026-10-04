# -*- coding: utf-8 -*-
"""认指纹那一套，得在真跑起来的服务上验一遍：静态看着对，端口上不一定。

这一轮修的三条 bug（画板打不开 / 导图打不开 / flomo 导入没反应）根因是同一个：界面从磁盘
现读、永远是新的，路由表却是进程起来那一刻装进内存的，于是端口上挂着几周前那版时，新功能
一律 HTTP 404，而界面把它报成「后端没启动」。修法是「前后端认同一份代码」——可这套判断
本身最容易出两种假绿：

1. 指纹两边各算各的（Python 一份、Node 一份、Swift 一份），一个字节不一样就会把「同一份」
   判成「旧的」，于是每次开都换一次进程，看起来一切正常，实际在原地打转。
2. 护栏反过来生效：把别人的进程当成归藏给停了，或者该接管时不敢接管。

所以这一套件全在真端口上跑，并且把「旧后端」现编在系统临时目录里冒充（脚本名就叫
ui_server.py，因为 platform_compat 认的是命令行里这个名字）：
闲着的旧后端 → 判 takeover 并真能停掉；正在跑任务的旧后端 → 判 busy 且一动不动；
别的应用占着端口 → 判 stranger 且一动不动；同一份代码 → 判 reuse；
/api/restart → 老人退出、接班人绑回同一个端口，期间只有一个监听者。

起的服务全是本套件自己起的，退出前一律收干净；门禁那份沙盒服务只读不打扰，
用户机器上正在跑的实例（8770/8800/60002…）一概不碰。

跑法：.venv/bin/python tests/test_backend_identity.py [http://127.0.0.1:PORT]
      不带参数时用环境变量 GUIZANG_TEST_URL（run_all.sh 会传）。
"""
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))
import selftest  # noqa: E402
import platform_compat as pc  # noqa: E402

SRV_PY = REPO / "ui_server.py"
MINE = pc.code_fingerprint(str(SRV_PY))
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
RESULTS = []

FAKE_SRC = '''# -*- coding: utf-8 -*-
"""假冒的「旧版归藏后端」：只在 /api/state 上答一句自己是 0.9.0、指纹对不上、有没有在跑任务。

文件名必须叫 ui_server.py —— platform_compat 敢动手的唯一条件就是命令行里有这个名字，
拿它当夹具正好能同时验「认得出是自家人」和「认不出就不动」这两条。
"""
import http.server
import json
import sys

RUNNING = "--running" in sys.argv
PORT = int(sys.argv[sys.argv.index("--port") + 1])


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"version": "0.9.0", "task": {"running": RUNNING}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


http.server.HTTPServer(("127.0.0.1", PORT), H).serve_forever()
'''


def note(ok, name, detail=""):
    RESULTS.append((ok, name, detail))
    print(("PASS  " if ok else "FAIL  ") + name + (("：" + detail) if detail else ""))
    return ok


def get_json(url, timeout=3):
    with NO_PROXY.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def post_json(url, body=None, timeout=15):
    req = urllib.request.Request(
        url, data=json.dumps(body or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with NO_PROXY.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def listeners(port):
    """这个端口上有几个进程在听（接管之后必须是恰好一个，不能新旧两个并存）。"""
    try:
        out = subprocess.run(["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-t"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    return [int(x) for x in out.split() if x.strip().isdigit()]


class Reap:
    """本套件起过的进程，退出前一律收掉。收自己起的，绝不碰别人的。"""

    def __init__(self):
        self.items = []

    def add(self, proc):
        self.items.append(proc)
        return proc

    def kill_all(self):
        for p in self.items:
            try:
                if p.poll() is not None:
                    continue                 # 早就退了，尸也收了，不用再动
                p.terminate()
                for _ in range(20):
                    if p.poll() is not None:
                        break
                    time.sleep(0.2)
                if p.poll() is None:
                    p.kill()
                    p.wait(timeout=3)
            except Exception:
                pass


def wait_heard(port, want=None, limit=30.0):
    """等端口上有人答话；want 给了就等它的 /api/state 里出现那个 version。"""
    deadline = time.time() + limit
    while time.time() < deadline:
        who = pc.backend_identity(port)
        if who and (want is None or who.get("version") == want):
            return who
        time.sleep(0.25)
    return None


def start_fake(running, tmp, reap):
    """把一个「旧后端」立在临时端口上，返回 (进程, 端口)。"""
    port = free_port()
    script = pathlib.Path(tmp) / "ui_server.py"
    script.write_text(FAKE_SRC, encoding="utf-8")
    args = [sys.executable, str(script), "--port", str(port)]
    if running:
        args.append("--running")
    proc = reap.add(subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    who = wait_heard(port, want="0.9.0", limit=12.0)
    return proc, port, who


def check_state_reports_code(base):
    try:
        s = get_json(base + "/api/state")
    except Exception as e:
        return note(False, "活服务报得出指纹", "打不开 %s/api/state：%s" % (base, e))
    got = str(s.get("code") or "")
    return note(got == MINE, "活服务报得出指纹",
                "后端报 %s，磁盘上这份算出来是 %s" % (got or "（没这个字段）", MINE))


def check_node_same_ruler(base):
    node = shutil.which("node")
    if not node:
        print("SKIP  Node 与 Python 同一把尺子：本机没有 node（这条不能算过，装了再跑）")
        return
    env = dict(os.environ)
    env["GUIZANG_API"] = base
    try:
        out = subprocess.run([str(node), str(REPO / "mcp" / "guizang-mcp.mjs"), "--identity"],
                             capture_output=True, text=True, timeout=30, env=env, cwd=str(REPO))
    except Exception as e:
        note(False, "Node 与 Python 同一把尺子", "跑不起来：%s" % e)
        return
    try:
        info = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:
        note(False, "Node 与 Python 同一把尺子", "--identity 没打出 JSON：%s" % out.stdout[:120])
        return
    disk = info.get("disk_code") or ""
    ok = disk == MINE and info.get("running_code") == MINE and info.get("same") is True
    note(ok, "Node 与 Python 同一把尺子",
         "适配器算磁盘 %s / 看服务 %s，Python 算 %s%s"
         % (disk or "—", info.get("running_code") or "—", MINE,
            "" if ok else "（对不上就是两份哈希各写各的，指纹判断全废）"))


def check_reuse(base):
    port = int(re.search(r":(\d+)", base).group(1))
    act, msg = pc.port_verdict(port, str(SRV_PY))
    note(act == "reuse", "同一份代码判成 reuse（不另起一个）", "%s｜%s" % (act, msg))


def check_idle_takeover(tmp, reap):
    proc, port, who = start_fake(False, tmp, reap)
    act, msg = pc.port_verdict(port, str(SRV_PY))
    ok = act == "takeover"
    detail = "%s｜%s" % (act, msg)
    if ok:
        can, note_txt = pc.take_over_port(port, MINE)
        # poll() 会把子进程收掉；不先收，os.kill(pid,0) 对「已经退了但还没收尸」的
        # 进程照样成功，看着像「没停掉」，其实是测试自己没打扫。
        proc.poll()
        gone = proc.poll() is not None
        ok = can and gone and not pc.port_is_heard(port, 0.6)
        detail += " → 接管回 (%s, %s)，旧进程%s" % (can, note_txt, "已退出" if gone else "还在")
    note(ok, "闲着的旧后端：判 takeover 并真能把端口接过来", detail)


def check_busy_not_interrupted(tmp, reap):
    proc, port, _ = start_fake(True, tmp, reap)
    act, msg = pc.port_verdict(port, str(SRV_PY))
    can, note_txt = pc.take_over_port(port, MINE)
    proc.poll()
    alive = proc.poll() is None and pc.port_is_heard(port, 0.6)
    note(act == "busy" and not can and alive,
         "在跑任务的旧后端：判 busy，绝不打断它",
         "%s｜接管回 (%s, %s)｜旧进程%s" % (act, can, note_txt, "还活着" if alive else "被弄死了"))


def check_stranger_untouched(reap):
    port = free_port()
    proc = reap.add(subprocess.Popen([sys.executable, "-m", "http.server", str(port), "-b", "127.0.0.1"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    deadline = time.time() + 10
    while time.time() < deadline and not pc.port_is_heard(port, 0.5):
        time.sleep(0.2)
    act, msg = pc.port_verdict(port, str(SRV_PY))
    can, note_txt = pc.take_over_port(port, MINE)
    proc.poll()
    alive = proc.poll() is None and pc.port_is_heard(port, 0.6)
    note(act == "stranger" and not can and alive,
         "别人的端口：判 stranger，一个字节都不动",
         "%s｜接管回 (%s, %s)｜对方%s" % (act, can, note_txt, "还在" if alive else "被弄死了"))


def check_restart_swap(reap):
    """一键换班：老人退、接班人绑同一个端口，全程只有一个监听者。

    这里自己起一个专属服务，绝不打扰门禁那份共享沙盒 —— 沙盒被换掉的话，
    排在后面的真机套件就连的不是同一个进程了。
    """
    port = free_port()
    log = tempfile.mkdtemp(prefix="gz-restart-")
    proc = reap.add(subprocess.Popen(
        [sys.executable, str(SRV_PY), "--port", str(port)],
        cwd=str(REPO), stdout=open(os.path.join(log, "server.log"), "wb"),
        stderr=subprocess.STDOUT, env=dict(os.environ)))
    who = wait_heard(port, want=None, limit=40.0)
    if not who:
        proc.terminate()
        return note(False, "一键换班 /api/restart", "专属沙盒服务没起来（端口 %d）" % port)
    old_pid = proc.pid
    try:
        r = post_json("http://127.0.0.1:%d/api/restart" % port)
    except Exception as e:
        # 读不到回执不等于换班没发生：接班人是被老人 detached 起来的，老人照退、
        # 它照绑同一个端口。这一趟要是就这么 return，机器上会多留一个没人管的进程
        # （第一轮就漏过一个，得手动收）。按端口把它找回来收干净再报失败。
        stray = pc.port_listener_pid(port)
        if stray and pc.is_our_backend(stray):
            pc.stop_backend(stray, wait=5.0)
        return note(False, "一键换班 /api/restart", "请求打不过去：%s" % e)
    new_pid = int(r.get("pid") or 0)
    # 老人是自己 os._exit 的，而它是我起的子进程 —— 不收尸的话 os.kill(pid,0)
    # 对僵尸照样成功，看着像「没退」。用 poll() 一边收一边判。
    gone = False
    deadline = time.time() + 20
    while time.time() < deadline and not gone:
        proc.poll()
        gone = proc.poll() is not None
        time.sleep(0.2)
    after = wait_heard(port, want=None, limit=40.0)
    ok = (r.get("ok") and gone and new_pid > 0 and after
          and after.get("code") == MINE and new_pid not in (old_pid,)
          and len(listeners(port)) == 1)
    note(bool(ok), "一键换班 /api/restart",
         "回执 ok=%s pid=%s｜老人(pid %d)%s｜换班后端口 %d 上是 %s，监听者 %d 个"
         % (r.get("ok"), new_pid, old_pid, "已退出" if gone else "没退",
            port, (after or {}).get("version") or "没人答话", len(listeners(port))))
    # 接班人不在本套件的进程树里（它是 detached 起来的），按回执 pid 收干净
    if new_pid and pc._pid_alive(new_pid):
        pc.stop_backend(new_pid, wait=6.0)
    shutil.rmtree(log, ignore_errors=True)


def main():
    base = selftest.need_base()
    if not MINE:
        print("FAIL  读不到 ui_server.py，指纹无从算起")
        return 1
    reap = Reap()
    tmp = tempfile.mkdtemp(prefix="gz-fake-old-")
    try:
        check_state_reports_code(base)
        check_node_same_ruler(base)
        check_reuse(base)
        check_idle_takeover(tmp, reap)
        check_busy_not_interrupted(tmp, reap)
        check_stranger_untouched(reap)
        check_restart_swap(reap)
    finally:
        reap.kill_all()
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [n for ok, n, _ in RESULTS if not ok]
    print(("FAIL  认指纹这一套有 %d 处不对：%s" % (len(bad), "、".join(bad))) if bad
          else "PASS  认指纹这一套全对上了：服务报得出、三语言同一把尺、该接的接、不该动的不动")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
