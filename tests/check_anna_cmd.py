# -*- coding: utf-8 -*-
"""安娜口令条的读口（anna_state.read_cmd）与搜索地址那把尺子（anna_state.search_url）。

为什么单开一条门禁
    `cache/anna_cmd.json` 是界面递给窗口的那块「该搜这个词」的牌子。它和账本一样会
    被人手改、也可能留着旧版本写下的脏值，而 `term` 拿到之后要做三件事：

      1. 拼搜索地址 —— `anna_browser.check_cmd` 调 `anna_state.search_url`，
         `urllib.parse.quote` 把关键词编成 utf-8，落单的半个代理码点（形如
         `{"term": "三体\\ud800"}`，json.load 收得下）在这一步抛 UnicodeEncodeError；
      2. 打进日志 —— 同一句 `log("按界面给的关键词去搜：%s" % term)` 把坏字符印到
         stdout，utf-8 的输出流照样抛同一个错；
      3. 写回状态账 —— `write_state` 那条有 `_drop_surrogates` 兜着，反而是最安全的一步。

    第 1 步在 `check_cmd` 里位于 `try` **之外**：异常直接穿出去，被 `anna_browser.run()`
    最外面那层 `except Exception` 接住，界面上的表现是窗口自己收摊（日志里一句「这一趟
    没走通：UnicodeEncodeError」）—— 用户只是搜了个词，整扇安娜的档案窗口就没了。
    第 2 步虽然在 `try` 之内、会被 catch 记成一句「这一页没打开」，但那同样是坏字符
    造出来的假故障，不该发生。

    写口令条时（`write_cmd`）本来就过 `clean_term`，所以坏字符是**从读口进来的**：
    手改、云同步落回、旧版本写下的档。收口因此放在读口（唯一入口洗一次）与那把尺子
    上（`search_url` 自己也不许把编不出来的字符串交给 `quote`）—— 与 `read_state`
    先过 `normalize`、`clean_domain` 自己洗代理码点是同一条规矩。

钉四面：
  A. 读口：手改的脏口令条读得开，`term` 洗过、正常字一个不改，坏档照旧回空；
  B. 对照：旧读法（直接 json.load）拿到的确实是带坏字符的 term，`quote` 对它确实抛
     —— 免得收口被拿掉还在空转；
  C. 尺子：`search_url` 对脏词不再抛，干净词 / 中文 / 空格 / 指定域名的地址一个字不改，
     脏词洗出来的地址与干净词的地址完全一致；
  D. 接线：窗口那一句 `check_cmd` 在脏口令条下不再被带走，`goto` 拿到的地址里没有
     坏码点，`term` 也已经能编出 utf-8（日志 / 落盘的前提）；静态钉住这两处收口还在。

跑法：纯函数 + 系统临时目录沙盒，不联网、不开浏览器、不起服务，归 --fast 静态段。
"""
import atexit
import json
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import anna_state as anna          # noqa: E402
import anna_browser as ab          # noqa: E402

SB = pathlib.Path(tempfile.mkdtemp(prefix="guizang-anna-cmd-"))
atexit.register(lambda: shutil.rmtree(SB, ignore_errors=True))

# 落单的半个代理码点用 chr() 现造：源码里不出现字面量，编辑器 / 编译器都不会被它绊到。
HI = chr(0xD800)
CMD = anna.cmd_path(str(SB))
FAIL = []
PASSED = [0]


def chk(name, ok, extra=""):
    if ok:
        PASSED[0] += 1
        print("  ok  " + name)
    else:
        FAIL.append(name)
        print(" FAIL " + name + (("  <- " + esc(extra)[:220]) if extra else ""))


def esc(s):
    """extra 里可能带着落单的半个代理码点：直接 print 会自己抛，把一条断言失败
    伪装成整个套件崩。转义成 \\ud800 再打。"""
    return str(s).encode("unicode_escape").decode("ascii")


def write_raw_cmd(payload):
    """照「手改 / 云同步落回 / 旧版本写下」的样子，直接落一份口令条。

    dict / list 先过 `json.dumps`（ensure_ascii=True）：盘上那份文本里写的是
    `"三体\\ud800"` 这串**转义**，json 读得回、utf-8 也编得出，与手改文件里真实的
    样子一致（裸的半码点反而写不进去）。字符串则原样落，用来造坏 JSON。
    """
    pathlib.Path(CMD).parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    pathlib.Path(CMD).write_text(text, encoding="utf-8")


def old_read():
    """对照：收口之前的读法 —— 原样 json.load 出来就交出去。"""
    with open(CMD, encoding="utf-8-sig") as f:
        d = json.load(f)
    return d if isinstance(d, dict) else {}


print("── A. 读口：手改的脏口令条读得开 ──────────────────────")
write_raw_cmd({"term": "三体" + HI, "ts": 1.0})
cmd = anna.read_cmd(str(SB))
chk("A1 脏口令条解得开、term 已洗过（半码点只丢它自己，正常字一个不改）",
    cmd.get("term") == "三体", esc(cmd))
chk("A2 洗过之后能编出 utf-8（日志与落盘那两步的前提）",
    cmd.get("term", "").encode("utf-8") == "三体".encode("utf-8"), esc(cmd))
chk("A3 ts 原样留着（它只是个「看没看过」的标记，不参与编码）",
    cmd.get("ts") == 1.0, esc(cmd))

write_raw_cmd({"term": "人类简史", "ts": 2})
chk("A4 干净词一个字不动", anna.read_cmd(str(SB)).get("term") == "人类简史",
    esc(anna.read_cmd(str(SB))))

write_raw_cmd({"term": "emoji😀成对", "ts": 3})
chk("A5 成对的代理码点（真 emoji）不在清洗范围内",
    anna.read_cmd(str(SB)).get("term") == "emoji😀成对", esc(anna.read_cmd(str(SB))))

write_raw_cmd(["不是对象"])
chk("A6 合法 JSON 但不是对象：照旧回空（老契约没被改）",
    anna.read_cmd(str(SB)) == {}, esc(anna.read_cmd(str(SB))))
write_raw_cmd('{坏 JSON')
chk("A7 半截 / 坏 JSON 照旧回空", anna.read_cmd(str(SB)) == {}, esc(anna.read_cmd(str(SB))))
chk("A8 文件不存在照旧回空", anna.read_cmd(str(SB / "没写过")) == {})

print("── B. 对照：旧读法 + 直接 quote 真会抛 ────────────────")
write_raw_cmd({"term": "三体" + HI, "ts": 1.0})
raw = old_read()
chk("B1 对照：旧读法原样吐出带坏字符的 term（这不是空转的收口）",
    HI in str(raw.get("term") or ""), esc(raw))
raised = None
try:
    from urllib.parse import quote
    quote(str(raw.get("term") or ""))
except UnicodeEncodeError:
    raised = "UnicodeEncodeError"
except Exception as e:                      # noqa: BLE001 —— 别的异常也算没接住
    raised = type(e).__name__
chk("B2 对照：把那个脏词直接交给 quote 确实抛 UnicodeEncodeError（窗口就是这样收摊的）",
    raised == "UnicodeEncodeError", raised)

print("── C. 尺子：search_url 不再把编不出来的字符串交给 quote ──")
caught = None
try:
    u = anna.search_url(str(raw.get("term") or ""))
except Exception as e:                      # noqa: BLE001
    caught = type(e).__name__
    u = ""
chk("C1 脏词走 search_url 不再抛", caught is None, caught)
chk("C2 脏词洗出来的地址与干净词的地址完全一致（坏码点不落进地址）",
    u == anna.search_url("三体"), (u, anna.search_url("三体")))

u = anna.search_url("人类简史")
chk("C3 中文关键词仍旧是编码过的、参数名仍旧是 q",
    u.startswith("https://annas-archive.is/search?q=") and "人类简史" not in u
    and "term=" not in u, u)
chk("C4 空格仍旧按 %20 而不是 +", "%20" in anna.search_url("a b"), anna.search_url("a b"))
chk("C5 指定域名就搜那个域名", "annas-archive.org" in anna.search_url("x", "annas-archive.org"))
chk("C6 空词 / None 仍旧拼得出一个干净的搜索页",
    anna.search_url("").endswith("/search?q=")
    and anna.search_url(None).endswith("/search?q="), anna.search_url(""))

print("── D. 接线：窗口那一句 check_cmd 不再被脏口令带走 ──────")
write_raw_cmd({"term": "三体" + HI, "ts": 7.0})


class FakePage:
    """只记 goto 的地址、不真开浏览器（与 check_anna.py 里那张假页同一套路）。"""

    def __init__(self):
        self.urls = []

    def goto(self, url, **kw):
        self.urls.append(url)


fp = FakePage()
stamp = None
try:
    stamp = ab.check_cmd(str(SB), (None, None), fp)
except Exception as e:                      # noqa: BLE001
    stamp = "RAISED:" + type(e).__name__
chk("D1 脏口令条下 check_cmd 不抛，还把牌子认下了（窗口继续活着）",
    isinstance(stamp, tuple) and stamp[0] == "三体", esc(stamp))
chk("D2 goto 拿到的地址里没有坏码点，且与干净词的地址一致",
    len(fp.urls) == 1 and HI not in fp.urls[0] and fp.urls[0] == anna.search_url("三体"),
    esc(fp.urls))
chk("D3 窗口收尾把 keyword 写回账本时也没被坏字符挡住",
    anna.read_state(str(SB)).get("keyword") == "三体", esc(anna.read_state(str(SB))))

# 静态接线：这两处收口还在，别哪天被一次重构悄悄绕开。
browser_src = (REPO / "anna_browser.py").read_text(encoding="utf-8")
state_src = (REPO / "anna_state.py").read_text(encoding="utf-8")
chk("D4 窗口那条路仍旧走 anna.search_url（没有另抄一份拼地址的写法）",
    "anna.search_url(term" in browser_src, browser_src[:0])
chk("D5 读口仍旧只此一处：read_cmd 里 term 过 clean_term",
    "def read_cmd" in state_src and "term=clean_term(" in state_src, state_src[:0])

print("\n安娜口令条读口体检：%d 项通过，失败 %d 项" % (PASSED[0], len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
