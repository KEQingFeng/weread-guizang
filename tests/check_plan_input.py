# -*- coding: utf-8 -*-
"""阅读计划的「脏读数」门禁：章序号报了个不是数的东西，不许把连接甩掉。

为什么单开一条：`readplan.pages_at` 原先只把 `frac` 用 try 包住，`at` 却是裸
`int(at or 0)`；而 `record_pos` 存 `at` 时用的又是收口过的 `_int`。同一个参数
一处防、一处不防，后果在真机上就是：界面按心跳上报读到哪，只要 `at` 是
``"abc"`` / ``NaN`` / 半个对象 这类值，路由那层没有兜底，`do_POST` 直接抛
`ValueError`，服务端把连接一关 —— 前端拿到的是 `RemoteDisconnected`，计划
既不前进也不报错，只停在原地。这一条把它钉成：

  · 纯函数：任何认不出的 `at` 都不许抛，且一律当第 0 章（宁可这一跳当没前进，
    绝不能当「读完了」）；
  · 真机：拿 `at: "abc"` 打一次 `/api/plan`，回的必须是 200 + 一份 JSON，
    书不能被记成读完。

不起浏览器，归静态段，--fast 也得带上。
"""
import atexit
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
sys.path.insert(0, str(REPO))
import book_layout  # noqa: E402
import readplan as rp  # noqa: E402

FAIL = []
PASSED = [0]


def chk(name, cond, extra=""):
    if cond:
        PASSED[0] += 1
    else:
        FAIL.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:240]))


# ── 1 纯函数：认不出的章序号一律当第 0 章，不许抛 ───────────────────
CH = [{"chars": 3000}, {"chars": 1000}]
BAD_AT = ["abc", "3.5", "  ", "", float("nan"), float("inf"), float("-inf"),
          {"a": 1}, ["x"], (1, 2), b"\xff", "\ud800", object(), "1e999"]

raised = []
base = rp.pages_at(CH, 0, 0.5)
for v in BAD_AT:
    try:
        got = rp.pages_at(CH, v, 0.5)
    except Exception as e:                       # noqa: BLE001 —— 就是来找它抛不抛
        raised.append((repr(v), type(e).__name__))
        continue
    if got != base:
        raised.append((repr(v), got, base))
chk("认不出的章序号：一个都不抛，且结果与第 0 章一致", not raised, raised)

# 坏值不能把书判成「读完」：这是最坏的一种错，计划会当场留档封存。
chk("坏章序号不当读完全书（不是 total_pages）",
    all(rp.pages_at(CH, v, 0.5) != rp.total_pages(CH) for v in BAD_AT),
    [rp.pages_at(CH, v, 0.5) for v in BAD_AT])

# 正常的数与数值字符串仍照旧认得（别把收口收成一律归零）。
chk("正常的章序号没被误伤",
    rp.pages_at(CH, 1, 0.0) == rp.pages_at(CH, "1", 0.0) == 3,
    (rp.pages_at(CH, 1, 0.0), rp.pages_at(CH, "1", 0.0)))
chk("越界的章序号照旧算读完全书", rp.pages_at(CH, 99, 0.0) == rp.total_pages(CH))
chk("负的章序号仍夹到第 0 章", rp.pages_at(CH, -3, 0.0) == 0)

# ── 2 纯函数：record_pos 收到脏 at 也不许抛、不许飘 ─────────────────
def fresh():
    return rp.put_plan({"plans": {}}, "B1", "探针", rp.total_pages(CH),
                       "daily", 2, now=1_800_000_000, target="2026-01-01")


for v in BAD_AT:
    st = {"plans": {"B1": fresh()}}
    try:
        rp.record_pos(st, "B1", CH, v, 0.5, now=1_800_000_000)
    except Exception as e:                       # noqa: BLE001
        raised.append(("record_pos", repr(v), type(e).__name__))
chk("record_pos 收到脏 at 也不抛", not any(r[0] == "record_pos" for r in raised), raised)

st = {"plans": {"B1": fresh()}}
p = rp.record_pos(st, "B1", CH, "abc", 0.9, now=1_800_000_000)
chk("脏 at 存下来时收成 0", "at" in p and int(p["at"]) == 0, p.get("at"))
chk("脏 at 不把书记成读完", not p.get("done_at"), p.get("done_at"))
chk("脏 at 之后读取仍在高水位之内", int(p.get("high") or 0) <= rp.total_pages(CH), p.get("high"))

# ── 3 真机：脏读数打不打得到服务（路由那层没有兜底）────────────────
SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-plan-input-"))
atexit.register(lambda: shutil.rmtree(SB, ignore_errors=True))
for _d in ("data", "books", "shots"):
    (SB / _d).mkdir(parents=True, exist_ok=True)

# 铺一本有正文的书：计划要能按章折算页数，才走得到 pages_at。
_bd = os.path.join(book_layout.book_dir(str(SB / "books"), "local"), "imp_probe")
os.makedirs(os.path.join(_bd, "chapters"), exist_ok=True)
with open(os.path.join(_bd, "meta.json"), "w", encoding="utf-8") as f:
    json.dump({"title": "探针", "source": "local"}, f, ensure_ascii=False)
with open(os.path.join(_bd, "chapters", "0001.md"), "w", encoding="utf-8") as f:
    f.write("# 一\n\n" + "字" * 2000 + "\n")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


env = dict(os.environ, GUIZANG_SELFTEST_DIR=str(SB), GUIZANG_DATA=str(SB / "data"),
           GUIZANG_BOOKS=str(SB / "books"), GUIZANG_SHOT_DIR=str(SB / "shots"),
           GUIZANG_ANNA_NO_WINDOW="1", PYTHONUNBUFFERED="1")
port = _free_port()
logp = SB / "server.log"
logf = open(logp, "w")
srv = subprocess.Popen([sys.executable, "ui_server.py", "--port", str(port)],
                       cwd=str(REPO), env=env, stdout=logf, stderr=subprocess.STDOUT)
atexit.register(lambda: srv.terminate())
base = ""
for _ in range(160):
    m = re.search(r"http://127\.0\.0\.1:(\d+)", logp.read_text(errors="replace"))
    if m:
        try:
            urllib.request.urlopen("http://127.0.0.1:%s/api/state" % m.group(1),
                                   timeout=1).read()
            base = "http://127.0.0.1:%s" % m.group(1)
            break
        except Exception:
            pass
    time.sleep(0.25)


def post(payload):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base + "/api/plan", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.status, json.loads(r.read().decode("utf-8", "replace"))


if not base:
    chk("真机：服务起来了", False, logp.read_text(errors="replace")[-300:])
else:
    chk("真机：服务起来了", True)
    code, out = post({"act": "set", "book": "imp_probe", "period": "daily", "pages": 2})
    chk("真机：先把计划定上", code == 200 and out.get("ok"), (code, out))
    code, out = post({"act": "pos", "book": "imp_probe", "at": 0, "frac": 0.5})
    chk("真机：正常读数照常做得成", code == 200 and out.get("ok"), (code, out))

    ok, msg = True, ""
    try:
        code, out = post({"act": "pos", "book": "imp_probe", "at": "abc", "frac": 0.9})
    except Exception as e:                       # 连接被甩时 urlopen 抛的就是它
        ok, msg = False, "%s: %s" % (type(e).__name__, e)
    chk("真机：脏读数也回 200 + JSON（不是连接被甩）", ok and code == 200 and out.get("ok"),
        msg or (code, out))

    try:
        code, out = post({"act": "get", "book": "imp_probe"})
        done = bool((out.get("data") or {}).get("done_at"))
    except Exception as e:
        code, done = 0, "%s: %s" % (type(e).__name__, e)
    chk("真机：脏读数没把书记成读完", code == 200 and not done, (code, done))
    chk("真机：服务还活着（日志里没有未捕获异常）",
        "Traceback" not in logp.read_text(errors="replace"))
    srv.terminate()

print()
if FAIL:
    print("阅读计划脏读数门禁有 %d 处不对：%s" % (len(FAIL), "、".join(FAIL)))
else:
    print("阅读计划脏读数：认不出的章序号一律当第 0 章，纯函数不抛、真机不断线。")
sys.exit(len(FAIL))
