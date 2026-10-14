#!/usr/bin/env python3
"""登录态账本（cache/login_state.json）的离线自测。

要守的是一个用户看得见的坑：设置页、壳、MCP 适配器三处都按「logged_in 是布尔、
checked_at 是秒数」直接算时间（`new Date(checked_at * 1000)`），可磁盘上这份文件
可能被手改过、可能是半截、也可能是老版本留下的别种形状 —— 于是它们印出来的就是
「未连接 NaN月NaN日 NaN:NaN」「（1970-01-01 08:00 检查）」，看着像后端坏了。
读的一方原来把 json.load 的结果原样递出去，脏形状从门口一路传到底。

另半段是落盘：这一份原来是直接 `open(path, "w")` 覆盖写的 —— 那一瞬间旧账就被
截成 0 字节，进程要是在写新内容之前被收走（点停止、退出应用），磁盘上只剩一个
空文件，界面从此显示「未检测」直到下次登录。别处（board / anna_state / book_import）
早就改成「先写 .tmp 再 os.replace」，这里补上同一条。

所以这一条钉五件事，全离线、不联网、不开浏览器、不需要 playwright：

  1. 形状只有一处真相：normalize_state 认什么、不认什么（字符串 "false" 不许当
     已登录，坏时间戳一律给 None，绝不猜）。
  2. 落盘是原子的：dump 抛 / replace 抛时旧档一个字不变、不留 .tmp。
  3. 读账本收口：半截 JSON / 空文件 / 坏形状回 None，脏值不往下游传。
  4. 接线：ui_server 读的是同一把尺子；界面与 MCP 都兜住了「没有秒数」这一档。
  5. is_logged_in 的边角：只有 wr_vid 有内容才算已登录，取 cookie 抛异常不穿出去。

跑法：.venv/bin/python tests/check_login.py
"""
import asyncio
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 先把数据目录指进临时沙盒再 import ui_server：免得它真去动用户真实的 cache/。
SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-login-"))
os.environ["GUIZANG_DATA"] = str(SB / "data")
os.environ["GUIZANG_BOOKS"] = str(SB / "books")

import login as lg      # noqa: E402
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


# ── 1. 形状只有一处真相 ────────────────────────────────────────────
GOOD = {"logged_in": True, "checked_at": 1700000000, "url": "https://weread.qq.com/web/shelf",
        "note": "会话有效"}
chk("好形状原样通过（时间是秒数）",
    lg.normalize_state(GOOD) == {"logged_in": True, "checked_at": 1700000000.0,
                                 "url": GOOD["url"], "note": "会话有效"},
    lg.normalize_state(GOOD))
chk("空对象给「未登录 + 没有时间」，不是 None",
    lg.normalize_state({}) == {"logged_in": False, "checked_at": None, "url": "", "note": ""})
chk("多出来的键丢掉（只留读者认的那四个）",
    lg.normalize_state({"logged_in": True, "checked_at": 1, "junk": "x"})
    == {"logged_in": True, "checked_at": 1.0, "url": "", "note": ""})

# 谎报「已连接」比说「未检测」更坏：只有真正的布尔 true 才算登录。
chk("字符串 'false' 不算已登录（原来它是真值，界面会报已连接）",
    lg.normalize_state({"logged_in": "false"})["logged_in"] is False)
chk("数字 1 不算已登录（bool 是 int 的子类，反过来说 1 也不许当真）",
    lg.normalize_state({"logged_in": 1})["logged_in"] is False)
chk("缺 logged_in 算未登录", lg.normalize_state({"checked_at": 1})["logged_in"] is False)

chk("坏时间戳给 None（'昨天' * 1000 是 NaN）",
    lg.normalize_state({"logged_in": True, "checked_at": "昨天"})["checked_at"] is None)
chk("数字的字符串也不算秒数（不猜）",
    lg.normalize_state({"checked_at": "1700000000"})["checked_at"] is None)
chk("布尔 true 不算秒数（True 会变成 1970 那条假时间）",
    lg.normalize_state({"checked_at": True})["checked_at"] is None)
chk("NaN / inf 不算秒数",
    lg.normalize_state({"checked_at": float("nan")})["checked_at"] is None
    and lg.normalize_state({"checked_at": float("inf")})["checked_at"] is None)
chk("0 与负数不算秒数（界面上会是 1970 / 更早）",
    lg.normalize_state({"checked_at": 0})["checked_at"] is None
    and lg.normalize_state({"checked_at": -1})["checked_at"] is None)
chk("小数秒数照收（time.time() 本来就是小数）",
    lg.normalize_state({"checked_at": 1700000000.25})["checked_at"] == 1700000000.25)
chk("url / note 不是字符串时给空串，不往下游传别的类型",
    lg.normalize_state({"url": 123, "note": None})["url"] == ""
    and lg.normalize_state({"url": 123, "note": None})["note"] == "")

# 顶层都不是字典：一个坏文件不该让 /api/state 变成 500 或半句胡话。
chk("数组 / 字符串 / 数字 / None 一律回 None",
    all(lg.normalize_state(x) is None for x in ([1, 2], "x", 42, None, True)))


# ── 2. 落盘是原子的 ────────────────────────────────────────────────
STATE = SB / "cache" / "login_state.json"
written = lg.write_state(True, "https://weread.qq.com/web/shelf", "会话有效", path=str(STATE))
chk("写进去的就是读得回来的那种形状",
    lg.read_state(STATE) == written and written["logged_in"] is True
    and isinstance(written["checked_at"], float), written)
chk("目录不存在时自己建出来", STATE.is_file())
chk("写完不留 .tmp", not (SB / "cache" / "login_state.json.tmp").exists())

_real_dump = lg.json.dump


def _dump_boom(*a, **kw):
    raise RuntimeError("disk full（模拟写到一半失败）")


lg.json.dump = _dump_boom
try:
    raised = False
    try:
        lg.write_state(False, "u", "n", path=str(STATE))
    except Exception:
        raised = True
finally:
    lg.json.dump = _real_dump
chk("dump 抛时往外抛（调用方知道没写成，不是安静吞掉）", raised)
chk("dump 抛时旧档一个字没动", lg.read_state(STATE)["logged_in"] is True)
chk("dump 抛时 .tmp 收掉了（不留半截垃圾）",
    not os.path.exists(str(STATE) + ".tmp"))

_real_replace = lg.os.replace


def _replace_boom(*a, **kw):
    raise OSError("rename failed（模拟替换那一步失败）")


lg.os.replace = _replace_boom
try:
    raised = False
    try:
        lg.write_state(False, "u", "n", path=str(STATE))
    except Exception:
        raised = True
finally:
    lg.os.replace = _real_replace
chk("replace 抛时也往外抛", raised)
chk("replace 抛时旧档仍是完整的（最危险的那扇窗）", lg.read_state(STATE)["logged_in"] is True)
chk("replace 抛时 .tmp 收掉了", not os.path.exists(str(STATE) + ".tmp"))

src_login = (ROOT / "login.py").read_text(encoding="utf-8")
chk("login.py 不再直接覆盖写账本（没有 open(..., \"w\") 落在 STATE_PATH 上）",
    'open(STATE_PATH, "w")' not in src_login)
chk("走的是 .tmp + os.replace 那一套",
    'tmp = path + ".tmp"' in src_login and "os.replace(tmp, path)" in src_login)


# ── 3. 读账本收口：坏档回 None，脏值不往下游传 ──────────────────────
STATE.write_text('{"logged_in": tr', encoding="utf-8")
chk("半截 JSON 回 None（不是抛异常把轮询打崩）", lg.read_state(STATE) is None)
STATE.write_text("", encoding="utf-8")
chk("空文件回 None（进程被收走留下的那个空档）", lg.read_state(STATE) is None)
STATE.write_text(json.dumps({"logged_in": "false"}), encoding="utf-8")
chk("能解析但形状脏：洗成未登录，而不是把 'false' 往上递",
    lg.read_state(STATE) == {"logged_in": False, "checked_at": None, "url": "", "note": ""})
chk("文件根本不在时回 None", lg.read_state(SB / "cache" / "nope.json") is None)


# ── 4. 接线：ui_server / 界面 / MCP 都兜住 ─────────────────────────
STATE.write_text(json.dumps(GOOD), encoding="utf-8")
_old_state_path = us.LOGIN_STATE
us.LOGIN_STATE = str(STATE)
try:
    chk("/api/state 里 login 那一格走的是同一把尺子",
        us.read_login_state() == lg.read_state(STATE), us.read_login_state())
    STATE.write_text(json.dumps({"logged_in": "false", "checked_at": "昨天"}), encoding="utf-8")
    got = us.read_login_state()
    chk("脏 logged_in 过 ui_server 后是「未连接」",
        got is not None and got["logged_in"] is False, got)
    chk("脏时间过 ui_server 后是 None（界面据此不印那半句）",
        got is not None and got["checked_at"] is None, got)
    STATE.write_text("{半截", encoding="utf-8")
    chk("半截档过 ui_server 回 None（界面显示「未检测」）", us.read_login_state() is None)
finally:
    us.LOGIN_STATE = _old_state_path

src_ui = (ROOT / "ui_server.py").read_text(encoding="utf-8")
m = re.search(r"def read_login_state\(\):\n(.*?)\n\n\n", src_ui, re.S)
body = m.group(1) if m else ""
chk("ui_server 不再自己 json.load 登录态（交给 login 一处）",
    "login.read_state(LOGIN_STATE)" in body and "json.load(" not in body, body[:160])

src_html = (ROOT / "ui.html").read_text(encoding="utf-8", errors="replace")
chk("界面不再把 checked_at 直接喂给 when()（那是 NaN月NaN日 的来源）",
    "when(s.login.checked_at)" not in src_html)
chk("界面印登录时间前先认「是不是一个正的有限数」",
    re.search(r"Math\.isFinite\(ts\)|Number\.isFinite\(ts\)", src_html) is not None)
chk("界面拿不到有效秒数时只写状态、不 append 那半句",
    re.search(r"if \(Number\.isFinite\(ts\) && ts > 0\) \{\s*\n\s*const em", src_html) is not None)

src_mcp = (ROOT / "mcp" / "guizang-mcp.mjs").read_text(encoding="utf-8", errors="replace")
chk("MCP 的 fmtWhen 对认不出的秒数给「—」（原来印 NaN-NaN-NaN NaN:NaN）",
    re.search(r"function fmtWhen\(ts\) \{[\s\S]{0,600}?return \"—\";", src_mcp) is not None)

# import login 不该把 playwright 拉进来：ui_server 在首次运行、虚拟环境还没建好的
# 时候就要起得来，而它现在 import 了 login 去读账本。子进程里量最干净。
r = subprocess.run([sys.executable, "-c",
                    "import login, sys; print('playwright' in sys.modules)"],
                   cwd=str(ROOT), capture_output=True, text=True)
chk("import login 不会把 playwright 拖进来（首跑没装依赖也进得来）",
    r.stdout.strip() == "False", (r.stdout + r.stderr)[:200])
chk("playwright 只在 main() 里 import",
    "from playwright.async_api import async_playwright" in src_login
    and re.search(r"^from playwright", src_login, re.M) is None)


# ── 5. is_logged_in 的边角 ─────────────────────────────────────────
class _Ctx:
    def __init__(self, cookies=None, boom=False):
        self._cookies = cookies or []
        self._boom = boom

    async def cookies(self, url):
        if self._boom:
            raise RuntimeError("browser gone")
        return self._cookies


def _login(cookies=None, boom=False):
    return asyncio.run(lg.is_logged_in(_Ctx(cookies, boom)))


chk("wr_vid 有值算已登录",
    _login([{"name": "wr_vid", "value": "V1"}]) is True)
chk("没有 wr_vid 算未登录（别的 cookie 不算数）",
    _login([{"name": "wr_gid", "value": "x"}]) is False)
chk("wr_vid 只有空白算未登录",
    _login([{"name": "wr_vid", "value": "   "}]) is False)
chk("wr_vid 的 value 是 None 也不炸",
    _login([{"name": "wr_vid", "value": None}]) is False)
chk("取 cookie 抛异常时算未登录（异常不穿出去）", _login(boom=True) is False)
chk("cookie 表里混着没 name 的项也不炸",
    _login([{}, {"name": "wr_vid", "value": "V1"}]) is True)


shutil.rmtree(SB, ignore_errors=True)
print()
print("登录态账本体检：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  形状一处真相、原子落盘、坏档收口、界面与 MCP 兜底全部验通过")
