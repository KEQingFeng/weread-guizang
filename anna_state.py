#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「安娜的档案」这一路的单一真源：镜像域名、状态账本、目录、格式判定、代理。

为什么要单独一个文件：窗口那条路（`anna_browser.py`）要开浏览器、要接下载，
而界面每 2.6 秒一次的轮询只要「读一份本地小账」。两边共用的口径 —— 状态账本
长什么样、下载落在哪、什么格式收得下、该走哪个代理、镜像域名有哪些 —— 钉在
这一处，窗口脚本和后端就不会各写一套再各自漂移。这一层不碰 playwright、
不联网，能离线单测（跟 `book_layout.py` 是一个思路）。

隐私：账本里只放文件名与字节数，绝不写绝对路径 —— 这一份 json 的内容会经
`/api/state` 进界面，也可能被抄进工单。
"""

import hashlib
import json
import math
import os
import re
import time
import uuid

try:
    import platform_compat as pc
except Exception:                                   # 打包缺模块也要能读账本
    pc = None

# 镜像域名按「实测能打开」的顺序排：头一个是本机有头浏览器实测出过搜索结果的那
# 个域，后面几个是对方常用的备用镜像。安娜的档案会轮换域名，所以这是列表不是常量。
DOMAINS = ("annas-archive.is", "annas-archive.gd", "annas-archive.org", "annas-archive.se")
DEFAULT_DOMAIN = DOMAINS[0]

STATE_FILE = os.path.join("cache", "anna.json")
# 窗口那头回来「该搜这个词了」用的口令条：界面写、窗口读，两个进程不共用内存。
CMD_FILE = os.path.join("cache", "anna_cmd.json")
# 下载下来的原始文件先落这一格，再从这儿转成书进书架。放在 cache 下是刻意的：
# 书架那一格是「已经是一本书了」的地方，还没转成功的文件不该混进去被扫成书。
INCOMING_DIR = os.path.join("cache", "downloads", "anna")

# 收得下的格式 = 现成导入器认得的那四种。mobi/djvu/zip 这类照样留在原始目录里，
# 但界面会明说「这本没进书架，因为格式不认」，不假装成功。
KEEP_EXT = ("epub", "pdf", "txt", "md")

# 没写完的下载：Chromium 自己的半截件是 `<名>.crdownload`（downloads_path 指的就是
# 这一格），归藏这一侧在原子改名之前落的是随机名 `.part`（anna_browser.handle_download），
# 旧版本改名之前还会留固定名 `.tmp`。它们都不是「一份可以补收的下载」——真拿半截
# EPUB / PDF 去解析，用户等到的是一句「这个 zip 打不开」，界面上还多一份永远收不进来
# 的条目。列清单时整份跳过（文件仍在盘上，由「清除本地数据」整格带走）。
TEMP_SUFFIXES = (".crdownload", ".part", ".tmp")

BOOKS_KEEP = 12          # 「最近入库」最多记几本
PENDING_KEEP = 12        # 「没进书架」最多记几本
SEEN_KEEP = 300          # 去重账最多记多少条摘要（超出从最旧裁）
NOTE_MAX = 160           # 一句话回执的字数上限

# 落盘文件名的两条上限。目录项上限是 255 字节（ext4 / APFS 都一样），所以只按
# 「字符数」截是不够的：120 个中文字看着只有 120 个字符，UTF-8 却是 360 字节，
# `open()` 当场 ENAMETOOLONG。留到 180 字节是给 anna_browser 撞名时追加的
# `-<时间戳>-<序号>`（十几字节）留余量，拼完仍在 255 以内。
MAX_NAME_CHARS = 120
MAX_NAME_BYTES = 180
MAX_EXT_CHARS = 16       # 长过这个的「扩展名」不是扩展名

# 落单的半个代理码点（U+D800–U+DFFF 单独出现、不成对）：浏览器的 JS 字符串被拦腰
# 截断时会留下它，json.loads 收得下（形如 "\ud83d"），于是它一路走到这一路两个
# 「量字节 / 落盘」的地方：
#   · safe_name 里 `name.encode("utf-8")` 量长度时抛 UnicodeEncodeError —— 表现是
#     「点了下载，窗口里什么也没接住」；
#   · 账本与口令条是 ensure_ascii=False 落盘的，json.dump 抛同一个错 —— 表现是
#     「点了搜索没反应 / 域名改不了」。
# 它是坏输入不是内容，摘掉比让整份东西存不下去强。成对的码点不必管：json 解码时
# 已经合成一个真字符，这里只会摘到落单的那个。
SURROGATE = re.compile("[\ud800-\udfff]")


# ── 归一化 ────────────────────────────────────────────────────────

def _blank():
    """账本该有的样子。读到残缺文件也照这儿补，界面就不用防着字段缺失。"""
    return {
        "domain": DEFAULT_DOMAIN,
        "window": "closed",         # closed / opening / open / failed
        "keyword": "",
        "caught": 0,
        "imported": 0,
        "skipped": 0,
        "seen": [],
        "books": [],
        "pending": [],
        "note": "",
        "last_error": "",
        "updated_at": 0,
    }


def _note(v):
    s = re.sub(r"\s+", " ", str(v or "")).strip()
    s = SURROGATE.sub("", s)
    return s[:NOTE_MAX]


def clean_term(term):
    """搜书关键词：去落单的半个代理码点、压空白、夹到 60 字。

    口令条、状态账里的 keyword、以及界面回话都用这一份。一个半截码点不能既写进
    账本（json.dump 会抛）、又把回包序列化打崩 —— 所以洗的口径只留在这里。
    """
    return _note(term)[:60]


def _as_int(v):
    """账里那些计数的收口。越界值（`Infinity` / `1e400`，`json` 认）走 OverflowError，
    和形状不对一样都当 0 —— 漏掉它 `read_state` 会整份退回空账，界面上看就是
    「安娜这一栏的记录全没了」。"""
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        return 0
    return n if n >= 0 else 0


# `books` / `pending` 里每条记录的字段表：形状由 `ingest` / `_pending` 定义，这也
# 就是「读回来该是什么样」的唯一出处。数字字段必须与顶层计数走同一把 `_as_int`。
_BOOK_NUMS = ("bytes", "chapters", "at")
_BOOK_TEXTS = ("name", "title", "author", "book")
_PENDING_NUMS = ("bytes", "at")
_PENDING_TEXTS = ("name", "reason")


def _finite(v):
    """递归把非有限浮点（`inf` / `-inf` / `nan`）换成 0，别的类型与结构原样。

    为什么非管不可：`json` 认 `Infinity` / `1e400` / `NaN` 这些字面量，读回来就是
    inf / nan，而 Python 的 `json.dumps` 默认 `allow_nan=True`，照写不误 —— 可它们
    不是 JSON 规范里的值，浏览器 `JSON.parse` 碰到一个就**整份拒收**。这份账本整份
    进 `/api/state`（界面每 2.6 秒轮询一次），所以一个坏数字的代价不是「某一格显示
    不对」，而是「整个界面什么都读不出来」。落在不认识的那个键上（手改 / 旧版本写
    的新字段）也一样要收，所以这一层是递归的。
    """
    if isinstance(v, float) and not math.isfinite(v):
        return 0
    if isinstance(v, dict):
        return {k: _finite(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_finite(x) for x in v]
    return v


def _clean_row(r, nums, texts):
    """把一条记录里**认得的那几个字段**收回口径；不认识的键原样留着（同便签那条账）。

    数字走 `_as_int`（能认的照认、"42" 也认，越界与形状不对归 0），文本走 `_note`
    （压空白、摘落单代理码点、夹长度）。最后整条再过一遍 `_finite`，与 `normalize`
    的契约对齐：洗出来的东西一定写得成严格 JSON。
    """
    out = dict(r)
    for k in nums:
        if k in out:
            out[k] = _as_int(out[k])
    for k in texts:
        if k in out:
            out[k] = _note(out[k])
    return _finite(out)


def normalize(raw):
    """把任意一份读出来的表洗成字段齐全的状态。脏值退回默认，不抛异常。

    `books` / `pending` 里的每条也一起收：原先只认「是对象」就原样收下，行里的
    `bytes` / `at` / `chapters` 被手改成越界数字（`1e400` → inf）时，`/api/state`
    的整份回包就是 `"bytes": Infinity` —— 浏览器 `JSON.parse` 整份失败，界面
    （每 2.6 秒轮询一次）从此什么都读不出来。见 `_clean_row` / `_finite`。
    """
    st = _blank()
    if not isinstance(raw, dict):
        return st
    dom = str(raw.get("domain") or "").strip().lower()
    st["domain"] = dom or DEFAULT_DOMAIN
    win = str(raw.get("window") or "").strip().lower()
    st["window"] = win if win in ("closed", "opening", "open", "failed") else "closed"
    st["keyword"] = clean_term(raw.get("keyword"))
    for k in ("caught", "imported", "skipped"):
        st[k] = _as_int(raw.get(k))
    seen = raw.get("seen")
    st["seen"] = [str(x) for x in seen if str(x).strip()][:SEEN_KEEP] \
        if isinstance(seen, list) else []
    for key, cap, nums, texts in (
            ("books", BOOKS_KEEP, _BOOK_NUMS, _BOOK_TEXTS),
            ("pending", PENDING_KEEP, _PENDING_NUMS, _PENDING_TEXTS)):
        rows = raw.get(key)
        out = []
        if isinstance(rows, list):
            for r in rows[:cap]:
                if isinstance(r, dict):
                    out.append(_clean_row(r, nums, texts))
        st[key] = out
    st["note"] = _note(raw.get("note"))
    st["last_error"] = _note(raw.get("last_error"))
    st["updated_at"] = _as_int(raw.get("updated_at"))
    return st


def state_path(data_dir):
    return os.path.join(data_dir or "", STATE_FILE)


def read_state(data_dir):
    """读账本。没读过就回一份全默认的 —— 首次点开这一栏也要有东西可显示。"""
    try:
        # utf-8-sig：手改 / 另存过的账本可能带 BOM，按 utf-8 读会整份解不动（被当空账）。
        with open(state_path(data_dir), encoding="utf-8-sig") as f:
            return normalize(json.load(f))
    except Exception:
        return _blank()


def _drop_surrogates(v):
    """把要落盘的东西里落单的半个代理码点摘干净（dict / list / str 逐层走）。

    这是写盘前最后一道：账本里的文本来自界面 POST、书目元数据与远端文件名，谁
    都可能夹一个半截码点，而 ensure_ascii=False 的 json.dump 碰到它就抛。文本以外
    的值原样回去，不改类型。
    """
    if isinstance(v, str):
        return SURROGATE.sub("", v)
    if isinstance(v, dict):
        return {k: _drop_surrogates(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_drop_surrogates(x) for x in v]
    return v


# 这一个落盘口临时件的名字为什么必须**一次一个**（进程号 + 一小串随机），不能写死成
# `path + ".tmp"`：
#
#   · 这一份账本有两个**进程**在写 —— 后端请求线程（`/api/anna` 的 search / domain /
#     清账）与窗口那一趟子进程（`anna_browser.py` 领关键词、接住下载、收尾报数）。
#     后端又是 `ThreadingHTTPServer`，同一进程里两个请求也能同时进来
#     （`write_state` 这一路没走 `_WRITE_LOCK`）。
#   · 临时件同名时，后来者 `open(..., "w")` 会把前一个人已经写了一半的临时件截断，
#     两边各自按自己的偏移往下写，最后 `os.replace` 把**两份内容交织在一起**的那一份
#     顶成正式账本。`read_state` 解不动就整份退回空账，界面上看到的是「入库记录全没了」
#     —— 而账本是刚刚两个进程各自按规矩写的。
#   · 还有当场抛异常的那一种：先收尾的那一个 `os.replace` 会把共用的临时件整个搬走，
#     后一个写手轮到自己那一句时临时件已经不在，抛 `FileNotFoundError`。这条异常从
#     `counted()` / `write_state()` 直接穿出去，而 `anna_browser.handle_download` 是在
#     `counted()` **之后**才 `queue.append(...)` 的 —— 文件接住了却永远排不进队列
#     （书架上就是没有这本，得等下一次「补收」）。
#   · 口径与 `flomo_notes`（带进程号）和 `ui_server._write_json`（进程号 + 随机串）
#     一致；这里取两者里更严的那一档，因为这条路上真的有两个进程。
def _atomic_write(path, payload):
    """原子落盘，并把摘过坏字符的那份回给调用方（界面回话要用它，不能各说各的）。

    临时件一次一个名字、写失败先收干净再抛：理由在函数上面那段注释里。
    这只解决「账本被写坏」，不解决「读—改—写互相覆盖」——两个写手先后落盘时后一个
    仍可能基于旧读数，所以计数仍以最后一次为准。
    """
    payload = _drop_surrogates(payload)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = "%s.%d.%s.tmp" % (path, os.getpid(), uuid.uuid4().hex[:8])
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        # 与 export_precise._write_json / sync._write_atomic 同一档：窗口那一趟是会被
        # 停止键收走的子进程，KeyboardInterrupt 这类不继承 Exception，也要收干净。
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return payload


def write_state(data_dir, patch):
    """改账本唯一入口：读旧、并进补丁、再原子落盘，回洗完之后的那份。

    窗口进程和后端请求线程都会写这一份，所以只能「先读后写 + 原子替换」；
    直接覆盖会把对方刚写进去的入库记录抹掉。
    """
    st = read_state(data_dir)
    if isinstance(patch, dict):
        st.update(patch)
    st["updated_at"] = int(time.time())
    return _atomic_write(state_path(data_dir), st)


# ── 口令条（界面 → 窗口）────────────────────────────────────────────

def cmd_path(data_dir):
    return os.path.join(data_dir or "", CMD_FILE)


def write_cmd(data_dir, term):
    """把「该搜这个词」立个牌子给窗口看。回洗过的口令条。"""
    payload = {"term": clean_term(term), "ts": time.time()}
    return _atomic_write(cmd_path(data_dir), payload)


def read_cmd(data_dir):
    """读口令条唯一入口：坏 JSON / 半截档 / 非对象一律当「没写过」。

    这一份和账本一样会被人手改、也可能留着旧版本写下的脏值，所以 `term` 在**读口**
    就过一遍 `clean_term`（写口令条时用的是同一把尺子）。不洗的话，一个落单的半个
    代理码点（形如 `{"term": "三体\\ud800"}`）会跟着 term 一路走到窗口那边：拼搜索
    地址时 `quote` 抛 UnicodeEncodeError（这一句在 `check_cmd` 的 try 之外，异常直接
    穿出、整趟窗口以「这一趟没走通」收摊），打进日志时 stdout 也抛同一个错（被 catch
    成一句假的「这一页没打开」）。洗在唯一入口上，三个下游（搜地址 / 日志 / 写回账本）
    都不必各自再防。
    """
    try:
        with open(cmd_path(data_dir), encoding="utf-8-sig") as f:
            d = json.load(f)
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}
    return dict(d, term=clean_term(d.get("term")))


# ── 地址与域名 ────────────────────────────────────────────────────

def clean_domain(domain):
    """把用户填的域名洗成一个干净的 host：剥协议、剥路径、补默认。"""
    d = str(domain or "").strip().lower()
    d = SURROGATE.sub("", d)
    d = re.sub(r"^https?://", "", d)
    d = d.split("/")[0].strip().strip(":")
    return d or DEFAULT_DOMAIN


def with_scheme(domain):
    d = clean_domain(domain)
    return d if d.startswith("http") else "https://" + d


def search_url(term, domain=None):
    """搜索页地址。关键词按 URL 编码，中文直接拼进去会打成乱码。

    参数名用的是 `q` 不是 `term`：2026-10-07 在有头窗口里实测，`/search?term=…`
    会照常画出四十行搜索结果，但那批书目与关键词毫无关系（换三个不同的词拿到同
    一列俄语书），而页面自己的搜索框提交的是 `/search?q=…`、搜出来才对得上。
    写成 `term` 不是报错、是「看着像结果」，比报错更坑，所以钉在这里。

    关键词仍要先过 `clean_term`：`quote` 把字符串编成 utf-8，落单的半个代理码点
    （来自手改的口令条 / 旧版本写下的脏值）在那一步抛 UnicodeEncodeError，而这一句
    在窗口主循环里、调用处没有各自的兜底。`term` 是外部文本，这把尺子又是唯一
    那把（口令条 / 状态账 / 界面回话共用），所以在这里再收一道。
    """
    from urllib.parse import quote
    kw = clean_term(term)
    base = with_scheme(domain or DEFAULT_DOMAIN)
    return base + "/search?q=" + quote(kw)


def domain_candidates(domain=None, extra=()):
    """该试哪些域：用户设的那个排最前，再按实测顺序补齐其余镜像。

    对方轮换域名是常事，只认一个域会让「今天还能用」变成「明天全打不开」。
    """
    pref = clean_domain(domain)
    out = [pref]
    for d in list(extra or ()) + list(DOMAINS):
        c = clean_domain(d)
        if c not in out:
            out.append(c)
    return out


# ── 下载落点与格式判定 ───────────────────────────────────────────

def incoming_dir(data_dir, create=False):
    p = os.path.join(data_dir or "", INCOMING_DIR)
    if create:
        os.makedirs(p, exist_ok=True)
    return p


def _fit_bytes(text, limit):
    """按 UTF-8 字节截断，且绝不切出半个字（半个字写盘就是坏字节）。"""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", "ignore")


def safe_name(filename):
    """浏览器给的文件名洗一遍再用：只留最后一截、去掉分隔符与控制字符。

    下载名来自远端站点，可能带路径、也可能就是 `../../x`。这一层不守住，
    写盘就能写到书架外面去 —— 所以宁可不信任它。

    名字还要收得住长度，而且**按字节收**：目录项的上限是 255 字节，中文一个字
    就是三个字节。按「字符数」截出来的名字看着不长，落盘那一下却直接
    ENAMETOOLONG（`open()` 抛 OSError，下载接不住）。所以两条上限同时管住：
    字符数（ASCII 怪名）与 UTF-8 字节数（中文长标题）。

    落单的半个代理码点先摘掉再量长度：`name.encode("utf-8")` 碰到它会抛
    UnicodeEncodeError，那一下是在「接住下载」的回调里，用户看到的是「点了没反应」。
    """
    name = os.path.basename(str(filename or "").replace("\\", "/")).strip()
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = SURROGATE.sub("", name)
    name = re.sub(r"[ /]+", "_", name)
    if len(name) > MAX_NAME_CHARS or len(name.encode("utf-8")) > MAX_NAME_BYTES:
        stem, dot, tail = name.rpartition(".")
        # 点号后面那一截长过 MAX_EXT_CHARS 就不是扩展名（是正文被点号切开的一截），
        # 不值得为留住它把整段正文截没。
        ext = tail if dot and 0 < len(tail) <= MAX_EXT_CHARS else ""
        cut = (len(ext) + 1) if ext else 0
        room_c = max(1, MAX_NAME_CHARS - cut)
        room_b = max(1, MAX_NAME_BYTES - cut)
        head = _fit_bytes(stem or name, room_b)[:room_c].rstrip(". ")
        name = ("%s.%s" % (head, ext)) if (head and ext) else (head or ext)
    # 只剩点号的名字（"." / ".."）拼上去就是「上一级」：basename 挡不住它，
    # 因为它本来就是最后一截。这一类一律当没名字处理，绝不拿去拼路径。
    if not name.strip("."):
        return "未命名"
    return name or "未命名"


def ext_of(filename):
    """小写扩展名（不带点）。认不出回空串。"""
    tail = os.path.splitext(str(filename or ""))[1].lower().lstrip(".")
    return re.sub(r"[^a-z0-9]", "", tail)


def keepable(filename):
    return ext_of(filename) in KEEP_EXT


def classify(filename, blob_head=b""):
    """判断这份下载能不能直接转成书：收得下的回扩展名，收不下的回空串。

    站点有时给个没扩展名的链接，所以再看一眼文件头 —— PDF 的 `%PDF` 与 EPUB 的
    `PK` 一眼就能认，和导入器那套判据对齐，免得这里放行、那里报「认不出格式」。
    """
    head = bytes(blob_head[:4] or b"")
    if ext_of(filename) in KEEP_EXT:
        return ext_of(filename)
    if head[:4] == b"%PDF":
        return "pdf"
    if head[:2] == b"PK":
        return "epub"
    return ""


def digest(blob):
    """一份字节一个摘要：同一本下载两次不会重复进书架（站点的近重复条目很多）。"""
    return hashlib.md5(bytes(blob or b"")).hexdigest()


def mark_seen(state, md5):
    """把摘要记进去重账。回 (是否第一次见, 新的账本)。"""
    st = normalize(state)
    key = str(md5 or "").strip()
    if not key:
        return True, st
    if key in st["seen"]:
        return False, st
    st["seen"] = (st["seen"] + [key])[-SEEN_KEEP:]
    return True, st


def push_book(state, row):
    """最近入库往前插一条，长的挤掉尾巴。"""
    st = normalize(state)
    st["books"] = ([row] + st["books"])[:BOOKS_KEEP]
    return st


def push_pending(state, row):
    st = normalize(state)
    st["pending"] = ([row] + st["pending"])[:PENDING_KEEP]
    return st


def _unfinished(name):
    """这份文件名算不算「没写完」：Chromium 的 `.crdownload` 与那两族临时尾巴。"""
    return str(name or "").lower().endswith(TEMP_SUFFIXES)


def incoming_listing(dirpath, limit=20):
    """列一眼原始下载那一格：文件名、字节、时间。只给名字不给路径。

    没写完的下载（`.crdownload` / `.part` / `.tmp`）整份跳过：它们只可能在断电、
    强杀或浏览器自己写到一半时出现，既转不成书，也不该让「补收」去试 —— 见
    `TEMP_SUFFIXES` 那段。正常文件一个字不改。
    """
    out = []
    try:
        for name in os.listdir(dirpath or ""):
            if _unfinished(name):
                continue
            p = os.path.join(dirpath, name)
            if not os.path.isfile(p):
                continue
            try:
                st = os.stat(p)
            except Exception:
                continue
            out.append({"name": name, "bytes": int(st.st_size),
                        "mtime": int(st.st_mtime), "ok": keepable(name)})
    except Exception:
        return []
    out.sort(key=lambda r: (-r["mtime"], r["name"]))
    return out[:limit]


def proxy_url():
    """该借哪个代理出去：环境里显式给的优先，否则读系统设置并探端口。

    探不通就不挂 —— 代理关了却留着系统设置时，硬塞一个死地址会把本来能成的
    直连一起拖垮。socks 代理 Chromium 能用、urllib 不能用，但这一路只喂浏览器。
    """
    for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "HTTP_PROXY", "http_proxy"):
        v = (os.environ.get(k) or "").strip()
        if v:
            return v
    try:
        p = urllib_getproxies()
    except Exception:
        return ""
    for k in ("https", "http", "socks5", "all"):
        v = (p.get(k) or "").strip()
        if not v:
            continue
        if v.startswith("socks") and pc is None:
            continue
        if pc is not None and hasattr(pc, "_proxy_alive") and not pc._proxy_alive(v):
            continue
        return v
    return ""


def urllib_getproxies():
    """单独包一层：`read_state` 之类不联网的函数不该因为导入 urllib 而受影响。"""
    import urllib.request
    return urllib.request.getproxies()


# ── 把一份下载转成一本书 ─────────────────────────────────────────

def ingest(shelf_root, cover_dir, filepath, title="", author="", data_dir=""):
    """把原始目录里的一份下载收进「本地书架」，回一句人话。

    走的是现成那条导入路（和用户自己拖文件进来完全一样），所以阅读器、导出、
    在文件管理器里定位这些能力一行不用改。三个口径在这一处守住：
      · 格式不认（mobi / djvu / zip 那一类）—— 原文件留着，记进 pending，说清为什么；
      · 同一本下两次 —— 按字节摘要跳过，不给书架造重复条目（站点的近重复条目很多）；
      · 解析失败 / 太大 —— 原文件照样留着，错因写进 pending，不会被一句「没成功」抹平。
    原文件一律不删：那是用户点的一次下载，转坏了还得留着手。
    """
    import book_import

    name = safe_name(os.path.basename(str(filepath or "")))
    if not name or not filepath or not os.path.isfile(filepath):
        return {"ok": False, "msg": "没找到这份文件", "name": name}
    try:
        size = os.path.getsize(filepath)
    except Exception:
        size = 0
    if size > book_import.MAX_BYTES:
        note = "文件太大（%d MB），归藏的导入上限是 %d MB，原文件留着没动" % (
            size // 1024 // 1024, book_import.MAX_BYTES // 1024 // 1024)
        _pending(data_dir, name, note, size)
        return {"ok": False, "msg": note, "name": name}
    try:
        with open(filepath, "rb") as f:
            blob = f.read()
    except Exception as e:
        note = "读不出来：%s" % (type(e).__name__)
        _pending(data_dir, name, note, size)
        return {"ok": False, "msg": note, "name": name}

    fmt = classify(name, blob[:8])
    if not fmt:
        note = "格式不认（现在只收 EPUB / PDF / TXT / Markdown），原文件留着没动"
        _pending(data_dir, name, note, size)
        return {"ok": False, "msg": note, "name": name, "pending": True}

    md5 = digest(blob)
    st = read_state(data_dir) if data_dir else _blank()
    first, st = mark_seen(st, md5)
    if not first:
        return {"ok": True, "msg": "这一本之前已经收过了，没有重复入库",
                "name": name, "dup": True}

    try:
        info = book_import.import_book(
            shelf_root, name, blob, title=str(title or "").strip(),
            author=str(author or "").strip(), cover_dir=cover_dir,
            book_id_prefix="anna", source="local")
    except ValueError as e:                  # 认不出格式 / 空文件 / 没读出正文
        note = str(e)[:NOTE_MAX] or "这份文件里没读出正文"
        _pending(data_dir, name, note, size)
        return {"ok": False, "msg": note, "name": name, "pending": True}
    except Exception as e:
        note = "没能转成书：%s" % (type(e).__name__)
        _pending(data_dir, name, note, size)
        return {"ok": False, "msg": note, "name": name, "pending": True}

    row = {"name": name, "title": info.get("title") or "", "author": info.get("author") or "",
           "book": info.get("id") or "", "bytes": size, "chapters": info.get("chapters") or 0,
           "at": int(time.time())}
    st = push_book(st, row)
    st["imported"] = _as_int(st.get("imported")) + 1
    st["note"] = "已收进本地书架：《%s》" % (row["title"] or name)
    if data_dir:
        write_state(data_dir, {k: st[k] for k in
                               ("seen", "books", "imported", "pending", "note")})
    return {"ok": True, "msg": "已收进本地书架：《%s》（%d 章）"
                               % (row["title"] or name, row["chapters"]),
            "name": name, "book": info, "fmt": fmt}


def _pending(data_dir, name, reason, size):
    """记一笔「这份没进书架」以及为什么。界面要把原因显示出来，不能只说没成功。"""
    if not data_dir:
        return
    st = read_state(data_dir)
    row = {"name": name, "reason": _note(reason), "bytes": _as_int(size),
           "at": int(time.time())}
    st = push_pending(st, row)
    st["skipped"] = _as_int(st.get("skipped")) + 1
    st["note"] = "有一本没进书架：%s" % _note(reason)
    write_state(data_dir, {"pending": st["pending"], "skipped": st["skipped"],
                           "note": st["note"]})


def counted(data_dir, patch):
    """带计数的改账本（接住几个 / 最近一句话）。窗口那条路只用这一个口写状态。"""
    st = read_state(data_dir)
    if not isinstance(patch, dict):
        return st
    if "caught_delta" in patch:
        st["caught"] = _as_int(st.get("caught")) + _as_int(patch.pop("caught_delta"))
    st.update(patch)
    return write_state(data_dir, st)

