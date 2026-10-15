#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""写作导出：地址上的 scale 写脏不许把这一趟导出 500 掉。

`/api/writer/export?kind=png&id=…&scale=abc` 这条路上，scale 是从查询串来的
字符串。原来的写法是

    scale = max(1, min(int(opts.get("scale") or 2), 3))

手改地址（`scale=abc` / `scale=1e400` / `scale=nan`）时 `int()` 当场抛
ValueError，`do_GET` 里那条兜底把它接住，但接住之后回的是 500 加一句

    导出没做成：invalid literal for int() with base 10: 'abc'

—— 用户点「导出图片」看见的是 Python 的报错原文，而不是「这一档是多少」。
界面上只有 1/2/3 三档，脏值该当没传：回缺省 2，再夹在 1..3。这跟 `_qint` /
`_body_int` 立下的口径是同一把尺子（见 tests/check_query_args.py）。

为什么不去起真服务走 HTTP：`/api/writer/export` 在调 `writer_export` 之前先有
一道 `chromium_ready()` 前置拦，离线门禁里铬内核没装好时那条路回的是 400，
根本到不了 scale 这一段。所以这里在 `writer_export()` 这一层直打：
把 `writer_source` / `writer_mod.export_image` 换成探针，验「传进去的 scale
是几」，既不起浏览器也不碰真实书库。
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


# ── 探针：截住 writer_source 与 export_image，只关心 scale 收到几 ──────
_SEEN = {"scale": None}


def _fake_source(book_id):
    return {"text": "# 标题\n正文一段\n", "title": "假稿", "updated": 0, "is_book": False}


def _fake_image(html, dest, width=760, scale=2):
    _SEEN["scale"] = scale
    return dest, True


ui_server.writer_source = _fake_source
ui_server.writer_mod.export_image = _fake_image


def scale_of(raw):
    """把 opts 里的 scale 原样喂给 writer_export，返回它实际用的那个数；抛错回 'RAISED'。"""
    _SEEN["scale"] = None
    opts = {} if raw is None else {"scale": raw}
    opts["tpl"] = "plain"
    try:
        ui_server.writer_export("png", "x", opts)
    except Exception as e:                    # noqa: BLE001 —— 门禁要看的就是「抛没抛」
        return "RAISED:%s" % type(e).__name__
    return _SEEN["scale"]


# ── ① 脏值当没传：回缺省 2，绝不抛 ────────────────────────────────
for raw in ("abc", "1e400", "nan", "inf", "3.7", "-", " ", "0x10", "２"):
    got = scale_of(raw)
    chk("scale=%r 当没传（回 2，不抛）" % raw, got == 2, got)

# 同一个键传两遍在路由里已经被 {k: v[0]} 收成标量；这里也钉住非标量不抛。
chk("scale 是非标量（列表）也不抛，回 2", scale_of(["1", "2"]) == 2, scale_of(["1", "2"]))
chk("scale 是对象也不抛，回 2", scale_of({}) == 2, scale_of({}))
chk("没传 scale 回缺省 2", scale_of(None) == 2, scale_of(None))
chk("scale 空串回缺省 2", scale_of("") == 2, scale_of(""))

# ── ② 好值原样、越界夹在 1..3 ─────────────────────────────────────
chk("scale=2 原样收", scale_of("2") == 2, scale_of("2"))
chk("scale=1 原样收", scale_of("1") == 1, scale_of("1"))
chk("scale=3 原样收", scale_of("3") == 3, scale_of("3"))
chk("scale=9 夹到 3（不放大到 9 倍白等一趟渲染）", scale_of("9") == 3, scale_of("9"))
chk("scale=0 夹到 1", scale_of("0") == 1, scale_of("0"))
chk("scale=-5 夹到 1", scale_of("-5") == 1, scale_of("-5"))
chk("scale 前后空白也认（' 2 ' 是 2）", scale_of(" 2 ") == 2, scale_of(" 2 "))

# ── ③ 源码走查：这一处接线用 _qint，别退回裸 int() ────────────────
chk("writer_export 的 scale 走 _qint（同一把尺子）",
    '_qint(opts.get("scale"), 2, lo=1, hi=3)' in SERVER,
    [l.strip() for l in SERVER.splitlines() if "opts.get(\"scale\")" in l][:3])
# 负向后顾要有：`_qint(` 的尾巴本来就是 `int(`，不看前一个字符会把自己也判成裸的。
_bare = re.findall(r'(?<![A-Za-z0-9_])int\(\s*opts\.get\("scale"\)', SERVER)
chk("src 里不再有裸 int(opts.get(\"scale\"))",
    not _bare,
    [l.strip() for l in SERVER.splitlines() if 'int(opts.get("scale")' in l][:3])

bad = [c for c in checks if not c[0]]
print("\n写作导出参数体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
