#!/usr/bin/env python3
"""「书城短 id」那一关的离线自测：一把尺子，两层都不许各写一份。

要守的是一个具体的坑：书城搜到一本书，界面上「加入书架」看着能点，点下去却被
引擎回一句「没有可用的书籍编号」。根子不在网络、也不在登录态，而在两边各写了一份
纯数字位数不同的正则 —— 界面那层放到 20 位，子进程 shelf_add.py 只到 12 位；
13–20 位的 id 在界面上算「有可加的短 id」，递到子进程却被安静丢掉。

所以这一条钉三件事，全部离线、不联网、不开浏览器：

  1. 规则本身：5–20 位纯数字留下，少一位 / 多一位 / 带字母的阅读器长 id 丢掉；
     空白、去重、保序这些边角也按说好的来。
  2. 那个具体的坑：13–20 位的 id（旧代码就丢在这一段）必须留得住，21 位以上和
     阅读器长 id 必须挡得住。
  3. 只剩一把尺子：ui_server.py 与 shelf_add.py 都接的是 book_ids 这一份，而不是
     各自再写一个数；ui.html 与 mcp 适配器是 JS / HTML 接不过来，就按同一组数字
     逐个比对 —— 谁改了其中一处没改另一处，这里就红。

跑法：.venv/bin/python tests/check_shelf_add.py
"""
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 先把数据目录指进临时沙盒再 import ui_server：免得它真去动用户真实的 cache/。
SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-shelfadd-"))
os.environ["GUIZANG_DATA"] = str(SB / "cache")
os.environ["GUIZANG_BOOKS"] = str(SB / "books")

import book_ids as bi   # noqa: E402
import ui_server as us  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


# ── 1. 规则本身 ────────────────────────────────────────────────────
GOOD = ["12345", "123450001", "123456789012", "12345678901234567", "1" * 20]
BAD = ["", "   ", "1234", "1" * 21, "abcde", "1234a", "a12345",
       "27212425abc", "https://weread.qq.com/web/bookDetail/abc123"]

chk("边界就是 5 与 20（门禁拿这两个数去比对界面那几处）",
    (bi.STORE_BOOK_ID_MIN, bi.STORE_BOOK_ID_MAX) == (5, 20),
    (bi.STORE_BOOK_ID_MIN, bi.STORE_BOOK_ID_MAX))
chk("5–20 位纯数字都算书城短 id",
    all(bi.is_store_book_id(x) for x in GOOD), [x for x in GOOD if not bi.is_store_book_id(x)])
chk("4 位 / 21 位 / 带字母 / 链接一律不算",
    not any(bi.is_store_book_id(x) for x in BAD), [x for x in BAD if bi.is_store_book_id(x)])
chk("首尾空白不算内容（\' 12345 \' 仍认）",
    bi.is_store_book_id("  12345  ") and bi.is_store_book_id("\t123450001\n"))
chk("None / 数字 / 非字符串都不炸",
    bi.is_store_book_id(None) is False
    and bi.is_store_book_id(12345) is True
    and bi.is_store_book_id(["12345"]) is False)
chk("clean 会去空白、保序、去重",
    bi.clean_store_book_ids([" 22222 ", "11111", "22222", "", None, "abc"]) == ["22222", "11111"])
chk("clean(None) / clean([]) 给空表而不是 None",
    bi.clean_store_book_ids(None) == [] and bi.clean_store_book_ids([]) == [])


# ── 2. 具体那个坑：13–20 位不许再被丢掉 ────────────────────────────
# 旧 shelf_add.py 用的是 r"\d{5,12}"，1e12 以上的短 id 全落在它外面。
SEAM = ["1234567890123",       # 13 位：旧代码从这里开始丢
        "12345678901234567890"]  # 20 位：界面那层允许的最大值
chk("13 位与 20 位的 id 留得住（旧代码在 13 位就丢了）",
    bi.clean_store_book_ids(SEAM) == SEAM, bi.clean_store_book_ids(SEAM))
chk("21 位不许多（界面上限就是 20，别在这边偷偷放宽）",
    bi.clean_store_book_ids(["1" * 21]) == [])
chk("全是坏 id 时给空表（调用方据此回「没有可用的书籍编号」）",
    bi.clean_store_book_ids(["abc", "123", "https://x/1"]) == [])


# ── 2b. 成功数：1 本不许报成 3 本（#11） ────────────────────────────
# 接口回的 succ 有时是布尔 true，有时是「成功几条」的数字。原来的
# `succ in (True, len(ids))` 在 Python 里因为 `1 == True`，批量加 3 本只成了 1 本时
# 会被判成整批都成，界面报「已加入书架（3/3）」—— 用户以为三本都进去了。
import shelf_add as sa  # noqa: E402
chk("succ=True 表示整批都成", sa.added_count(True, 3) == 3)
chk("succ=3（正好等于总数）算全成", sa.added_count(3, 3) == 3)
chk("succ=1 而共 3 本，只能报 1（原来会报 3）", sa.added_count(1, 3) == 1)
chk("succ=2 而共 3 本，报 2", sa.added_count(2, 3) == 2)
chk("succ=0 / False 表示一本没进", sa.added_count(0, 3) == 0 and sa.added_count(False, 3) == 0)
chk("succ 回字符串数字也认", sa.added_count("2", 3) == 2)
chk("succ 超过总数时按总数封顶，不虚报", sa.added_count(9, 3) == 3)
chk("succ 是看不懂的东西时回 None（由调用方报错，不硬编一个数）",
    sa.added_count({"a": 1}, 3) is None and sa.added_count("三本", 3) is None)
chk("单本时 1 == True 的老坑不复现", sa.added_count(1, 1) == 1)


# ── 3. 两层接的是同一把尺子 ────────────────────────────────────────
src_ui = (ROOT / "ui_server.py").read_text(encoding="utf-8")
src_sa = (ROOT / "shelf_add.py").read_text(encoding="utf-8")
chk("ui_server 那一层接的是 book_ids（不是自己再写一个数）",
    re.search(r"^import book_ids$", src_ui, re.M) is not None
    and "book_ids.clean_store_book_ids(raw_ids)" in src_ui)
chk("shelf_add 那一层接的也是 book_ids",
    re.search(r"^import book_ids$", src_sa, re.M) is not None
    and "book_ids.clean_store_book_ids(" in src_sa)
chk("子进程那层不再留着 5–12 的旧尺子",
    r"\d{5,12}" not in src_sa)
chk("ui_server 不再自己编译书城 id 正则（SHELF_ADD_ID 已撤）",
    "SHELF_ADD_ID" not in src_ui)

# ui.html 与 MCP 适配器是 JS / HTML，import 不了 Python 那一份；那就把「写的是不是
# 同一个数」逐处比对。只认锚成整串的那一种（/^\d{N,M}$/）——界面里另有 \d{1,2} 那种
# 时间正则、\d{4,} 那种「看着像阅读器 id」的判据，都不是书城短 id 这条规则，别误伤。
BOUND = re.compile(r"\^\\d\{(\d+),(\d+)\}\$")
js_files = [ROOT / "ui.html", ROOT / "mcp" / "guizang-mcp.mjs"]
found = []
for f in js_files:
    text = f.read_text(encoding="utf-8", errors="replace")
    found += [(str(f.relative_to(ROOT)), int(a), int(b)) for a, b in BOUND.findall(text)]
chk("界面与 MCP 里确实找得到这条规则（别是文件名写错、扫了个空）",
    len(found) >= 6, found[:3])
chk("界面与 MCP 里每一处都写着同一个 5–20",
    found and all((a, b) == (bi.STORE_BOOK_ID_MIN, bi.STORE_BOOK_ID_MAX)
                  for _, a, b in found),
    [x for x in found if (x[1], x[2]) != (5, 20)])
chk("Python 这一侧只剩 book_ids.py 一处定义（别的 .py 不再出现这条数字规则）",
    all(r"\d{5,20}" not in (ROOT / p).read_text(encoding="utf-8", errors="replace")
        for p in ["shelf_add.py", "ui_server.py"]))


# ── 4. 走一遍 ui_server 那一层：好 id 真递到子进程，坏 id 一个请求都不发 ──
class _Proc:
    returncode = 0
    stderr = ""
    stdout = json.dumps({"ok": True, "msg": "已加入书架（1/1）",
                         "added": [], "at": 0}, ensure_ascii=False)


captured = []


def _fake_run(argv, **kw):
    captured.append(list(argv))
    return _Proc()


_real_run = us.subprocess.run
us.subprocess.run = _fake_run
_waiting = us.TASK.get("running")
us.TASK["running"] = False
try:
    r = us.shelf_add([SEAM[1], "  12345  ", "12345", "not-an-id"])
    chk("好 id（含 20 位那个）都递到了子进程，坏 id 没混进去、重复的去掉",
        captured and captured[-1][2:] == [SEAM[1], "12345"],
        captured[-1] if captured else r)
    chk("回包按子进程那行 JSON 原样往上传（ok 保真）",
        r.get("ok") is True, r)

    captured.clear()
    r2 = us.shelf_add(["not-an-id", "123", "abc"])
    chk("一个短 id 都没有时不发请求，回人话",
        captured == [] and r2.get("ok") is False, (captured, r2))
finally:
    us.subprocess.run = _real_run
    if _waiting is not None:
        us.TASK["running"] = _waiting


shutil.rmtree(SB, ignore_errors=True)
print()
print("书城短 id 一把尺子：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  规则边界、13–20 位的那个坑、两层接线与界面同一组数字全部验通过")
