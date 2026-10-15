#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`tools/pr_triage.py` 的「自带证明」那一栏：改了产品代码有没有带一条离线门禁。

为什么单开一条
    这个仓库的规矩是「修一处坑，补一条离线门禁」—— 翻历史提交，几乎每条产品修复的
    标题尾巴都写着「（补离线门禁）」。一批 auto/* 里两条改动看着都对，差别往往就在
    一条能自动重放的证明上：带门禁的那条，坑再犯会红；没带的那条，只能靠人这次读仔细。

    这一栏是给「合 PR 之前」用的，平时不在提交流程里跑，坏了也没人知道 —— 所以把
    三个判定（有 / 缺 / 不适用）连同几条边角一起离线钉住，纯函数喂输入，不联网、
    不碰真仓库、不开浏览器，归 --fast 静态段。

钉的是「分类对不对」，不是「该不该合」：报「缺」不等于它错，只说明这条没有可自动
重放的证明。所以这里既不把「缺」算进红线，也不替人下判决。
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "tools"))

import pr_triage as t  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def diff(*entries):
    """拼一段最小 diff：entries = (路径, [(老起点, 老长度, 新长度, [新增行…])…])。

    老长度 0 就是「新文件 / 纯插入」；新长度为 0 就是「只删行」。
    """
    out = []
    for path, hunks in entries:
        out.append("diff --git a/%s b/%s" % (path, path))
        for old_start, old_len, new_len, added in hunks:
            out.append("@@ -%d,%d +%d,%d @@" % (old_start, old_len, old_start + 1, new_len))
            for line in added:
                out.append("+" + line)
    return "\n".join(out) + "\n"


# ── A 有门禁：产品代码 + 一条新的 check_*.py ─────────────────────────
got = t.gate_evidence(t.parse_hunks(diff(
    ("ui_server.py", [(100, 0, 2, ["x = 1", "y = 2"])]),
    ("tests/check_new.py", [(0, 0, 1, ['chk("新门禁", True)'])]),
)))
chk("A1 动了产品代码又带了新门禁 → 有", got[0] == "有", got)
chk("A2 「有」把门禁文件名点出来（人要能直接去读那条证明）",
    "tests/check_new.py" in got[1], got)

# 改已有门禁（老长度 1、加了行）也算带证明；test_*.py 与 check_*.py 同等看待。
got = t.gate_evidence(t.parse_hunks(diff(
    ("feed.py", [(3, 0, 1, ["z = 3"])]),
    ("tests/test_platform_compat.py", [(5, 1, 2, ["new_line = 1"])]),
)))
chk("A3 改已有门禁（不只是新建）也算「有」", got[0] == "有", got)

# ── B 缺门禁：碰了产品代码，一处门禁都没动 ───────────────────────────
got = t.gate_evidence(t.parse_hunks(diff(
    ("ui_server.py", [(100, 0, 1, ["x = 1"])]),
)))
chk("B1 动了产品代码却一处门禁都没动 → 缺", got[0] == "缺", got)
chk("B2 「缺」把碰过的产品文件点出来（不是一句空话）",
    "ui_server.py" in got[1], got)

got = t.gate_evidence(t.parse_hunks(diff(
    ("ui.html", [(1, 0, 1, ["<div></div>"])]),
)))
chk("B3 只动界面 ui.html 没带门禁 → 缺", got[0] == "缺", got)

got = t.gate_evidence(t.parse_hunks(diff(
    ("mcp/guizang-mcp.mjs", [(1, 0, 1, ["const a = 1;"])]),
)))
chk("B4 只动 MCP 适配器没带门禁 → 缺", got[0] == "缺", got)

# 只删产品代码里的死代码、同样没带证明 —— 删也是改，不该被漏掉。
got = t.gate_evidence(t.parse_hunks(
    "diff --git a/feed.py b/feed.py\n@@ -10,1 +10,0 @@\n-old = 1\n"))
chk("B5 只删产品代码（没新增行）也算动了 → 缺", got[0] == "缺", got)

# ── C 不适用：本来就不带门禁的那几类 ────────────────────────────────
for path in ("README.md", "docs/部署说明.md", "tools/pr_triage.py", "shell/main.swift",
             "tests/run_all.sh"):
    got = t.gate_evidence(t.parse_hunks(diff((path, [(1, 0, 1, ["x"])]))))
    chk("C 只动 %s → 不适用" % path, got[0] == "不适用", (path, got))

# 空 diff / 什么都不碰也不该报「缺」。
chk("C 空改动 → 不适用（门禁那一栏不替空分支喊话）",
    t.gate_evidence([])[0] == "不适用", t.gate_evidence([]))

# 只登记 run_all.sh、没有新门禁文件：不算一条新证明（登记那件事另有硬门禁管）。
got = t.gate_evidence(t.parse_hunks(diff(
    ("tests/run_all.sh", [(50, 0, 1, ['run "x" "$PY" tests/check_a.py'])])
)))
chk("C 只改 run_all.sh 登记行 → 不适用（不是新证明，也不在产品代码里）",
    got[0] == "不适用", got)

# ── D 对照：这一栏真的分得开「有」和「缺」─────────────────────────────
# 两个样本只差一个 tests/check_*.py：分类必须跟着变，否则这一栏是空转的。
base = ("ui_server.py", [(100, 0, 1, ["x = 1"])])
without = t.gate_evidence(t.parse_hunks(diff(base)))
with_gate = t.gate_evidence(t.parse_hunks(diff(
    base, ("tests/check_x.py", [(0, 0, 1, ["chk('a', True)"])]))))
chk("D1 对照：只差一条门禁文件，分类从「缺」变「有」",
    (without[0], with_gate[0]) == ("缺", "有"), (without, with_gate))

# 「只有删除行的门禁文件」不算一条证明：它可能是把已有断言删掉，不该被当成带证明。
got = t.gate_evidence(t.parse_hunks(
    "diff --git a/feed.py b/feed.py\n@@ -1,0 +2,1 @@\n+x = 1\n"
    "diff --git a/tests/check_old.py b/tests/check_old.py\n@@ -5,1 +5,0 @@\n-chk('gone', True)\n"))
chk("D2 门禁文件只在删行 → 不算「有」（删断言不是证明）", got[0] == "缺", got)

print("\nPR 预扫描·自带证明：%d 项通过，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for n in FAIL:
        print("  ✗ " + n)
sys.exit(1 if FAIL else 0)
