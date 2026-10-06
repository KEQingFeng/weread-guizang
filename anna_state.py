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
import os
import re
import time

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

BOOKS_KEEP = 12          # 「最近入库」最多记几本
PENDING_KEEP = 12        # 「没进书架」最多记几本
SEEN_KEEP = 300          # 去重账最多记多少条摘要（超出从最旧裁）
NOTE_MAX = 160           # 一句话回执的字数上限


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
    return s[:NOTE_MAX]


def _as_int(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0
    return n if n >= 0 else 0


def normalize(raw):
    """把任意一份读出来的表洗成字段齐全的状态。脏值退回默认，不抛异常。"""
    st = _blank()
    if not isinstance(raw, dict):
        return st
    dom = str(raw.get("domain") or "").strip().lower()
    st["domain"] = dom or DEFAULT_DOMAIN
    win = str(raw.get("window") or "").strip().lower()
    st["window"] = win if win in ("closed", "opening", "open", "failed") else "closed"
    st["keyword"] = _note(raw.get("keyword"))[:60]
    for k in ("caught", "imported", "skipped"):
        st[k] = _as_int(raw.get(k))
    seen = raw.get("seen")
    st["seen"] = [str(x) for x in seen if str(x).strip()][:SEEN_KEEP] \
        if isinstance(seen, list) else []
    for key, cap in (("books", BOOKS_KEEP), ("pending", PENDING_KEEP)):
        rows = raw.get(key)
        out = []
        if isinstance(rows, list):
            for r in rows[:cap]:
                if isinstance(r, dict):
                    out.append(r)
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
        with open(state_path(data_dir), encoding="utf-8") as f:
            return normalize(json.load(f))
    except Exception:
        return _blank()


def _atomic_write(path, payload):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def write_state(data_dir, patch):
    """改账本唯一入口：读旧、并进补丁、再原子落盘，回洗完之后的那份。

    窗口进程和后端请求线程都会写这一份，所以只能「先读后写 + 原子替换」；
    直接覆盖会把对方刚写进去的入库记录抹掉。
    """
    st = read_state(data_dir)
    if isinstance(patch, dict):
        st.update(patch)
    st["updated_at"] = int(time.time())
    _atomic_write(state_path(data_dir), st)
    return st


# ── 口令条（界面 → 窗口）────────────────────────────────────────────

def cmd_path(data_dir):
    return os.path.join(data_dir or "", CMD_FILE)


def write_cmd(data_dir, term):
    """把「该搜这个词」立个牌子给窗口看。回洗过的口令条。"""
    payload = {"term": _note(term)[:60], "ts": time.time()}
    _atomic_write(cmd_path(data_dir), payload)
    return payload


def read_cmd(data_dir):
    try:
        with open(cmd_path(data_dir), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


# ── 地址与域名 ────────────────────────────────────────────────────

def clean_domain(domain):
    """把用户填的域名洗成一个干净的 host：剥协议、剥路径、补默认。"""
    d = str(domain or "").strip().lower()
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
    """
    from urllib.parse import quote
    kw = str(term or "").strip()
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


def safe_name(filename):
    """浏览器给的文件名洗一遍再用：只留最后一截、去掉分隔符与控制字符。

    下载名来自远端站点，可能带路径、也可能就是 `../../x`。这一层不守住，
    写盘就能写到书架外面去 —— 所以宁可不信任它。
    """
    name = os.path.basename(str(filename or "").replace("\\", "/")).strip()
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = re.sub(r"[ /]+", "_", name)
    if len(name) > 120:
        stem, dot, tail = name[:100], ".", name.split(".")[-1]
        name = stem + dot + tail if dot in name else stem
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


def incoming_listing(dirpath, limit=20):
    """列一眼原始下载那一格：文件名、字节、时间。只给名字不给路径。"""
    out = []
    try:
        for name in os.listdir(dirpath or ""):
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
    out.sort(key=lambda r: r["mtime"], reverse=True)
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

