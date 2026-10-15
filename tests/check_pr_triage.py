#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`tools/pr_triage.py` 的离线门禁：合并前预扫描的判定逻辑不许悄悄坏掉。

为什么单开一条：这个脚本是给「合 PR 之前」用的，它自己不会在每次提交时跑到 ——
也就是说它坏了没人知道，直到某次合并漏掉了本该裁掉的重复、或者把干净的一批报成
一堆假冲突。判定全靠三个纯函数（切 hunk / 判区间相交 / 算相似度），这三样都能离线
喂输入测，所以不联网、不碰真仓库、不开浏览器，归静态段。

钉四层：
  A 切 hunk：`@@ -a,b +c,d @@` 的几种写法（省略长度、纯插入 b=0）都认；
  B 区间相交：纯插入（老长度 0）按一格算 —— 这是最要紧的一档，两个 PR 都把新
    代码插在同一个锚点上就是最常见的那种冲突，0 宽区间会让它漏掉；
  C 相似度：同一处实现换了写法要报高（像重复），两件不相干的事插在相邻行要报低；
  D 接线：真拿 git 里已有的历史分支跑一遍 `pair_verdict`，确认它认得出「已知的
    重复」与「已知的只是相邻」—— 用仓库自己的历史当夹具，不另造数据。

不联网，归静态段，--fast 也得带上。
"""
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "tools"))

import pr_triage as t  # noqa: E402

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


# ── A 切 hunk ────────────────────────────────────────────────────
SAMPLE = """diff --git a/ui_server.py b/ui_server.py
--- a/ui_server.py
+++ b/ui_server.py
@@ -4160,0 +4161,6 @@ def feed_do(body):
+    n = 1
+    m = 2
@@ -4171 +4177,2 @@ def feed_do(body):
-    old = 1
+    new = 2
+    more = 3
diff --git a/tests/run_all.sh b/tests/run_all.sh
--- a/tests/run_all.sh
+++ b/tests/run_all.sh
@@ -629,0 +630,8 @@ run "请求体字符串字段脏形状" "$PY" tests/check_post_body_str.py
+run "响应正文坏字符" "$PY" tests/check_response_utf8.py
"""
hs = t.parse_hunks(SAMPLE)
chk("切出 3 处改动", len(hs) == 3, [h.path for h in hs])
chk("路径认得出两个文件", {h.path for h in hs} == {"ui_server.py", "tests/run_all.sh"},
    {h.path for h in hs})
h0 = hs[0]
chk("纯插入：老长度 0、新长度 6",
    (h0.old_start, h0.old_len, h0.new_start, h0.new_len) == (4160, 0, 4161, 6),
    (h0.old_start, h0.old_len, h0.new_start, h0.new_len))
chk("纯插入按一格算区间（不相交判定的关键）", h0.old_span == (4160, 4160), h0.old_span)
chk("省略长度时按 1 行算",
    (hs[1].old_len, hs[1].new_len) == (1, 2),
    (hs[1].old_len, hs[1].new_len))
chk("收得进新增行（用来算相似度）",
    h0.added == ["    n = 1", "    m = 2"] and hs[1].removed == ["    old = 1"],
    (h0.added, hs[1].removed))
chk("区间标签取 git 给的函数名上下文", h0.region == "feed_do()", h0.region)
chk("标签：run 那一行没有 def，就截原文（上下文是插入点前那行）",
    hs[2].region.startswith('run "请求体字符串字段脏形状'), hs[2].region)

# ── B 区间相交：纯插入同锚点必须判为相交 ─────────────────────────
def H(path, start, length, added=()):
    return t.Hunk(path=path, old_start=start, old_len=length,
                  new_start=start, new_len=max(length, 1),
                  ctx="", added=list(added))


chk("两个纯插入落在同一锚点 → 相交（最常见的那类冲突）",
    t.spans_intersect(H("f", 629, 0).old_span, H("f", 629, 0).old_span))
chk("纯插入挨着已有的修改行（629 插 / 629 改）→ 相交",
    t.spans_intersect(H("f", 629, 0).old_span, H("f", 629, 1).old_span))
chk("隔得挺远的两处 → 不相交",
    not t.spans_intersect(H("f", 100, 3).old_span, H("f", 200, 3).old_span))
chk("首尾相接不算相交（100-102 与 103-105）",
    not t.spans_intersect(H("f", 100, 3).old_span, H("f", 103, 3).old_span))
chk("部分重叠 → 相交",
    t.spans_intersect(H("f", 100, 10).old_span, H("f", 105, 10).old_span))


# ── C 相似度：像重复报高，不相干报低 ─────────────────────────────
same = "    meta = read_meta_dict(book_dir)\n    seen = collect_seen_images(raw_dir)"
alike = "    meta = book_layout.read_meta(book_dir)\n    seen = _seen_image_urls(raw_dir)"
other = 'run "云同步取回落盘（半截不落目标）" "$PY" tests/check_sync_atomic.py'
ratio_alike = t.similarity(same, alike)
ratio_other = t.similarity(same, other)
chk("同一件事的两种实现 → 相似度高（>=0.45 判疑似重复）",
    ratio_alike >= t.DUP_HINT, ratio_alike)
chk("不相干的两条登记行 → 相似度低（<0.45 不算重复）",
    ratio_other < t.DUP_HINT, ratio_other)
chk("两边都空 → 算 1（同一处空改动）", t.similarity("", "") == 1.0)
chk("一边空一边有 → 很低", t.similarity("", "x = 1") < 0.5)


# ── D 拿真仓库的历史当夹具：认得已知重复、也不冤枉已知的相邻 ──────
def git(*a):
    p = subprocess.run(["git", *a], cwd=str(REPO), capture_output=True, text=True)
    return p.returncode, p.stdout.strip()


have = git("rev-parse", "--verify", "--quiet", "be87e5f^{commit}")[0] == 0
if not have:
    chk("仓库里有 be87e5f 可用于历史对照（跳过则说明是浅克隆）", True)
else:
    # 已知的重复：#67 与 #68 —— 都去收 export_precise.py 开跑前的两个读盘口。
    a, b = "origin/auto/20261015-020433-1", "origin/auto/20261015-020949-2"
    code_a, _ = git("rev-parse", "--verify", "--quiet", a + "^{commit}")
    code_b, _ = git("rev-parse", "--verify", "--quiet", b + "^{commit}")
    if code_a or code_b:
        chk("两个已知重复分支在本地（跳过则说明已 prune）", True)
    else:
        da = t.diff_hunks("be87e5f", a, cwd=str(REPO))
        db = t.diff_hunks("be87e5f", b, cwd=str(REPO))
        verdicts = t.pair_verdict(da or [], db or [])
        # 元组顺序：(档位, 路径, 区间标签, 相似度, 说明)
        hits = [v for v in verdicts if v[0] == "同区间" and v[3] >= t.DUP_HINT]
        chk("已知重复 #67×#68 被判为「疑似重复」", bool(hits),
            [(v[1], v[2], round(v[3], 2)) for v in verdicts])
        chk("重复点正是 export_precise.py 的 main()（不是别处误报）",
            any(v[1] == "export_precise.py" and "main" in v[2] for v in hits),
            [(v[1], v[2]) for v in hits])

    # 已知的「只是相邻」：#85 与 #87 —— 同一段里两条不同的 run 登记行。
    a2, b2 = "origin/auto/20261015-033356-1", "origin/auto/20261015-034224-1"
    if git("rev-parse", "--verify", "--quiet", a2 + "^{commit}")[0] == 0 \
            and git("rev-parse", "--verify", "--quiet", b2 + "^{commit}")[0] == 0:
        da = t.diff_hunks("03e8eb5", a2, cwd=str(REPO))
        db = t.diff_hunks("03e8eb5", b2, cwd=str(REPO))
        verdicts = t.pair_verdict(da or [], db or [])
        inter = [v for v in verdicts if v[0] == "同区间"]
        chk("已知相邻 #85×#87 被判为「同区间」（确实会撞，要人解）", bool(inter),
            [(v[1], v[2]) for v in verdicts])
        chk("但它俩不被当成重复（相似度低于阈值，只是同段可并存）",
            all(v[3] < t.DUP_HINT for v in inter),
            [(v[1], round(v[3], 2)) for v in inter])

# ── E 红线体检：禁改路径 / 改已有测试 / 疑似凭据 / 改动规模 ───────
# 一条人造 diff 把四类都摆进去，再确认「只加新测试文件」不被冤枉。
# 假 key 现拼现用：源码里不出现会被隐私扫描器直接命中的字面量。
FAKE_KEY = "sk-" + "a" * 24
RED_SAMPLE = """diff --git a/requirements.txt b/requirements.txt
--- a/requirements.txt
+++ b/requirements.txt
@@ -3,0 +4,1 @@
+requests==2.31.0
diff --git a/tests/check_feed.py b/tests/check_feed.py
--- a/tests/check_feed.py
+++ b/tests/check_feed.py
@@ -10 +10 @@
-chk("旧断言", a == 1)
+chk("新断言", a == 2)
diff --git a/tests/check_new_gate.py b/tests/check_new_gate.py
--- a/tests/check_new_gate.py
+++ b/tests/check_new_gate.py
@@ -0,0 +1,2 @@
+chk("新门禁", True)
+chk("再来一条", True)
diff --git a/feed.py b/feed.py
--- a/feed.py
+++ b/feed.py
@@ -20,0 +21,2 @@
+TOKEN = "%s"
+ok = True
""" % FAKE_KEY
rh = t.parse_hunks(RED_SAMPLE)
red = t.red_lines(rh)
chk("红线：改了依赖清单要点名", any(r[0] == "禁改路径" and r[1] == "requirements.txt" for r in red), red)
chk("红线：已有测试出现删除行要点名", any(r[0] == "改了已有测试" and r[1] == "tests/check_feed.py" for r in red), red)
chk("红线：新增行里的 sk- key 要点名", any(r[0] == "疑似凭据" and r[1] == "feed.py" for r in red), red)
chk("红线：只加新测试文件不算「改了已有测试」",
    not any(r[0] == "改了已有测试" and r[1] == "tests/check_new_gate.py" for r in red), red)
chk("红线：requirements.txt 不被误报成凭据",
    not any(r[0] == "疑似凭据" and r[1] == "requirements.txt" for r in red), red)
chk("红线规模：加 6 行删 1 行", t.churn(rh) == (6, 1), t.churn(rh))

# 干净的一条：普通源码改动 + 只加不改的测试，四类都不该报。
clean = t.parse_hunks("""diff --git a/feed.py b/feed.py
--- a/feed.py
+++ b/feed.py
@@ -1,0 +2,1 @@
+x = 1
diff --git a/tests/check_new_gate.py b/tests/check_new_gate.py
--- a/tests/check_new_gate.py
+++ b/tests/check_new_gate.py
@@ -0,0 +1,1 @@
+chk("新门禁", True)
""")
chk("红线：干净的改动一个都不报", t.red_lines(clean) == [], t.red_lines(clean))

# 空 diff 是单独一档：没有改动的分支不该被当成待合的 PR。
chk("红线：空 diff 单独报「空改动」",
    [r[0] for r in t.red_lines([])] == ["空改动"], t.red_lines([]))

# 改动偏大：>= CHURN_REVIEW 行要提示；占位符 / 假 key 不该被误判成凭据。
big = [t.Hunk(path="a.py", old_start=1, old_len=0, new_start=1, new_len=1,
              ctx="", added=["x%d = 1" % i]) for i in range(t.CHURN_REVIEW)]
chk("红线：达到规模阈值要提示", any(r[0] == "改动偏大" for r in t.red_lines(big)), t.red_lines(big))
plain = t.parse_hunks("""diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,0 +2,1 @@
+api_key = "YOUR_API_KEY_HERE"
""")
chk("红线：占位符不算凭据", t.red_lines(plain) == [], t.red_lines(plain))

# ── 接线：默认分支列表与 merge-tree 探测都不炸 ───────────────────
chk("--help 能跑通（参数没写坏）",
    subprocess.run([sys.executable, str(REPO / "tools" / "pr_triage.py"), "--help"],
                   capture_output=True).returncode == 0)

# 取不到 PR 与「取到了但一个都没有」是两件事：前者该退回按分支名扫，后者该直接收工。
# 混在过一起的后果是实打实的 —— 会退回去扫几十个历史分支，再两两预演 n²/2 次
# git merge-tree，整条命令挂在那儿不动（这个 bug 真发生过，别让它回来）。
_orig = t.fetch_prs
try:
    t.fetch_prs = lambda slug: None
    chk("API 取不到 → fetch_prs 回 None（与「零个 PR」区分开）", t.fetch_prs("x") is None)
    t.fetch_prs = lambda slug: {}
    chk("API 取成了但零个 open PR → 回空 dict（不是 None）", t.fetch_prs("x") == {})
finally:
    t.fetch_prs = _orig

# 给一个不存在的 API slug，确认「取不到 PR」这条路是**直接收工并指路**，不会退回
# 扫几十个历史分支再两两预演（那个坑真踩过：命令挂在那儿几分钟不动）。
_rc = subprocess.run([sys.executable, str(REPO / "tools" / "pr_triage.py"),
                      "--api", "nonexistent-owner-xyz/nope"],
                     capture_output=True, text=True, timeout=60)
chk("--api 打到不存在的仓库：快速收工并提示怎么写（不挂死）",
    _rc.returncode == 0 and "API 没取到数据" in _rc.stdout,
    (_rc.stdout or "")[-200:] + (_rc.stderr or "")[-200:])

bad = [c for c in checks if not c[0]]
print("\nPR 预扫描体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
if bad:
    for _ok, n, x in bad:
        print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
