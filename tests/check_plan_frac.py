# -*- coding: utf-8 -*-
"""阅读计划的「脏比例」门禁：NaN / Infinity 不许被当成「这一章读到头了」。

与 check_plan_input.py 是同一件事的两半：那一条管 `at`（章序号）认不出时当第 0 章，
这一条管 `frac`（章内读到几成）认不出时当 0（没读到）。两处的错法不一样 ——

  · `at` 是裸 `int()` 抛异常，症状是连接被甩；
  · `frac` 的收口写着 `max(0.0, min(1.0, frac))`，而 Python 的 `min(1.0, nan)`
    回的是 **1.0**（nan 与谁比都为假，min 留着先到的那一个）。`json` 认 `NaN`
    与 `Infinity` 这两个字面量，`float("1e999")` 也回 inf，于是「认不出的比例」
    被读成「读到章尾」。

最坏的一种不是今日的账多记几页，而是 `record_pos` 最后那段：
心跳报在读到的最后一章、比例又是个 NaN，`pages_at` 把章尾当读完，
`high` 刚好凑满总页数 —— 书当场被判成读完并 `done_at` 留档封存，
用户只是翻着书，计划却自己结束了。盘上那份账本被手改 / 云同步写回一个
`"frac": NaN` 时，`load()` 也会把它收成 1.0，同样虚增高水位。

所以这一条钉三件事，全部离线、不联网、不开浏览器：

  1. 纯函数：`pages_at` 与 `_clamp01` 收到 NaN / ±inf / `"1e999"` 这类值一律当 0，
     正常的数与数值字符串一个不误伤；
  2. 账本：脏 `frac` 读回来是 0（不是 1），条目其余部分照旧；
  3. 真机：拿 `frac: NaN` 打一次 `/api/plan`，回的必须是 200 + JSON，且书不能被
     记成读完（这一条在收口前一定红）。

不起浏览器，归静态段，--fast 也得带上。
"""
import atexit
import json
import math
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


# ── 1 纯函数：认不出的比例一律当 0，正常值不动 ─────────────────────
CH = [{"chars": 3000}, {"chars": 1000}]          # 4 页，2 章
LAST = len(CH) - 1
BAD_FRAC = [float("nan"), float("inf"), float("-inf"), "nan", "NaN", "inf",
            "Infinity", "-inf", "1e999", "-1e999", math.inf]

# 认不出的比例 = 没读到：与「同一章、比例 0」逐字等价，绝不等价于读完。
same_as_zero = all(rp.pages_at(CH, LAST, v) == rp.pages_at(CH, LAST, 0.0)
                   for v in BAD_FRAC)
chk("认不出的比例与「比例 0」等价（不当前进）", same_as_zero,
    [(repr(v), rp.pages_at(CH, LAST, v)) for v in BAD_FRAC])

chk("认不出的比例不当读完全书（不是 total_pages）",
    all(rp.pages_at(CH, LAST, v) != rp.total_pages(CH) for v in BAD_FRAC),
    [(repr(v), rp.pages_at(CH, LAST, v)) for v in BAD_FRAC])

raised = []
for v in BAD_FRAC:
    try:
        rp.pages_at(CH, LAST, v)
    except Exception as e:                       # noqa: BLE001 —— 就是来找它抛不抛
        raised.append((repr(v), type(e).__name__))
chk("认不出的比例一个都不抛", not raised, raised)


# `_clamp01` 是另一个入口（load 与 record_pos 存 frac 都用它），同一个坑。
chk("_clamp01：NaN / ±inf / 越界串都当 0",
    rp._clamp01(float("nan")) == 0.0 and rp._clamp01(float("inf")) == 0.0
    and rp._clamp01(float("-inf")) == 0.0 and rp._clamp01("1e999") == 0.0
    and rp._clamp01("nan") == 0.0,
    [rp._clamp01(v) for v in (float("nan"), float("inf"), float("-inf"), "1e999", "nan")])

chk("_clamp01：正常值口径一点不改",
    rp._clamp01(0.5) == 0.5 and rp._clamp01("0.25") == 0.25
    and rp._clamp01(5) == 1.0 and rp._clamp01(-3) == 0.0
    and rp._clamp01(None) == 0.0 and rp._clamp01("abc") == 0.0,
    [rp._clamp01(v) for v in (0.5, "0.25", 5, -3, None, "abc")])

chk("正常的比例没被误伤（章尾仍是章尾）",
    rp.pages_at(CH, LAST - 1, 1.0) == 3 and rp.pages_at(CH, 0, 0.5) == 2
    and rp.pages_at(CH, LAST, 5) == rp.total_pages(CH),
    (rp.pages_at(CH, LAST - 1, 1.0), rp.pages_at(CH, 0, 0.5), rp.pages_at(CH, LAST, 5)))

# ── 2 账本：手改 / 云同步写回的脏 frac 收成 0，条目仍在 ────────────
dirty = {"plans": {"B1": {"title": "探针", "pages": 4, "goal": {"period": "daily", "pages": 2},
                          "started": "2026-10-01", "high": 1, "frac": float("nan")}}}
loaded = rp.load(dirty)
entry = (loaded.get("plans") or {}).get("B1") or {}
chk("脏账本里的 frac 收成 0（不是 1）", entry.get("frac") == 0.0, entry.get("frac"))
chk("脏账本里的条目没被丢掉（标题 / 高水位照旧）",
    entry.get("title") == "探针" and int(entry.get("high") or 0) == 1, entry)

# ── 3 纯函数：record_pos 收到 NaN 比例不把书判成读完 ───────────────
def fresh():
    return rp.put_plan({"plans": {}}, "B1", "探针", rp.total_pages(CH),
                       "daily", 2, now=1_800_000_000, target="2026-01-01")


st = {"plans": {"B1": fresh()}}
p = rp.record_pos(st, "B1", CH, LAST, float("nan"), now=1_800_000_000)
chk("NaN 比例落在最后一章：书没被记成读完", not p.get("done_at"), p.get("done_at"))
chk("NaN 比例存下来时收成 0", p.get("frac") == 0.0, p.get("frac"))
chk("NaN 比例没把高水位顶到总页数",
    int(p.get("high") or 0) < rp.total_pages(CH) and int(p.get("high") or 0) >= 0,
    p.get("high"))

st = {"plans": {"B1": fresh()}}
p = rp.record_pos(st, "B1", CH, LAST, "1e999", now=1_800_000_000)
chk("字符串越界值（\"1e999\"）同样不判读完", not p.get("done_at"), p.get("done_at"))

# 正常的读完仍要判得出来：最后一章 + 比例 1 照旧留档封存。
st = {"plans": {"B1": fresh()}}
p = rp.record_pos(st, "B1", CH, LAST, 1.0, now=1_800_000_000)
chk("真读到章尾照旧判读完并留档",
    bool(p.get("done_at")) and len(p.get("history") or []) == 1,
    (p.get("done_at"), p.get("history")))

# ── 4 真机：NaN 比例打不打得到服务，会不会把计划封存 ───────────────
SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-plan-frac-"))
atexit.register(lambda: shutil.rmtree(SB, ignore_errors=True))
for _d in ("data", "books", "shots"):
    (SB / _d).mkdir(parents=True, exist_ok=True)

# 两章的书：`at` 落在最后一章，NaN 比例一旦被当章尾就会凑满总页数。
_bd = os.path.join(book_layout.book_dir(str(SB / "books"), "local"), "frac_probe")
os.makedirs(os.path.join(_bd, "chapters"), exist_ok=True)
with open(os.path.join(_bd, "meta.json"), "w", encoding="utf-8") as f:
    json.dump({"title": "探针", "source": "local"}, f, ensure_ascii=False)
with open(os.path.join(_bd, "chapters", "0001.md"), "w", encoding="utf-8") as f:
    f.write("# 一\n\n" + "字" * 3000 + "\n")
with open(os.path.join(_bd, "chapters", "0002.md"), "w", encoding="utf-8") as f:
    f.write("# 二\n\n" + "字" * 1000 + "\n")


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


def post(payload, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base + "/api/plan", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.status, json.loads(r.read().decode("utf-8", "replace"))


if not base:
    chk("真机：服务起来了", False, logp.read_text(errors="replace")[-300:])
else:
    chk("真机：服务起来了", True)
    code, out = post({"act": "set", "book": "frac_probe", "period": "daily", "pages": 1})
    chk("真机：先把计划定上", code == 200 and out.get("ok"), (code, out))

    # `json.dumps` 默认就印 `NaN` / `Infinity` 这两个裸字面量，`json.loads` 也收得下。
    ok, msg = True, ""
    try:
        code, out = post({"act": "pos", "book": "frac_probe", "at": 1, "frac": float("nan")})
    except Exception as e:                       # 连接被甩时 urlopen 抛的就是它
        ok, msg = False, "%s: %s" % (type(e).__name__, e)
    chk("真机：NaN 比例回 200 + JSON（不是连接被甩）", ok and code == 200 and out.get("ok"),
        msg or (code, out))

    try:
        code, out = post({"act": "get", "book": "frac_probe"})
        data = out.get("data") or {}
        done = bool(data.get("done_at"))
    except Exception as e:
        code, done = 0, "%s: %s" % (type(e).__name__, e)
    chk("真机：NaN 比例没把计划记成读完", code == 200 and not done, (code, done))

    # 裸字面量那一档（不经 json.dumps 的转义）：同样是合法 JSON 体。
    ok2, msg2 = True, ""
    try:
        code, out = post(None, raw=b'{"act":"pos","book":"frac_probe","at":1,"frac":Infinity}')
    except Exception as e:
        ok2, msg2 = False, "%s: %s" % (type(e).__name__, e)
    chk("真机：裸 Infinity 字面量也回 200 + JSON",
        ok2 and code == 200 and out.get("ok"), msg2 or (code, out))

    code, out = post({"act": "get", "book": "frac_probe"})
    chk("真机：裸 Infinity 也没把计划记成读完",
        code == 200 and not ((out.get("data") or {}).get("done_at")), (code, out))

    # 正常读数照旧：这个口子别被收口收紧了。
    code, out = post({"act": "pos", "book": "frac_probe", "at": 0, "frac": 0.5})
    chk("真机：正常比例照常做得成", code == 200 and out.get("ok"), (code, out))
    code, out = post({"act": "get", "book": "frac_probe"})
    chk("真机：正常比例确实记进去了",
        code == 200 and int((out.get("data") or {}).get("read") or 0) >= 1, (code, out))
    chk("真机：服务还活着（日志里没有未捕获异常）",
        "Traceback" not in logp.read_text(errors="replace"))
    srv.terminate()

print()
if FAIL:
    print("阅读计划脏比例门禁有 %d 处不对：%s" % (len(FAIL), "、".join(FAIL)))
else:
    print("阅读计划脏比例：NaN / Infinity 一律当没读到，计划不会凭空封存。")
sys.exit(len(FAIL))
