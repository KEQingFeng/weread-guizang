# -*- coding: utf-8 -*-
"""「安娜的档案」这一路的离线自测：账本、防穿越、去重、入库、域名、代理。

为什么全都能离线跑：这一路唯一必须联网的部分是「在窗口里搜书、点下载」，而那一段
是站点自己的页面，归藏接不住也不该代抓。归藏这边做的事全是本地文件活 —— 把接住的
字节转成一本书、把状态记进一份小 json、把口令递给窗口。所以这一套不起服务、不开
浏览器、不发一个网络请求：数据目录用系统临时目录里的一份沙盒，下载用现编的假
download 对象，站点回来的内容用真导入器吃得下的最小 Markdown / EPUB 头。

这一套替产品挡掉的四类事故：
  · 远端给的文件名带 `../` 或者就是 `..` —— 写盘写到书架外面去；
  · 同一本下两次 —— 书库里多一条重复条目（这站点的近重复条目非常多）；
  · mobi / djvu / zip —— 假装成功、用户回来看见一个打不开的「书」；
  · 账本写进绝对路径 —— 那份 json 会经 /api/state 进界面，路径里带着用户名。

另外三条源码级护栏是给「以后的人会改回去」准备的：界面不许再出现密码框（上一轮
Z-Library 已经验证过：机器填不进对方的登录表单，只有用户自己登）；窗口必须是
`headless=False`（无头会被对方在 TLS 层挡掉，有头是正确性要求不是口味）；
Z-Library 那一整条链路不许有残留。
"""
import atexit
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import anna_state as anna  # noqa: E402
import anna_browser as ab  # noqa: E402
import book_import  # noqa: E402
import book_layout  # noqa: E402

FAIL = []
PASSED = [0]

# 一份沙盒数据目录：跑完就整棵删掉，不碰用户的 cache/。
DD = tempfile.mkdtemp(prefix="guizang-anna-data-")
SHELF = os.path.join(DD, "books", "本地书架")
COVERS = os.path.join(DD, "cache", "covers")
atexit.register(shutil.rmtree, DD, True)

# 这条路是真的会往盘上写东西的，所以先自证一下跑在沙盒里：仓库里那份 cache
# 是开发者自己跑过归藏留下的，测试一写就会污染真状态。缺省的工作目录恰恰就是
# 仓库根，所以这一道闸门不是形式主义 —— 少了它，「直接在仓库里 python
# tests/check_anna.py」就会把示范书目写进用户的本地书架。
if os.path.realpath(DD).startswith(os.path.realpath(str(REPO)) + os.sep):
    sys.exit("沙盒目录竟然落在仓库里，拒绝跑（否则会写进开发机的 cache/）。")

# 一份现编的 Markdown：两个 `#` 标题，导入器能切出两章。
# 书名与正文全是编的，不带任何真实书目信息。
MD_TEXT = "# 第一章 示范\n\n这一段正文是现编的，用来验入库链路能不能走通。\n\n" \
          "# 第二章 还有内容\n\n第二段也是编的，字数够它切出一章。\n"
MD_BYTES = MD_TEXT.encode("utf-8")


def B(s):
    """中文字面量不能写成 b"..."，统一在这儿编码。"""
    return s.encode("utf-8")


def chk(name, cond, extra=""):
    if cond:
        PASSED[0] += 1
    else:
        FAIL.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:240]))


def fresh_state():
    """把账本擦回「没用过」，让每一段断言从同一起点开始。"""
    anna._atomic_write(anna.state_path(DD), anna._blank())


def write_incoming(name, blob):
    """往原始下载格里放一份文件，回它的路径。"""
    d = anna.incoming_dir(DD, create=True)
    p = os.path.join(d, name)
    with open(p, "wb") as f:
        f.write(blob)
    return p


class env:
    """临时改环境变量，退出时还原。代理与输出目录那几条都得这么测。"""

    def __init__(self, **kv):
        self.kv = kv
        self.old = {}

    def __enter__(self):
        for k, v in self.kv.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *_a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class FakeDownload:
    """顶替 playwright 的 download 对象：只用到 suggested_filename 与 save_as。"""

    def __init__(self, name, blob=b"x", fail=False):
        self.suggested_filename = name
        self.blob = blob
        self.fail = fail
        self.saved_to = None

    def save_as(self, target):
        if self.fail:
            raise RuntimeError("目标站点把连接断了")
        self.saved_to = target
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(self.blob)


class FakePage:
    """顶替 page：只数着被 goto 了几次，以及要不要抛异常。"""

    def __init__(self, fail=False):
        self.urls = []
        self.fail = fail

    def goto(self, url, **_kw):
        self.urls.append(url)
        if self.fail:
            raise RuntimeError("导航超时")


class FakeCtx:
    def __init__(self, pages):
        self.pages = pages


# ── 1 账本：归一化与读写 ────────────────────────────────────────
b = anna._blank()
chk("空白账本的字段齐全（界面不用防缺字段）",
    set(b) == {"domain", "window", "keyword", "caught", "imported", "skipped",
               "seen", "books", "pending", "note", "last_error", "updated_at"}, sorted(b))
chk("缺省镜像域名就是实测能打开的那个", b["domain"] == anna.DEFAULT_DOMAIN == "annas-archive.is", b["domain"])
chk("normalize(None) 回空白，不抛异常", anna.normalize(None) == anna._blank())
chk("normalize(字符串) 也回空白", anna.normalize("乱七八糟") == anna._blank())
chk("窗口状态只认那四种，脏值退回 closed",
    anna.normalize({"window": "起飞了"})["window"] == "closed")
for win in ("closed", "opening", "open", "failed"):
    chk("窗口状态认得 %s" % win, anna.normalize({"window": win})["window"] == win)
chk("计数脏值与负数都归 0",
    anna.normalize({"caught": -5, "imported": "abc", "skipped": None})["caught"] == 0
    and anna.normalize({"imported": -1})["imported"] == 0
    and anna.normalize({"skipped": "x"})["skipped"] == 0)
chk("计数正常值照收", anna.normalize({"caught": 7})["caught"] == 7)
chk("域名脏值洗成小写并去掉首尾空格",
    anna.normalize({"domain": " ANNAS-ARCHIVE.GD "})["domain"] == "annas-archive.gd")
chk("最近入库最多留 12 本", len(anna.normalize({"books": [{"n": i} for i in range(40)]})["books"]) == 12)
chk("没收进来的最多留 12 条", len(anna.normalize({"pending": [{"n": i} for i in range(40)]})["pending"]) == 12)
chk("列表里的非字典行被丢掉（半截写坏的 json 不炸界面）",
    anna.normalize({"books": [{"n": 1}, "字符串", None]})["books"] == [{"n": 1}])
chk("去重账最多留 300 条", len(anna.normalize({"seen": ["x%d" % i for i in range(500)]})["seen"]) == 300)
chk("关键词一句话上限 60 字", len(anna.normalize({"keyword": "长" * 90})["keyword"]) == 60)
chk("回执句子里的换行被压成一句", "\n" not in anna.normalize({"note": "第一行\n第二行"})["note"])
chk("回执句子有长度上限", len(anna.normalize({"note": "哦" * 400})["note"]) <= anna.NOTE_MAX)

fresh_state()
chk("没写过的数据目录读出来是空白账（首次点开也有东西显示）",
    anna.read_state(os.path.join(DD, "空的")) == anna._blank())
bad = os.path.join(DD, "坏json")
os.makedirs(os.path.dirname(anna.state_path(bad)), exist_ok=True)
with open(anna.state_path(bad), "w", encoding="utf-8") as f:
    f.write("{这不是 json")
chk("账本写成半截也不抛异常，退回空白", anna.read_state(bad) == anna._blank())
st = anna.write_state(DD, {"window": "open"})
chk("write_state 回洗完的那份", st["window"] == "open" and st["updated_at"] > 0)
st = anna.write_state(DD, {"caught": 3})
chk("改账本是先读后并：窗口状态没被下一次写抹掉",
    st["window"] == "open" and st["caught"] == 3, st)
chk("原子写不留 .tmp 垃圾", not os.path.exists(anna.state_path(DD) + ".tmp"))
fresh_state()

# ── 2 名字与格式：远端给的东西一律先洗 ──────────────────────────
chk("防穿越：`../../etc/passwd` 只剩文件名", anna.safe_name("../../etc/passwd") == "passwd")
chk("防穿越：反斜杠也算分隔符", anna.safe_name(r"..\..\windows\x.epub") == "x.epub")
chk("防穿越：纯点号的名字不许拿去拼路径（会指到上一级）",
    anna.safe_name("..") == "未命名" and anna.safe_name(".") == "未命名",
    [anna.safe_name(".."), anna.safe_name(".")])
chk("去掉控制字符", "\x00" not in anna.safe_name("一本\x00怪名字.epub"))
chk("空格与斜杠换成下划线", " " not in anna.safe_name("这 本 / 那 本.epub"))
chk("超长名截断且留住扩展名", anna.safe_name("长" * 300 + ".pdf").endswith(".pdf")
    and len(anna.safe_name("长" * 300 + ".pdf")) <= 130)
chk("空名字兜成「未命名」", anna.safe_name("") == "未命名")
chk("扩展名小写", anna.ext_of("书.EPUB") == "epub")
chk("没扩展名回空串", anna.ext_of("没有后缀") == "")
chk("收得下的就是导入器认得那四种",
    [anna.keepable("a." + x) for x in anna.KEEP_EXT] == [True] * 4 and anna.KEEP_EXT == ("epub", "pdf", "txt", "md"))
chk("mobi / djvu / zip 不收", not any(anna.keepable("a." + x) for x in ("mobi", "djvu", "zip")))
chk("分类：扩展名优先", anna.classify("一本.md") == "md" and anna.classify("一本.epub") == "epub")
chk("分类：没扩展名时看文件头认 PDF", anna.classify("下载.bin", b"%PDF-1.7 xxx") == "pdf")
chk("分类：没扩展名时看文件头认 EPUB（zip 容器）", anna.classify("下载.bin", b"PK\x03\x04extra") == "epub")
chk("分类：认不出的回空串，不是回 None", anna.classify("a.mobi", b"MOBIPACK") == "")
chk("摘要一份字节一个，稳定可比",
    anna.digest(b"abc") == anna.digest(b"abc") and anna.digest(b"abc") != anna.digest(b"abd"))
chk("空字节也有摘要（不因 None 而炸）", len(anna.digest(None)) == 32)

# ── 3 去重账与列表 ─────────────────────────────────────────────
first, s1 = anna.mark_seen(anna._blank(), "abc123")
chk("第一次见到这份 → 该入库", first is True and s1["seen"] == ["abc123"])
second, s2 = anna.mark_seen(s1, "abc123")
chk("第二次见到同一份 → 认出重复", second is False and s2["seen"] == ["abc123"])
blank_key, s3 = anna.mark_seen(s1, "")
chk("摘要拿不到时放行（宁可重下一遍，也不悄悄丢一本）",
    blank_key is True and s3["seen"] == ["abc123"])
many, s4 = anna.mark_seen({"seen": ["s%d" % i for i in range(anna.SEEN_KEEP)]}, "新一本")
chk("去重账满了会挤掉最旧的，长度不再涨",
    many is True and len(s4["seen"]) == anna.SEEN_KEEP and s4["seen"][0] == "s1", len(s4["seen"]))
p = anna.push_book(anna._blank(), {"n": 1})
p = anna.push_book(p, {"n": 2})
chk("最近入库是往前插（界面上第一本最新）", [r["n"] for r in p["books"]] == [2, 1])
big = anna._blank()
for i in range(20):
    big = anna.push_book(big, {"n": i})
chk("超过 12 本挤掉尾巴", len(big["books"]) == 12 and big["books"][0]["n"] == 19)
pk = anna.push_pending(anna._blank(), {"name": "旧格式样本.mobi", "reason": "格式不认"})
chk("没收进来的也往前插并且留得住原因",
    pk["pending"][0]["reason"] == "格式不认" and len(pk["pending"]) == 1)
chk("脏账本也能插（normalize 挡住 None）",
    len(anna.push_pending(None, {"a": 1})["pending"]) == 1)

# ── 4 域名与搜索地址 ──────────────────────────────────────────
chk("域名洗掉协议头", anna.clean_domain("https://annas-archive.is") == "annas-archive.is")
chk("域名洗掉路径与查询", anna.clean_domain("annas-archive.is/search?term=x") == "annas-archive.is")
chk("域名后面多打的冒号洗掉，但端口留着（镜像可能开在非标准端口）",
    anna.clean_domain("annas-archive.is:") == "annas-archive.is"
    and anna.clean_domain("https://annas-archive.gd:8443/search") == "annas-archive.gd:8443",
    anna.clean_domain("annas-archive.is:"))
chk("空域名补默认（界面输入框清空不许拼出 https:///）",
    anna.clean_domain("") == anna.DEFAULT_DOMAIN and anna.clean_domain(None) == anna.DEFAULT_DOMAIN)
chk("补上协议头", anna.with_scheme("annas-archive.is") == "https://annas-archive.is")
chk("界面里填了 http 也统一走 https，且不会拼出两个协议头",
    anna.with_scheme("http://annas-archive.is") == "https://annas-archive.is"
    and "https://https" not in anna.with_scheme("https://annas-archive.is"))
u = anna.search_url("人类简史")
chk("中文关键词是编码过的（直接拼会打成乱码）",
    u.startswith("https://annas-archive.is/search?q=") and "人类简史" not in u, u)
# 2026-10-07 有头窗口实测：?term= 也画出四十行「结果」，但换三个不同的词拿到的是同一列
# 俄语书 —— 参数名写错不报错、只给一屏假结果，比打不开更难发现，所以这一条单独钉住。
chk("搜索参数是 q 不是 term（term 会给一屏与关键词无关的书）",
    "term=" not in u, u)
chk("空格按 %20 而不是 +（对方搜索页认 %20）", "%20" in anna.search_url("a b"), anna.search_url("a b"))
chk("指定域名就搜那个域名", "annas-archive.org" in anna.search_url("x", "annas-archive.org"))
cands = anna.domain_candidates("annas-archive.gd")
chk("候选域名把用户设的那个排最前", cands[0] == "annas-archive.gd", cands)
chk("候选域名去重（不能出现两次同一个）", len(cands) == len(set(cands)) == 4, cands)
chk("候选补齐了另外几个镜像（对方换域不至于全打不开）",
    set(cands) == set(anna.DOMAINS), cands)
chk("额外提示的域名排在「用户设的」之后、内置镜像之前",
    anna.domain_candidates("自定义.测试", extra=["另一个.测试"])[:2] == ["自定义.测试", "另一个.测试"],
    anna.domain_candidates("自定义.测试", extra=["另一个.测试"]))
chk("没设域名时默认那个仍在最前（口令条与候选不会各排一套）",
    anna.domain_candidates(None, extra=["另一个.测试"])[0] == anna.DEFAULT_DOMAIN)

# ── 5 原始下载格 ─────────────────────────────────────────────
fresh_state()
d1 = write_incoming("第一本.md", MD_BYTES)
time.sleep(0.02)
d2 = write_incoming("第二本.mobi", b"MOBIPACK" * 10)
os.makedirs(os.path.join(anna.incoming_dir(DD), "子目录"), exist_ok=True)
rows = anna.incoming_listing(anna.incoming_dir(DD))
chk("下载格按时间新→旧排", [r["name"] for r in rows] == ["第二本.mobi", "第一本.md"],
    [r["name"] for r in rows])
chk("只列文件不列目录", all("子目录" != r["name"] for r in rows))
chk("每行只给名字与字节，不给路径",
    set(rows[0]) == {"name", "bytes", "mtime", "ok"} and "/" not in json.dumps(rows, ensure_ascii=False),
    rows[0])
chk("每行标了收不收得下", [r["ok"] for r in rows] == [False, True], rows)
chk("limit 生效", len(anna.incoming_listing(anna.incoming_dir(DD), limit=1)) == 1)
chk("那一格不存在时回空列表（首次进来不炸）",
    anna.incoming_listing(os.path.join(DD, "没有这格")) == [])
chk("目录里没有文件也回空列表", anna.incoming_listing(DD) is not None)

# ── 6 代理 ────────────────────────────────────────────────────
with env(HTTPS_PROXY="", https_proxy="", ALL_PROXY="", HTTP_PROXY="", http_proxy=""):
    got = anna.proxy_url()
    chk("环境里没写代理时回一个字符串（可能直连，不硬塞死地址）", isinstance(got, str), got)
with env(HTTPS_PROXY="http://127.0.0.1:7890", https_proxy=None, ALL_PROXY=None,
         HTTP_PROXY=None, http_proxy=None):
    chk("环境里显式给的代理优先于系统设置",
        anna.proxy_url() == "http://127.0.0.1:7890", anna.proxy_url())
with env(HTTPS_PROXY="  http://127.0.0.1:7890  ", https_proxy=None, ALL_PROXY=None,
         HTTP_PROXY=None, http_proxy=None):
    chk("代理值首尾空格会洗掉", anna.proxy_url() == "http://127.0.0.1:7890", anna.proxy_url())
with env(HTTPS_PROXY="", https_proxy="http://127.0.0.1:7891", ALL_PROXY=None,
         HTTP_PROXY=None, http_proxy=None):
    chk("大小写两种写法都认", anna.proxy_url() == "http://127.0.0.1:7891", anna.proxy_url())

# ── 7 入库：一份下载 → 一本真书（走的就是用户自己拖文件那条路） ──
fresh_state()
shutil.rmtree(SHELF, ignore_errors=True)
r = anna.ingest(SHELF, COVERS, d1, title="示范这本", author="张三", data_dir=DD)
chk("Markdown 收得下，回一句人话", r.get("ok") and "示范这本" in r.get("msg", ""), r)
chk("入库后「最近」里第一本就是它",
    anna.read_state(DD)["books"][0]["title"] == "示范这本", anna.read_state(DD))
chk("入库计数 +1", anna.read_state(DD)["imported"] == 1)
chk("书目录真的建出来了", os.path.isdir(os.path.join(SHELF, r["book"]["id"])), r["book"]["id"])
chk("章节切出来了（不是一本空壳）", r["book"]["chapters"] >= 2, r["book"])
chk("书 id 用 anna_ 前缀（跟用户自己导入的区分得开）", r["book"]["id"].startswith("anna_"), r["book"]["id"])
chk("anna_ 前缀归「本地书架」这一格，不是新开一格",
    book_layout.module_of(r["book"]["id"]) == "local", book_layout.module_of(r["book"]["id"]))
chk("这本书的 meta 写的是 local 来源",
    json.load(open(os.path.join(SHELF, r["book"]["id"], "meta.json"), encoding="utf-8"))["source"] == "local")

dup = anna.ingest(SHELF, COVERS, d1, title="示范这本", author="张三", data_dir=DD)
chk("同一本下两次不重复入库", dup.get("ok") and dup.get("dup") and "之前已经收过" in dup["msg"], dup)
chk("重复那次没把计数推上去", anna.read_state(DD)["imported"] == 1, anna.read_state(DD))
chk("重复那次也没往「最近」里再塞一条", len(anna.read_state(DD)["books"]) == 1)

r2 = anna.ingest(SHELF, COVERS, d2, data_dir=DD)
chk("mobi 不收，直说格式不认", not r2.get("ok") and "格式不认" in r2["msg"], r2)
chk("不收的那份原文件留着（那是用户点的一次下载）", os.path.isfile(d2))
st2 = anna.read_state(DD)
chk("没收进来的记进 pending 并带原因",
    st2["pending"][0]["name"] == "第二本.mobi" and "格式不认" in st2["pending"][0]["reason"], st2["pending"])
chk("没收进来的计数 +1", st2["skipped"] == 1 and st2["imported"] == 1, st2)
chk("回执里说了「有一本没进书架」", "没进书架" in st2["note"], st2["note"])

empty = write_incoming("空的.md", b"")
r3 = anna.ingest(SHELF, COVERS, empty, data_dir=DD)
chk("空文件被导入器挡下，原样留着并写明原因",
    not r3.get("ok") and os.path.isfile(empty) and r3["msg"], r3)

big_md = write_incoming("超大.md", b"# Big\n\n" + b"x" * 4096)
old_max = book_import.MAX_BYTES
book_import.MAX_BYTES = 100
try:
    r4 = anna.ingest(SHELF, COVERS, big_md, data_dir=DD)
finally:
    book_import.MAX_BYTES = old_max
chk("超过导入上限时明说上限，而不是假装成功",
    not r4.get("ok") and "太大" in r4["msg"] and "上限" in r4["msg"], r4)
chk("太大那份也留在下载格里没被删", os.path.isfile(big_md))
chk("上限恢复原值（不许把 200MB 留在 monkeypatch 上）", book_import.MAX_BYTES == old_max)

r5 = anna.ingest(SHELF, COVERS, os.path.join(DD, "根本不存在.md"), data_dir=DD)
chk("文件不见了回「没找到这份文件」", not r5.get("ok") and r5["msg"] == "没找到这份文件", r5)
r6 = anna.ingest(SHELF, COVERS, "", data_dir=DD)
chk("连路径都没给也不炸", not r6.get("ok"), r6)
r7 = anna.ingest(SHELF, COVERS, d1, data_dir="")
chk("没给数据目录时照样能入库（只是不记账）", r7.get("ok") or r7.get("dup"), r7)

# 隐私：账本里出现的只能是名字与数字，绝不能有本机路径。
st_all = json.dumps(anna.read_state(DD), ensure_ascii=False)
chk("账本里没有绝对路径（那份 json 会经 /api/state 进界面）",
    DD not in st_all and "/Users/" not in st_all and "/var/" not in st_all and "/private/" not in st_all,
    st_all[:200])

# ── 8 counted：窗口那头的唯一写口 ──────────────────────────────
fresh_state()
anna.counted(DD, {"caught_delta": 1, "window": "open"})
anna.counted(DD, {"caught_delta": 2, "note": "接到一份下载：另一本.epub"})
st3 = anna.read_state(DD)
chk("caught_delta 是累加不是覆盖", st3["caught"] == 3, st3)
chk("顺手写的状态与回执都落盘了", st3["window"] == "open" and "另一本" in st3["note"], st3)
chk("负增量不会把计数拉成负数", (anna.counted(DD, {"caught_delta": -9})["caught"]) >= 0)
chk("脏 patch（不是字典）原样回账本，不写坏",
    isinstance(anna.counted(DD, "字符串"), dict) and os.path.isfile(anna.state_path(DD)))

# ── 9 口令条：界面 → 窗口那块牌子 ──────────────────────────────
anna.write_cmd(DD, "示范关键词")
c = anna.read_cmd(DD)
chk("牌子写得上也读得出", c.get("term") == "示范关键词" and c.get("ts") > 0, c)
chk("长口令截到 60 字", len(anna.write_cmd(DD, "长" * 200)["term"]) == 60)
chk("空口令也只是个空牌子（窗口自己会忽略）", anna.read_cmd(os.path.join(DD, "没写过")) == {})

# ── 10 窗口那条路的纯逻辑（不开浏览器也能验） ───────────────────
args = ab.browser_args("http://127.0.0.1:7890")
chk("探到代理就显式挂给浏览器", "--proxy-server=http://127.0.0.1:7890" in args, args)
chk("没代理时不挂（当直连，不塞死地址）",
    not any(a.startswith("--proxy-server") for a in ab.browser_args("")))
chk("启动参数里带着「不是机器人」那一条",
    "--disable-blink-features=AutomationControlled" in args)
chk("窗口尺寸给了个够搜书的", any(a.startswith("--window-size") for a in args))

with env(GUIZANG_DATA=DD):
    chk("数据目录吃 GUIZANG_DATA（装成 app 时指向用户目录）", ab.data_dir() == DD, ab.data_dir())
    # shelf_dir 的兜底那条会 makedirs，所以这一条只在沙盒里跑：它顺手建出来的
    # 那格必须是沙盒下的，否则这套件就把「本地书架」建到开发机的仓库里去了。
    # 兜底量的是「书库」那把尺子（GUIZANG_BOOKS），不是数据目录 —— 断言跟着它
    # 真正用的那把尺子量，才能在门禁沙盒与临时目录两种摆法下都成立。
    sd = ab.shelf_dir(DD)
    chk("没指定输出目录时兜底到书库的「本地书架」那一格",
        sd.endswith(book_layout.DIR_OF["local"]), sd)
    BOOKROOT = (os.environ.get("GUIZANG_BOOKS") or "").strip() or DD
    chk("兜底那一格建在这套件用的书库里，不碰用户目录",
        os.path.realpath(sd).startswith(os.path.realpath(BOOKROOT)), (sd, BOOKROOT))
with env(GUIZANG_OUTPUT=os.path.join(DD, "指定书架")):
    chk("输出目录吃 GUIZANG_OUTPUT（后端已经指好本地书架那一格）",
        ab.shelf_dir(DD) == os.path.join(DD, "指定书架"), ab.shelf_dir(DD))
with env(GUIZANG_DATA=""):
    chk("没给数据目录时退回当前工作目录（源码直接跑那一种）",
        ab.data_dir() == os.getcwd(), ab.data_dir())
chk("封面格用的是 cache/covers（跟界面显示封面同一处）",
    ab.covers_dir(DD) == os.path.join(DD, "cache", "covers"))

fresh_state()
q = []
fd = FakeDownload("这一本.md", MD_BYTES)
ab.handle_download(DD, q, fd)
chk("接住一份下载：原样落到原始下载格", len(q) == 1 and os.path.isfile(q[0][0]))
chk("接住之后 caught +1", anna.read_state(DD)["caught"] == 1, anna.read_state(DD))
chk("落盘文件名是洗过的", q and "/" not in q[0][1] and q[0][1].endswith(".md"), q)
chk("状态顺手记成窗口开着", anna.read_state(DD)["window"] == "open")
fd2 = FakeDownload("这一本.md", B("# 另一本\n\n内容内容内容\n"))
ab.handle_download(DD, q, fd2)
chk("同名不覆盖：加时间戳尾巴，两份都留着",
    len(q) == 2 and q[1][1] != q[0][1] and os.path.isfile(q[0][0]) and os.path.isfile(q[1][0]),
    [x[1] for x in q])
chk("两份各算一份，caught 推到 2", anna.read_state(DD)["caught"] == 2)
evil = FakeDownload("../../逃出去.md", B("# 逃\n\n正文一段\n"))
ab.handle_download(DD, q, evil)
chk("远端给的路径逃不出下载格",
    all(os.path.dirname(p) == anna.incoming_dir(DD) for p, _n in q), [p for p, _n in q])
dotdot = FakeDownload("..", b"x")
ab.handle_download(DD, q, dotdot)
chk("远端给的名字是纯点号时也不会写到上一级",
    all(os.path.dirname(p) == anna.incoming_dir(DD) for p, _n in q), [p for p, _n in q])
before = len(q)
boom = FakeDownload("断了的一份.md", b"", fail=True)
ab.handle_download(DD, q, boom)
chk("这份没接住时不排队、也不谎报接住了",
    len(q) == before and "没接住" in anna.read_state(DD)["note"], anna.read_state(DD)["note"])
chk("失败原因同时写进 last_error（界面那一行看得见）",
    "没接住" in anna.read_state(DD)["last_error"], anna.read_state(DD))

fresh_state()
q3 = [(d1, "第一本.md")]
n = ab.drain(DD, SHELF, COVERS, q3)
chk("排队的一份被转成书并从队列里取走", n == 1 and q3 == [], (n, q3))
chk("drain 之后账本记着入库 1 本", anna.read_state(DD)["imported"] == 1, anna.read_state(DD))
chk("drain 空队列不报错", ab.drain(DD, SHELF, COVERS, []) == 0)

fresh_state()
anna.write_cmd(DD, "换个词搜")
fp = FakePage()
seen = (None, None)
seen = ab.check_cmd(DD, seen, fp)
chk("牌子上换了词就把窗口领过去", len(fp.urls) == 1 and "/search?q=" in fp.urls[0], fp.urls)
chk("领过去之后把关键词记回账本", anna.read_state(DD)["keyword"] == "换个词搜")
same = ab.check_cmd(DD, seen, fp)
chk("同一块牌子不去重复导航（不然窗口会被拽着来回跳）",
    len(fp.urls) == 1 and same == seen, fp.urls)
anna.write_cmd(DD, "")
gone = ab.check_cmd(DD, (None, None), fp)
chk("空牌子不当成「该搜了」", isinstance(gone, tuple) and len(fp.urls) == 1, fp.urls)
anna.write_cmd(DD, "再试一个词")
failing = FakePage(fail=True)
bad_stamp = ab.check_cmd(DD, (None, None), failing)
chk("搜索页没打开时写清 last_error 而不是崩掉",
    "没打开" in anna.read_state(DD)["last_error"], anna.read_state(DD))
chk("导航失败也认下这块牌子（不然每一轮都重试、窗口被拽着反复跳）",
    bad_stamp == (anna.read_cmd(DD).get("term"), anna.read_cmd(DD).get("ts")), bad_stamp)
chk("还有牌子可看时窗口继续活着（异常不外抛）",
    ab.check_cmd(DD, (None, None), FakePage(fail=True)) is not None)
chk("最后一个标签被关掉 = 窗口走完了", ab.alive(FakeCtx([])) is False)
chk("还有标签就还活着", ab.alive(FakeCtx([object()])) is True)
ab._STOP["now"] = False
ab._on_stop(15, None)
chk("点「关掉窗口」发的那颗信号立了牌子", ab._STOP["now"] is True)
ab._STOP["now"] = False

# ── 11 源码级护栏 ─────────────────────────────────────────────
html = (REPO / "ui.html").read_text(encoding="utf-8")
srv = (REPO / "ui_server.py").read_text(encoding="utf-8")
low = html.lower()
# 界面里可以有的密码框只有三处：自己的接口 Key（agentkey）、云转写的接口 Key（asrKey）、
# 自己云盘的口令（syncPass），都是「用户自己的秘密交给自己这台机器」。替第三方书店收口令
# 不在允许之列 —— 上一轮验证过：机器填不进对方的登录表单，收上来的口令只是一份额外外泄面。
# 用白名单而不是「一个都不许有」：那样会把这几条合法的一起误伤，改的人只会把整条删掉。
pw_ids = set(re.findall(r'id="([A-Za-z0-9_]+)"\s+type="password"', html))
chk("界面里的密码框只有那三处合法入口（不再多）",
    pw_ids == {"agentkey", "asrKey", "syncPass"}, sorted(pw_ids))
chk("这一路与搜书页没有任何口令输入框",
    not [i for i in pw_ids if re.search(r"anna|zlib|lib|store|search", i, re.I)], sorted(pw_ids))
chk("界面里不再出现 Z-Library 这条链路", "zlib" not in low and "z-library" not in low,
    [w for w in ("zlib", "z-library") if w in low])
chk("后端里也不剩 Z-Library", "zlib" not in srv.lower())
nav_block = srv.split("NAV_ITEMS = [")[1].split("]")[0]
chk("名册里有这一栏、归在工具组", '("anna", "安娜的档案", "tool")' in nav_block)
chk("它排在名册最末（用户要的就是「最底部新增一栏」）",
    nav_block.rstrip().rstrip(",").rstrip().endswith('("anna", "安娜的档案", "tool")'),
    nav_block.rstrip()[-60:])
brs = (REPO / "anna_browser.py").read_text(encoding="utf-8")
chk("开窗口那条动作把任务 kinds 写对（界面据此判断窗口真假）",
    'start_task("anna_browser"' in srv and 'script("anna_browser.py")' in srv)
chk("轮询状态里带着这一栏的账", '"anna": anna_state.read_state(DATA_DIR)' in srv)
chk("窗口用的域名列表与备份镜像都在（对方换域不至于全打不开）",
    "domain_candidates" in brs and "annas-archive" in anna.DOMAINS[0])
chk("浏览器脚本必须是有头的（无头会被对方在 TLS 层挡掉）", "headless=False" in brs)
chk("脚本里不写 cookie、不接令牌（这一路只接文件，登录态一律留在窗口里）",
    "remix_userid" not in brs and "remix_userkey" not in brs and ".cookies(" not in brs)
build = (REPO / "shell" / "build_macos.sh").read_text(encoding="utf-8")
chk("打包清单里有这两个新脚本", "\n  anna_state.py\n" in build and "\n  anna_browser.py\n" in build)
chk("打包清单里不再剩 Z-Library 的脚本", "zlib" not in build.lower())
clean = (REPO / "cleanup.py").read_text(encoding="utf-8")
chk("清除本地数据时把这一栏的账与浏览器档案一起算进来",
    "anna_profile" in clean and "anna.json" in clean and "anna_cmd.json" in clean)
chk("下载格（含这一路的原文件）在清除清单里", '"downloads"' in clean)
chk("清除清单里不再剩 Z-Library", "zlib" not in clean.lower())

print("\n通过 %d 项，失败 %d 项" % (PASSED[0], len(FAIL)))
for f in FAIL:
    print("  × " + f)
sys.exit(len(FAIL))
