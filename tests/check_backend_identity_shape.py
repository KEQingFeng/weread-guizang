# -*- coding: utf-8 -*-
"""认指纹的**读口**不能因为对方回的 JSON 形状怪就抛。

`platform_compat.backend_identity()` 问端口上那个进程「你是哪份代码」，读的是
`/api/state` 里的 `version` / `code` / `task`。前两个是标量，怎么脏都能 `str()`；
`task` 是要往下取 `.get("running")` 的，于是形状就成了事：

    {"version":"1.0","code":"abc","task":"up"}      → 'str' object has no attribute 'get'
    {"version":"1.0","code":"abc","task":[1,2]}     → 'list' object has no attribute 'get'
    {"version":"1.0","code":"abc","task":7}         → 'int' object has no attribute 'get'

旧写法 `(data.get("task") or {}).get("running")` 只兜住了 null / 缺键（假值时 `or {}`），
非空非对象当场 AttributeError。这不是「页面按钮没反应」那一档 —— 它更靠前：

  · `ui_server.py --takeover` 在绑端口**之前**就调 `pc.take_over_port()`，而两个启动
    脚本、mac 壳、MCP 适配器全都带 `--takeover`。8770 上要是坐着别的程序（用户开着
    另一个本地服务），它只要回一个带 `task` 的 JSON、形状不是对象，归藏这次启动就带着
    一行英文报错退出 —— 用户的观感是「点了图标什么都没发生」。
  · `port_verdict()` 是 .command / 适配器复用端口前的唯一一句话，一样在这条路上。

Node 那边的同一把尺子 `!!(s.task && s.task.running)` 本来就不会抛（JS 取不存在的属性
给 undefined）。这一条把 Python 侧对齐，并钉住三件事：

  A 纯函数：`task` 是对象才读 `running`，其余形状一律当「没在跑」，绝不抛；
    真对象里的 true / false 一个字不改，干净回话不被误伤。
  B 对照：把旧表达式拿同一个脏值跑一遍，必须真抛 AttributeError —— 免得收口被
    拿掉之后这条套件还在空转。
  C 真机：现编一个「答归藏的话、却是别人的程序」立在临时端口上，把上表这些形状
    逐一喂给 `backend_identity` / `port_verdict` / `take_over_port`，三个口都不许抛；
    而且 `take_over_port` 仍必须判「不是自家进程」、一个字节都不动它。
    另加 Node 侧同形状一把尺子（有 node 才跑，没有明确标 SKIP）。

只碰回环 127.0.0.1 上一个自己起的临时服务，不联网、不起浏览器、不碰用户的书，
归 --fast 静态段。
"""
import atexit
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

import platform_compat as pc  # noqa: E402

FAIL = []
PASSED = [0]


def chk(name, cond, extra=""):
    if cond:
        PASSED[0] += 1
    else:
        FAIL.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:300]))


# ── 现编一个「陌生人」服务：回我们指定的那串字节，别的一概不做 ──────────
# 文件名故意不叫 ui_server.py —— platform_compat 敢动手的唯一条件是命令行里有那个
# 名字，这里要验的正是「它不是自家的，读口也得读得开、而且不许碰」。
FAKE_SRC = '''# -*- coding: utf-8 -*-
"""测试夹具：/api/state 回 BODY 文件里的原样字节，用来喂各种脏形状。"""
import http.server
import sys

BODY = sys.argv[2]


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            with open(BODY, "rb") as f:
                raw = f.read()
        except OSError:
            raw = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
'''

SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-id-shape-"))
BODY_FILE = SB / "body.bin"
SCRIPT = SB / "fake_state_srv.py"
SCRIPT.write_text(FAKE_SRC, encoding="utf-8")
BODY_FILE.write_bytes(b"{}")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT = _free_port()
srv = subprocess.Popen([sys.executable, str(SCRIPT), str(PORT), str(BODY_FILE)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _cleanup():
    try:
        srv.terminate()
        srv.wait(timeout=5)
    except Exception:
        try:
            srv.kill()
        except Exception:
            pass
    shutil.rmtree(SB, ignore_errors=True)


atexit.register(_cleanup)

server_up = False
for _ in range(80):
    if pc.port_is_heard(PORT, 0.3):
        server_up = True
        break
    time.sleep(0.1)
chk("真机：夹具服务起来了", server_up, "端口 %d 没人听" % PORT)


def read_as(raw, timeout=1.5):
    """把 BODY 换成 raw，再让 backend_identity 读一次。返回 (值, 异常字符串或空)。"""
    BODY_FILE.write_bytes(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
    try:
        return pc.backend_identity(PORT, timeout=timeout), ""
    except Exception as e:                     # 旧代码就是在这里抛 AttributeError
        return None, "%s: %s" % (type(e).__name__, e)


if server_up:
    # ── A 纯函数：task 是对象才读 running，其余形状当「没在跑」 ─────────
    # (说明, 回包字节, 期望的 running)
    UTTER = '{"version":"9.9","code":"feedface","task":%s}'
    CASES = (
        ("task 是对象、running 真", UTTER % '{"running":true}', True),
        ("task 是对象、running 假", UTTER % '{"running":false}', False),
        ("task 是空对象", UTTER % "{}", False),
        ("task 是字符串", UTTER % '"up"', False),
        ("task 是非空数组", UTTER % "[1,2,3]", False),
        ("task 是数字", UTTER % "7", False),
        ("task 是布尔真", UTTER % "true", False),
        ("task 是 null", UTTER % "null", False),
        ("压根没有 task 这个键",
         '{"version":"9.9","code":"feedface"}', False),
    )
    bad = []
    for name, raw, want in CASES:
        who, err = read_as(raw)
        if err or not isinstance(who, dict) or who.get("running") is not want:
            bad.append((name, err or who))
    chk("纯函数：脏 task 一律不抛，且 clean 的 true/false 一个字不改", not bad, bad)

    # 标量字段照旧读得出来（收口别把正常回话也一起收了）
    who, err = read_as(UTTER % '{"running":false}')
    chk("纯函数：version / code 照旧读得出来",
        not err and (who or {}).get("version") == "9.9" and (who or {}).get("code") == "feedface",
        err or who)

    # 顶层不是对象 / 不是 JSON：既有的「回 None」契约不许动
    for name, raw in (("顶层是数组", b"[1,2,3]"), ("顶层是字符串", b'"hello"'),
                      ("根本不是 JSON", b"{oops")):
        who, err = read_as(raw)
        chk("纯函数：%s → None（不是抛）" % name, not err and who is None, err or who)

    # ── B 对照：旧表达式拿同一个脏值确实会抛 ──────────────────────────
    control_bad = []
    for dirty in ("up", [1, 2, 3], 7, True):
        try:
            bool((dirty or {}).get("running"))
            control_bad.append((repr(dirty), "没抛"))
        except AttributeError:
            pass
    chk("对照：旧写法 `(task or {}).get('running')` 遇到这些脏值确实抛 AttributeError",
        not control_bad, control_bad)

    # ── C 真机：接管链的三个读口都不抛，而且不碰这个「陌生人」 ─────────
    BODY_FILE.write_bytes((UTTER % '"up"').encode("utf-8"))
    try:
        verdict, msg = pc.port_verdict(PORT, str(REPO / "ui_server.py"))
        ok_verdict = isinstance(verdict, str) and verdict != ""
    except Exception as e:
        verdict, msg, ok_verdict = None, "%s: %s" % (type(e).__name__, e), False
    chk("真机：port_verdict 读脏回话不抛、给得出判定", ok_verdict, msg)
    # 夹具脚本不叫 ui_server.py，is_our_backend 必须认不出它 —— 所以是 stranger。
    chk("真机：答着归藏话的陌生程序仍判 stranger（不许误当自家后端）",
        verdict == "stranger", "判定=%r｜%s" % (verdict, msg))

    try:
        can, note = pc.take_over_port(PORT, "deadbeefdead")
        ok_take = can is False
    except Exception as e:
        can, note, ok_take = None, "%s: %s" % (type(e).__name__, e), False
    chk("真机：take_over_port 读脏回话不抛、且拒绝接管", ok_take,
        "回 (%r, %s)" % (can, note))
    time.sleep(0.3)
    chk("真机：拒绝之后那个程序还在（一个字节都没动它）", pc.port_is_heard(PORT, 0.6),
        "端口 %d 没人听了，说明把陌生人弄掉了" % PORT)

    # ── Node 侧同一把尺子（没有 node 就 SKIP，不算过也不算错） ──────────
    node = shutil.which("node")
    if not node:
        print("SKIP  Node 侧同一把尺子：本机没有 node（这条不能算过，装了再跑）")
    else:
        js = ('console.log(JSON.stringify(["up",[1],7,true,{},null]'
              '.map(t => !!(t && t.running))))')
        try:
            out = subprocess.run([node, "-e", js], capture_output=True,
                                 text=True, timeout=20)
            got = json.loads(out.stdout.strip().splitlines()[-1])
        except Exception as e:
            got = "跑不起来：%s" % e
        chk("Node 侧同一把尺子（脏 task 一律不算在跑）",
            got == [False, False, False, False, False, False], got)

print()
if FAIL:
    print("认指纹读口形状门禁有 %d 处不对：%s" % (len(FAIL), "、".join(FAIL)))
else:
    print("认指纹读口形状：task 不是对象也只当「没在跑」，接管链三个读口都不抛、不碰陌生人。")
sys.exit(1 if FAIL else 0)
