# -*- coding: utf-8 -*-
"""MCP 适配器「服务没在跑」这一条路的门禁。

为什么要单开一份：check_mcp_tools.py 是挂在 run_all.sh 那个活服务上跑的，
它永远验不到「后台没人、第一次点」的场景 —— 而那恰恰是用户装完归藏、
只开了 agent 没开界面时的真实处境。这时适配器要自己把服务拉起来。

第三十轮修的就是这条路上攒下的四个坑（每一个都会让画板 / 导图点名报错）：

1. 解释器找错地方。只认 <数据目录>/bin 与 /Scripts，本项目建的是 .venv/，
   于是退回 PATH 上的 python3 —— 那个 python 没装本项目的依赖，服务起来就死。
2. feedparser 在 feed.py 顶层 import。少这一个可选包，整个服务（画板、导图、
   笔记全在内）都起不来。
3. 端口找不着。8770 被占时服务自己往后挪最多 20 个，适配器只认它请的那个数字，
   挪完就没人知道它在哪。
4. 一部分工具直接 fetch，跳过「先把服务拉起来」这一步，用户看到的是
   Node 的 "fetch failed" —— 那不是归藏说的话，谁都不知道该干什么。

跑法（不需要外部服务，自己起、自己收）：
    .venv/bin/python tests/check_mcp_boot.py
本机没有 node 时明确 SKIP 并退 0：起不来适配器就等于什么都没测，不能装绿。
"""
import atexit
import json
import os
import pathlib
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import selftest  # noqa: E402

ROOT = selftest.REPO
MCP_FILE = ROOT / "mcp" / "guizang-mcp.mjs"

# 自己的沙盒：不跟 run_all.sh 那份共用 cache，否则两边服务会读写同一份配置。
BOOT = selftest.SANDBOX / "boot"
CACHE = BOOT / "cache"
BOOKS = BOOT / "books"

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def port_busy(p):
    s = socket.socket()
    try:
        s.connect(("127.0.0.1", p))
        return True
    except OSError:
        return False
    finally:
        s.close()


def http_ok(port, path="/api/state"):
    import urllib.request
    try:
        urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=2).read()
        return True
    except Exception:
        return False


class BootMCP:
    """冷启动用的 JSON-RPC 客户端：只给端口号，不给地址。

    不给 GUIZANG_API 是有意的 —— 那正是「用户什么都没配」的默认形态，
    端口要靠适配器自己从服务写的回执里找。
    """

    def __init__(self, port):
        CACHE.mkdir(parents=True, exist_ok=True)
        BOOKS.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.pop("GUIZANG_PYTHON", None)      # 让适配器自己找解释器，才是真实场景
        env.pop("GUIZANG_API", None)
        env.update(GUIZANG_REPO=str(ROOT), GUIZANG_PORT=str(port),
                   GUIZANG_DATA=str(CACHE), GUIZANG_BOOKS=str(BOOKS))
        self.port = port
        self.p = subprocess.Popen([NODE, str(MCP_FILE)], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8", env=env, cwd=str(ROOT))
        self.q = queue.Queue()
        self.err = []
        for stream, sink in ((self.p.stdout, self.q.put), (self.p.stderr, self.err.append)):
            threading.Thread(target=self._pump, args=(stream, sink), daemon=True).start()
        self.n = 0

    @staticmethod
    def _pump(stream, sink):
        for line in stream:
            sink(line)
        sink(None)

    def _send(self, obj):
        self.p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.p.stdin.flush()

    def call(self, tool, args=None, timeout=60):
        """发一次 tools/call，返回 (耗时, 文本, isError)。超时/断流都算失败，别把门禁挂死。"""
        self.n += 1
        rid = self.n
        t0 = time.time()
        self._send({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                    "params": {"name": tool, "arguments": args or {}}})
        while time.time() - t0 < timeout:
            try:
                line = self.q.get(timeout=max(0.1, timeout - (time.time() - t0)))
            except queue.Empty:
                break
            if line is None:
                return time.time() - t0, "适配器提前退出：" + "".join(self.err[-4:]), True
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") != rid:
                continue
            if "error" in msg:
                return time.time() - t0, json.dumps(msg["error"], ensure_ascii=False), True
            res = msg.get("result") or {}
            text = "".join(c.get("text", "") for c in res.get("content", []))
            return time.time() - t0, text, bool(res.get("isError"))
        return time.time() - t0, "（%d 秒没回这一条）" % timeout, True

    def close(self):
        try:
            self.p.kill()
        except Exception:
            pass


def stop_spawned_server():
    """只收自己这台沙盒服务：端口写在自己的 runtime.json 里，认它不认进程名。

    按 --port 匹配去 pkill 会误伤用户自己开着的归藏（它们也带同样的命令行）。
    """
    rt = CACHE / "runtime.json"
    try:
        pid = int(json.loads(rt.read_text(encoding="utf-8")).get("pid") or 0)
    except Exception:
        return
    if pid <= 0:
        return
    try:
        os.kill(pid, 15)
    except (ProcessLookupError, PermissionError):
        pass


NODE = shutil.which("node") or shutil.which("node.exe")
if not NODE:
    print("SKIP  本机 PATH 里没有 node，MCP 适配器起不起来，这套件没测任何东西。")
    sys.exit(0)
if not MCP_FILE.is_file():
    print("FAIL  找不到适配器：" + str(MCP_FILE))
    sys.exit(1)

shutil.rmtree(BOOT, ignore_errors=True)
atexit.register(lambda: shutil.rmtree(BOOT, ignore_errors=True))
atexit.register(stop_spawned_server)

BAD = ("fetch failed", "启动超时", "没应答", "ECONNREFUSED", "not enough arguments")

# ── ① 冷启动：后台没人，第一次点 ────────────────────────────────
print("\n① 后台没起服务时，画板 / 导图 / 订阅 / 书架各自要给出人话")
p1 = free_port()
while port_busy(p1):
    p1 = free_port()
m = BootMCP(p1)
m._send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "boot-check", "version": "0"}}})
m.q.get(timeout=30)
for tool in ("board_list", "map_show", "feed_list", "shelf_list", "app_status"):
    dt, text, is_err = m.call(tool, {"book": "没这本"} if tool in ("board_list", "map_show") else {})
    chk("%s 冷启动有答复（%.1fs）" % (tool, dt), dt < 20 and not any(b in text for b in BAD), text[:120])
    # 画板与导图按书名找书，书名不在书架上 —— 该报的是「没这本书」，不是连不上服务
    if tool in ("board_list", "map_show"):
        chk("%s 说的是「没这本书」而不是连不上" % tool, "书架" in text or "没有" in text, text[:120])
chk("服务确实被拉起来了", http_ok(p1) or http_ok(p1 + 1))
m.close()
stop_spawned_server()
time.sleep(0.4)

# ── ② 默认端口被占：服务挪了窝，适配器还找得着 ──────────────────
print("\n② 请它用的端口被占时，服务自己往后挪，适配器要顺着回执找过去")
p2 = free_port()
squatter = socket.socket()
squatter.bind(("127.0.0.1", p2))
squatter.listen(1)
shutil.rmtree(BOOT, ignore_errors=True)
m2 = BootMCP(p2)
m2._send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                     "clientInfo": {"name": "boot-check", "version": "0"}}})
m2.q.get(timeout=30)
dt, text, _ = m2.call("shelf_list")
chk("端口被占也能用起来（%.1fs）" % dt, not any(b in text for b in BAD) and dt < 20, text[:120])
try:
    bound = int(json.loads((CACHE / "runtime.json").read_text(encoding="utf-8"))["port"])
except Exception:
    bound = 0
chk("服务把真实端口写进回执（挪到了 %s）" % (bound or "？"),
    bound and bound != p2 and port_busy(bound))
m2.close()
stop_spawned_server()
squatter.close()

# ── ③ 解释器口径：必须认 .venv/ ────────────────────────────────
print("\n③ 适配器与后端要用同一个解释器（项目自带的 .venv），不能退回 PATH 上的 python3")
src = MCP_FILE.read_text(encoding="utf-8")
chk("适配器找 .venv", '".venv"' in src and "dataDir()" in src)
chk("适配器不再只认 bin/ 与 Scripts/ 两个位置", src.count("python.exe") >= 1 and ".venv" in src)
venv = CACHE / ".venv"      # 沙盒里没建环境，退回的是仓库那份 —— 这里验仓库路径能命中
chk("仓库里有 .venv 可认", (ROOT / ".venv" / "bin" / "python").exists()
    or (ROOT / ".venv" / "Scripts" / "python.exe").exists())
sys_src = (ROOT / "feed.py").read_text(encoding="utf-8")
lines = [l.strip() for l in sys_src.splitlines()]
chk("feedparser 不在顶层 import（少个可选包不该拖死整个服务）",
    "import feedparser" in sys_src and "import feedparser" not in lines[:60])

print()
print("通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
