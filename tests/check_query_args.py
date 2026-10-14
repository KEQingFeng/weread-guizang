#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查询串里的数字脏值不许把连接掐断（do_GET 的四条路由）。

用户报过的「点按钮没反应」在后端有一种非常难查的形态：地址里的数字被手改坏
（`/api/log?tail=abc`、`/api/notes_random?n=abc`），`int(q.get(...)[0])` 当场抛
ValueError，异常一路穿出 do_GET —— BaseHTTPRequestHandler 不会替你回 500，
它只是把连接关掉。前端 `await fetch` 收到的是 RemoteDisconnected，onclick 没有
catch，屏幕上就是「按下去毫无动静」；而服务本身还活着，重启一次也照样复现。

收口是 ui_server._qint()：解不出（abc / 空串 / 小数 / inf / 非标量）回默认值，
再按 lo/hi 夹住。真机上确认过修复前后：`?tail=abc` 由 HTTP 000（断连）变成 200 +
正常 JSON。这一条是不依赖浏览器的静态门禁，所以归 --fast。

为什么不塞进 smoke_api.py：那份要起真服务、归慢套餐；这里的重点是「四个调用点
全部改用它」，源码走查一把就够，改坏了也当场红。
"""
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import selftest  # noqa: E402

sys.path.insert(0, str(selftest.REPO))    # 进程内 import ui_server 验后端
selftest.export_sandbox_env()             # 必须在 import ui_server 之前设好环境变量

import ui_server  # noqa: E402

SERVER = (selftest.REPO / "ui_server.py").read_text(encoding="utf-8")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


# ── ① 纯函数：脏值回默认、好值原样、越界夹住 ─────────────────────
q = ui_server._qint
chk("干值 abc 回默认（不抛）", q("abc", 80) == 80, q("abc", 80))
chk("空串回默认（?tail= 是 keep_blank_values 放出来的）", q("", 80) == 80, q("", 80))
chk("None 回默认（键没传）", q(None, 80) == 80, q(None, 80))
chk("正常数字原样收，前后空白也认", q("12", 80) == 12 and q(" 12 ", 80) == 12,
    (q("12", 80), q(" 12 ", 80)))
chk("负数也认（要不要夹由调用方决定）", q("-3", 80) == -3, q("-3", 80))
chk("小数不当整数用（3.7 回默认，不截成 3）", q("3.7", 80) == 80, q("3.7", 80))
chk("inf / nan 回默认（int() 会抛，仍是脏值）",
    q("inf", 80) == 80 and q("nan", 80) == 80, (q("inf", 80), q("nan", 80)))
chk("非标量（同一个键传两遍）当脏值，不抛 unhashable",
    q(["1", "2"], 80) == 80, q(["1", "2"], 80))
chk("lo / hi 两端都夹得住",
    q("-5", 0, lo=1, hi=30) == 1 and q("999", 0, lo=1, hi=30) == 30,
    (q("-5", 0, lo=1, hi=30), q("999", 0, lo=1, hi=30)))

# ── ② 四个调用点全部改用它 ──────────────────────────────────────
GET = SERVER[SERVER.index("    def do_GET(self):"):SERVER.index("    def do_POST(self):")]

chk("/api/log 的 tail / since 走 _qint",
    '_qint(q.get("tail"' in GET and '_qint(q.get("since"' in GET,
    [l.strip() for l in GET.splitlines() if "tail" in l or "since" in l][:4])
chk("/api/notes_search 的 limit 走 _qint",
    '_qint(q.get("limit"' in GET, [l.strip() for l in GET.splitlines() if "limit" in l][:3])
chk("/api/notes_random 的 n 走 _qint，且仍夹在 1..30",
    '_qint(q.get("n", ["6"])[0], 6, lo=1, hi=30)' in GET,
    [l.strip() for l in GET.splitlines() if "notes_random" in l][:2])

# 这一条是这份门禁的真正价值：以后新加路由时又写回裸 int(...) 会当场红。
bare = re.findall(r"(?<![A-Za-z0-9_])int\(\s*q\.get\(", GET)
chk("do_GET 里不再有裸 int(q.get(...))（脏值会穿出去掐连接）", not bare, bare)
bare_one = re.findall(r"(?<![A-Za-z0-9_])int\(\s*one\(", GET)
chk("do_GET 里也不再有裸 int(one(...))", not bare_one, bare_one)

bad = [c for c in checks if not c[0]]
print("\n查询参数脏值体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
