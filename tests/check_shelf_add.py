# -*- coding: utf-8 -*-
"""加书架（shelf_add.py）的书号口径与成功数 —— 纯逻辑离线自测，此前一条测试都没有。

「加入书架」是归藏少数几个会真的去改用户微信那边状态的动作（官方 Agent Gateway 只读，
这条走网页端写接口）。它最怕的两件事都不是「网络不通」，而是：

  A 书号口径四处不一致。界面 /ui.html、本地服务 ui_server.py、MCP 适配器三处写的都是
    `\\d{5,20}`，shelf_add.py 却写 `\\d{5,12}`，且 Python 的 `\\d` 连全角「１２３４５」
    和阿拉伯-印度数字「٥٥٥٥٥」都算数字（JS 的 \\d 只认 0-9）。于是同一本书在界面上点得动、
    到这里被判成「没有可用的书籍编号」，或者一段根本不该发出去的全角数字被当成书号
    发给微信。这一条把「四把尺子是同一把、都只认 ASCII 数字、长度都是 5–20」钉死。

  B 成功数把 1 本当 3 本。接口回的 succ 有时是布尔 true，有时是「成功几条」的数字。
    原来的 `succ in (True, len(ids))` 在 Python 里因为 `1 == True`，批量加 3 本只成了
    1 本时会判成整批都成，界面就报「已加入书架（3/3）」—— 用户以为三本都进去了。
    added_count 把这个翻译拆成纯函数逐种面孔验过。

playwright 是起真浏览器的重依赖（离线门禁的机器上不一定装了），这里只在缺它的时候塞
一个占位模块，让 shelf_add.py 能被导入去验那两个纯函数；不发任何请求、不开窗口。
"""
import pathlib
import sys
import types

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

FAIL = []
PASSED = [0]


def chk(name, cond, extra=""):
    if cond:
        PASSED[0] += 1
    else:
        FAIL.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:240]))


# ── 只在真的缺 playwright 时放一个占位，别的依赖照常真导入 ──────────
try:
    import playwright  # noqa: F401
except Exception:
    _pw = types.ModuleType("playwright")
    _api = types.ModuleType("playwright.async_api")
    _api.async_playwright = None
    _pw.async_api = _api
    sys.modules.setdefault("playwright", _pw)
    sys.modules.setdefault("playwright.async_api", _api)

import shelf_add as sa  # noqa: E402

# ── A 书号口径：只留 5–20 位 ASCII 数字 ──────────────────────────
chk("真书城短 id 收得下", sa.clean_ids(["3300060341"]) == ["3300060341"])
chk("5 位、20 位边界都收", sa.clean_ids(["12345", "1" * 20]) == ["12345", "1" * 20])
chk("两边空白先修掉再判", sa.clean_ids(["  3300060341  "]) == ["3300060341"])
chk("重复书号只留一条、顺序不乱", sa.clean_ids(["22222", "11111", "22222"]) == ["22222", "11111"])
chk("4 位 / 21 位一律拒", sa.clean_ids(["1234", "1" * 21]) == [])
chk("空值 / None 不留痕", sa.clean_ids([None, "", "   "]) == [])
chk("夹字母的假 id 拒收", sa.clean_ids(["123a5", "b3300060341"]) == [])
chk("阅读器长 id / 链接 / deepLink 拒收",
    sa.clean_ids(["wr_abcdefg", "https://weread.qq.com/book/123", "weread://book/12345"]) == [])
# 这两条是 Python 与 JS 的真正分歧点：JS 的 \d 不认它们，Python 的认。
chk("全角数字「１２３４５」不当书号", sa.clean_ids(["１２３４５"]) == [])
chk("阿拉伯-印度数字「٥٥٥٥٥」不当书号", sa.clean_ids(["٥٥٥٥٥"]) == [])
sa_src = (REPO / "shelf_add.py").read_text(encoding="utf-8")
chk("Python 侧 SHELF_ID 是 ASCII 数字 5–20，不是 Unicode \\d",
    'SHELF_ID = re.compile(r"^[0-9]{5,20}$")' in sa_src and
    're.compile(r"^\\d{5' not in sa_src)

# ── B 成功数：1 本不许报成 3 本 ─────────────────────────────────
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

# ── C 接线：四处同一把尺子 + 门禁挂上 ────────────────────────────
server = (REPO / "ui_server.py").read_text(encoding="utf-8")
ui = (REPO / "ui.html").read_text(encoding="utf-8")
mcp = (REPO / "mcp" / "guizang-mcp.mjs").read_text(encoding="utf-8")
run_all = (HERE / "run_all.sh").read_text(encoding="utf-8")

chk("ui_server 的书号尺子是 ASCII 数字 5–20",
    "SHELF_ADD_ID = re.compile(r\"^[0-9]{5,20}$\")" in server)
chk("ui_server 不再用 Unicode \\d 收书号",
    "SHELF_ADD_ID = re.compile(r\"^\\d{5,20}$\")" not in server)
chk("界面的尺子与后端同长（JS \\d 即 ASCII）", "/^\\d{5,20}$/" in ui)
chk("MCP 适配器的尺子与后端同长", "\\d{5,20}" in mcp)
chk("本地服务仍起 shelf_add.py 这个脚本",
    '"shelf_add.py"' in server or "shelf_add.py" in server)
chk("--fast 门禁挂上了这一条", "tests/check_shelf_add.py" in run_all)

bad = [c for c in FAIL]
print("\n加书架口径体检：%d 项通过，失败 %d 项" % (PASSED[0], len(bad)))
for n in bad:
    print("  ✗ " + n)
sys.exit(1 if bad else 0)
