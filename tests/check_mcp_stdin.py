#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏门禁：MCP 适配器的标准输入里出现「读不懂的行」不许把整个进程带走。

MCP 适配器（mcp/guizang-mcp.mjs）是一行一条 JSON-RPC 的 stdio 服务，宿主
（Claude Desktop / Cursor / 各家 Agent 运行时）把请求一行行喂进去。原来那一段的形状是

    let msg;
    try { msg = JSON.parse(line); } catch { return; }
    const { id, method, params } = msg;

`try/catch` 只兜得住「JSON 本身坏了」，兜不住「JSON 合法、但根本不是请求对象」——
`JSON.parse("null")` 老老实实返回 `null`，紧接着 `const {…} = null` 当场抛
TypeError。而 readline 的回调是 `async` 的，异常没人接，变成 unhandledRejection：
Node 22 直接把栈打到 stderr、进程退 1。实测喂一条 `null` 之后适配器不再应答任何
请求 —— 61 个工具一起消失，宿主那边只看到「MCP 服务掉了」，和 `try/catch` 想做的
事正好相反。

这一条把「合法 JSON，但不是请求对象」的形状全喂一遍（null / 数字 / 字符串 /
布尔 / 数组 / 缺 method …），**每喂一条就跟一条 ping**：进程不许退、ping 必须回。
另外正向钉一遍正常流程（initialize / tools/list / 未知工具 / 工具报错）没被这层
收口改坏，以及不能当 JSON 解的坏文本仍按原样被忽略。

全程一个临时数据目录，**一个真工具都不调**（适配器的每个真工具都会先 ensureServer()、
起一个 detached 的真服务进程，那是真机套件的事），坏形状一律走 `__nope__` 这条
未知工具路；另指着一个没人听的端口，不联网、不起服务、不碰真实 cache/ 与书库。
要 node，和内联 JS 那两条同档。
跑法：.venv/bin/python tests/check_mcp_stdin.py
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
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import selftest  # noqa: E402

REPO = selftest.REPO
ADAPTER = REPO / "mcp" / "guizang-mcp.mjs"
SRC = ADAPTER.read_text(encoding="utf-8")

FAIL = []
PASSED = [0]


def chk(name, cond, extra=""):
    if cond:
        PASSED[0] += 1
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:300]))
    if not cond:
        FAIL.append(name)


def dead_port():
    """一个绑过又立刻放掉的本地端口：连它必定被拒，工具调用于是走错误分支。"""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class Adapter:
    """一个真跑起来的适配器进程：喂行、收行、按 id 等回话。"""

    def __init__(self, data_dir, api_base):
        env = dict(os.environ)
        env["GUIZANG_DATA"] = str(data_dir)
        env["GUIZANG_REPO"] = str(REPO)
        env["GUIZANG_API"] = api_base
        env.pop("GUIZANG_PORT", None)
        self.proc = subprocess.Popen(
            ["node", str(ADAPTER)], cwd=str(REPO), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1)
        self._out, self._err, self._lock = [], [], threading.Lock()
        for stream, sink in ((self.proc.stdout, self._out), (self.proc.stderr, self._err)):
            threading.Thread(target=self._pump, args=(stream, sink), daemon=True).start()

    def _pump(self, stream, sink):
        try:
            for line in stream:
                with self._lock:
                    sink.append(line)
        except Exception:  # noqa: BLE001 —— 收不到就算了，断言那边自会红
            pass

    def send(self, line):
        """喂一行。进程已经死了就回 False，让断言红在一处而不是抛栈。"""
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, ValueError, OSError):
            return False

    def wait_id(self, mid):
        """等 `"id":mid` 的回话。等不到（进程退了 / 超时）回 None。"""
        needle = '"id":%d,' % mid
        end = time.time() + 8.0
        while time.time() < end:
            with self._lock:
                for ln in self._out:
                    if needle in ln.replace(" ", ""):
                        return ln
            if self.proc.poll() is not None:
                return None
            time.sleep(0.02)
        return None

    def alive(self):
        return self.proc.poll() is None

    def stderr(self):
        with self._lock:
            return "".join(self._err)

    def close(self):
        try:
            self.proc.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass


# ── 喂进去的坏形状 ────────────────────────────────────────────
# 「合法 JSON，但不是请求对象」：这一组原来只有 null 会带走进程，其余当时就无害，
# 一并钉住是为了以后加分支时不会又漏一个。
DIRTY = [
    ("null", "null"),
    ("true", "true"),
    ("false", "false"),
    ("数字 0", "0"),
    ("数字 1", "1"),
    ("数字 -1.5", "-1.5"),
    ("空字符串", '""'),
    ("字符串", '"ping"'),
    ("空数组", "[]"),
    ("数组", '["jsonrpc","2.0"]'),
    ("空对象", "{}"),
    ("对象缺 method", '{"jsonrpc":"2.0","id":7}'),
    ("method 是数字", '{"jsonrpc":"2.0","id":7,"method":123}'),
    ("method 是对象", '{"jsonrpc":"2.0","id":7,"method":{"a":1}}'),
    ("params 是 null", '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":null}'),
    ("params 是字符串", '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":"x"}'),
    ("params 是数组", '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":[1,2]}'),
    ("arguments 是数字", '{"jsonrpc":"2.0","id":7,"method":"tools/call",'
                         '"params":{"name":"__nope__","arguments":123}}'),
    ("arguments 是字符串", '{"jsonrpc":"2.0","id":7,"method":"tools/call",'
                           '"params":{"name":"__nope__","arguments":"z"}}'),
    ("arguments 是 null", '{"jsonrpc":"2.0","id":7,"method":"tools/call",'
                          '"params":{"name":"__nope__","arguments":null}}'),
    ("arguments 是数组", '{"jsonrpc":"2.0","id":7,"method":"tools/call",'
                         '"params":{"name":"__nope__","arguments":[]}}'),
    ("initialize params 是 null", '{"jsonrpc":"2.0","id":7,"method":"initialize","params":null}'),
    ("initialize params 是数组", '{"jsonrpc":"2.0","id":7,"method":"initialize","params":[1]}'),
    ("没有这个名字的方法", '{"jsonrpc":"2.0","id":7,"method":"__nope__"}'),
    ("深嵌套", '{"jsonrpc":"2.0","id":7,"method":"ping","params":'
               + "[" * 200 + "]" * 200 + "}"),
    ("落单代理码点", '{"jsonrpc":"2.0","id":7,"method":"ping","params":{"x":"\\ud800"}}'),
]
# 连 JSON 都不是：原来就走 catch，这次一并确认没被上面那层收口带跑偏。
BROKEN = [
    ("半截对象", "{oops"),
    ("半截数组", "[1,"),
    ("没闭合的字符串", '"unterminated'),
    ("多余逗号", '{"a":1,}'),
    ("裸右括号", "}"),
    ("只有一个冒号", ":"),
]

node = shutil.which("node")
chk("本机有 node（适配器与内联 JS 两条门禁同一条依赖）", bool(node),
    "找不到 node，装一个再跑：https://nodejs.org")

# ── 源码走查：收口必须在解构之前（一处实现）────────────────────
DESTRUCT = "const { id, method, params } = msg;"
pos = SRC.find(DESTRUCT)
chk("适配器里解构 msg 那一行还在（拿它当锚点）", pos >= 0, DESTRUCT)
chk("解构之前有「非对象就按读不懂处理」这道闸",
    pos >= 0 and "typeof msg" in SRC[max(0, pos - 500):pos],
    "收口要落在 `const {…} = msg` 之前，否则 null 行照样抛")

if not FAIL:
    SANDBOX = tempfile.mkdtemp(prefix="guizang-mcp-stdin-")
    atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))
    a = Adapter(SANDBOX, "http://127.0.0.1:%d" % dead_port())
    try:
        # ── ① 正向：正常流程没被这层收口改坏 ────────────────────
        a.send('{"jsonrpc":"2.0","id":1,"method":"initialize",'
               '"params":{"protocolVersion":"2024-11-05"}}')
        ln = a.wait_id(1)
        obj = None
        try:
            obj = json.loads(ln) if ln else None
        except Exception:  # noqa: BLE001
            obj = None
        chk("initialize 有回话，身份是归藏",
            bool(obj) and obj.get("result", {}).get("serverInfo", {}).get("name") == "guizang",
            ln)

        a.send('{"jsonrpc":"2.0","id":2,"method":"tools/list"}')
        ln = a.wait_id(2)
        names = []
        try:
            names = [t.get("name") for t in json.loads(ln)["result"]["tools"]]
        except Exception:  # noqa: BLE001
            names = []
        chk("tools/list 回的工具表仍是 61 件（README 报的就是这个数）",
            len(names) == 61, "拿到 %d 件：%s" % (len(names), names[:5]))
        chk("工具表里有 shelf_list（拿它当正常调用的样本）", "shelf_list" in names, names[:5])

        a.send('{"jsonrpc":"2.0","id":3,"method":"tools/call",'
               '"params":{"name":"__nope__","arguments":{}}}')
        ln = a.wait_id(3)
        chk("tools/call 报错：回 isError 的人话，不是沉默也不是崩",
            bool(ln) and '"isError":true' in ln.replace(" ", "") and "unknown tool" in ln, ln)

        # ── ② 坏行：每喂一条就跟一条 ping，进程不许退、必须回话 ──
        died_at = None
        for i, (name, line) in enumerate(DIRTY + BROKEN):
            mid = 1000 + i
            a.send(line)
            a.send('{"jsonrpc":"2.0","id":%d,"method":"ping"}' % mid)
            ln = a.wait_id(mid)
            if not (ln and a.alive()):
                died_at = name
                chk("坏行 %-26s：进程还活着且 ping 有回话" % name, False,
                    "stderr 末尾：%s" % a.stderr()[-300:].replace("\n", " | "))
                break
        else:
            chk("坏行 %d 条（含 %d 条不是 JSON 的）逐条喂进去，进程始终活着、条条 ping 有回话"
                % (len(DIRTY) + len(BROKEN), len(BROKEN)), True)

        if died_at is not None:
            chk("坏行序列没在中途带走适配器", False,
                "第一条致命的行是「%s」" % died_at)

        # ── ③ 坏行之后正常请求照旧 ────────────────────────────
        a.send('{"jsonrpc":"2.0","id":9,"method":"ping"}')
        ln = a.wait_id(9)
        chk("一整轮坏行之后，正常 ping 照旧有回话",
            bool(ln) and '"result":{}' in ln.replace(" ", ""), ln)

        err = a.stderr()
        chk("整轮下来 stderr 没有崩溃栈（unhandledRejection / TypeError）",
            "unhandledRejection" not in err and "TypeError" not in err, err[-300:])

        # 这条是给以后的自己上的锁：适配器的真工具会 ensureServer() 拉一个 detached
        # 真服务；门禁一旦误调真工具，沙盒里就会多出服务写的 runtime.json，这里当场红。
        chk("全程没有偷偷拉起真服务（临时数据目录里没有 runtime.json）",
            not os.path.exists(os.path.join(SANDBOX, "runtime.json")),
            "谁在门禁里调了真工具？")
    finally:
        a.close()

print("MCP 标准输入脏行体检：%d 项通过，失败 %d 项" % (PASSED[0], len(FAIL)))
if FAIL:
    print("未通过：")
    for f in FAIL:
        print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
