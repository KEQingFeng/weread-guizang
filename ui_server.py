#!/usr/bin/env python3
"""微信读书导出 — 本地 Web 界面后端（仅监听 127.0.0.1）。

职责：包装 export_precise.py / download_images.py / login.py，把 stdout 实时推到前端，
并列出已导出的书籍、打包下载（正文 + 图片，修正相对路径）。

启动: 项目虚拟环境里的 python 跑 ui_server.py --port 8770
     （Windows 是 .venv\\Scripts\\python.exe，macOS/Linux 是 .venv/bin/python；
       两者都由 platform_compat.venv_python 自动解析，也可用 GUIZANG_PYTHON 指定）
"""
import argparse
import base64
import glob
import hashlib
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

import platform_compat as pc
import book_export
import book_import

REPO = os.path.dirname(os.path.abspath(__file__))

# 版本号只写在这一处：shell/build_macos.sh 会把它读出来盖进 Info.plist，
# 打的 dmg 也就跟着叫同一个名字，不会再出现「界面一个数、访达另一个数」。
# 界面「关于」那一类要显示它 —— 用户报问题时先问「你装的哪一版」，界面上能直接看到。
VERSION = "0.9.3"


def py():
    """该用哪个解释器 —— 每次现算，不在导入时定死。

    解释器按平台解析：Windows 的虚拟环境在 Scripts 子目录（python.exe），原来写死
    POSIX 的 .venv/bin/python，Windows 上所有「起子进程」的功能都是一点就哑火。

    之所以是个函数而不是常量：首次运行时虚拟环境还不存在，此刻只能用系统 Python；
    等「我思故我在」把 .venv 建好，后面起的任务必须立刻换成 .venv 里的解释器 ——
    导入时算一次的话，会一直攥着那个「还没装依赖的系统 Python」去跑引擎，
    表现为配好环境后第一次取书仍然报「找不到 playwright」。
    """
    return pc.venv_python(REPO)


def script(name):
    """项目脚本的绝对路径。

    子进程的工作目录是数据目录，不是源码目录 —— 引擎与登录脚本里的
    `output/`、`cache/` 都是相对路径（见 export_precise.py:1248、login.py:31），
    让 cwd 落在数据那头，它们就自己写对了地方，一行都不用改。
    代价是脚本名不能再靠 cwd 去找，必须给全路径。
    """
    return os.path.join(REPO, name)


# 数据落在哪由 platform_compat 一处说了算：源码直接跑 = 项目目录本身，
# 装成 app = 壳通过 GUIZANG_DATA 指到用户目录（包体不写东西）。
DATA_DIR = pc.data_dir(REPO)
# 书库：取回的书、图片、合并稿都在这儿（界面「书架」读的就是它）。
# 装成 app 之后壳会把 GUIZANG_BOOKS 指到用户文档下的「归藏」，直接能在访达里翻；
# 源码直接跑则退回数据目录下的 output/，行为与从前一致。见 platform_compat.books_dir。
OUT_DIR = pc.books_dir(REPO)
CACHE_DIR = os.path.join(DATA_DIR, "cache")
LOGIN_STATE = os.path.join(CACHE_DIR, "login_state.json")
DOWNLOAD_DIR = os.path.join(CACHE_DIR, "downloads")
LIB_PATH = os.path.join(CACHE_DIR, "library.json")
CFG_PATH = os.path.join(CACHE_DIR, "config.json")
COVER_DIR = os.path.join(CACHE_DIR, "covers")
WALL_PATH = os.path.join(CACHE_DIR, "wallpaper.bin")
UI_PATH = os.path.join(REPO, "ui.html")
# 离线可用的第三方前端库（页面内读正文用的 markdown 渲染器，见 vendor/README.md）
VENDOR_DIR = os.path.join(REPO, "vendor")

# 微信读书官方 Agent Gateway（用户自填 wrk- API Key）
WEREAD_GATEWAY = "https://i.weread.qq.com/api/agent/gateway"
WEREAD_SKILL_VERSION = "1.0.4"
WEREAD_APIS = {
    "/shelf/sync", "/store/search", "/book/info", "/book/chapterinfo",
    "/book/getprogress", "/user/notebooks", "/book/bookmarklist",
    "/review/list/mine", "/readdata/detail", "/book/recommend",
    "/book/similar", "/review/list", "/book/underlines",
    "/book/bestbookmarks", "/book/readreviews", "/_list",
}

# 从任意粘贴形式里认书名 id：reader / bookDetail 链接、纯 id、带空格标点的整段文本
_ID_IN_URL = re.compile(r"/(?:reader|bookDetail|book)/([0-9A-Za-z_-]{6,})")
_ID_BARE = re.compile(r"^[0-9A-Za-z_-]{6,}$")
_ID_ANY = re.compile(r"[0-9A-Za-z_-]{6,}")


def extract_book_id(raw):
    """把用户粘的任何东西解析成书籍 id —— 不让用户自己去删减内容。"""
    if not raw:
        return ""
    s = raw.strip().strip("\"'“”‘’《》<>\u3000")
    m = _ID_IN_URL.search(s)
    if m:
        return m.group(1)
    if _ID_BARE.match(s):
        return s
    # 退路：最常见的「复制了一整段话」——取里面最长的 id 形状片段
    cands = [c for c in _ID_ANY.findall(s) if not c.isalpha()]
    if cands:
        return max(cands, key=len)
    cands = _ID_ANY.findall(s)
    return max(cands, key=len) if cands else ""

# 浏览器缓存目录各平台不同（Windows 在 %LOCALAPPDATA%，Linux 在 ~/.cache）
MS_PLAYWRIGHT = pc.ms_playwright_dir()

_lock = threading.Lock()
LOG = []
LOG_BASE = 0
MAX_LOG = 8000
TASK = {"running": False, "kind": None, "book": None, "started_at": None,
        "exit_code": None, "proc": None}


# ---------- helpers ----------

def log(text):
    with _lock:
        for line in text.rstrip("\n").split("\n"):
            LOG.append({"t": time.time(), "s": line})
        if len(LOG) > MAX_LOG:
            drop = len(LOG) - MAX_LOG
            del LOG[:drop]
            global LOG_BASE
            LOG_BASE += drop


def clear_log():
    """清掉进展栏攒下的输出记录（后端这份是环形缓冲，界面右下角的「清空」动不到它）。

    正在跑的任务也照清：用户点「清除记录」就是要这里空下来，留着旧行只会被看成
    「点了没反应」。清完新输出照常往里写，LOG_BASE 跟着前移，
    前端手里的 since 游标自然落到新起点，不会把已删的行再吐回去。
    """
    with _lock:
        dropped = len(LOG)
        LOG.clear()
        global LOG_BASE
        LOG_BASE += dropped
    running = TASK.get("running")
    return {"ok": True, "dropped": dropped,
            "msg": "输出记录已经是空的" if not dropped else
                   f"已清掉 {dropped} 行输出记录"
                   + ("，这次任务的后续输出会继续记在这里" if running else "")}


def chromium_ready():
    for pat in ("chromium-*", "chromium_headless_shell-*"):
        for d in glob.glob(os.path.join(MS_PLAYWRIGHT, pat)):
            if os.path.isdir(d):
                return True
    return False


def read_login_state():
    try:
        with open(LOGIN_STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def safe_book_dir(book_id):
    if not book_id or not re.fullmatch(r"[A-Za-z0-9_\-]+", book_id):
        return None
    d = os.path.realpath(os.path.join(OUT_DIR, book_id))
    if not d.startswith(os.path.realpath(OUT_DIR) + os.sep):
        return None
    return d if os.path.isdir(d) else None


def ensure_books_dir():
    """把书库目录建出来，并把老位置里的书搬过来一次。

    0.9.1 及更早把书存在 ~/Library/Application Support/归藏/output —— 藏得深，
    用户基本找不到。0.9.3 起改存用户文档下的「归藏」，取过的书一眼可见。
    升级时若新位置还空着、老位置却有书，就整目录搬过去；不搬的话用户升完级
    会以为「我的书全没了」。只在新书库不存在（或为空）时搬，绝不动已有内容的书库。
    """
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
    except OSError:
        return
    old = os.path.join(DATA_DIR, "output")
    if os.path.realpath(old) == os.path.realpath(OUT_DIR) or not os.path.isdir(old):
        return
    try:
        if os.listdir(OUT_DIR):          # 新书库已经有东西，别去覆盖
            return
        moved = 0
        for name in os.listdir(old):
            if name.startswith("."):
                continue
            try:
                shutil.move(os.path.join(old, name), os.path.join(OUT_DIR, name))
                moved += 1
            except OSError:
                pass
        if moved:
            log(f"--- 已把 {moved} 项旧书从数据目录搬进书库：{OUT_DIR} ---")
    except OSError:
        pass


# ---------- 书架（文件夹 / 归类 / 排序） ----------

def load_lib():
    try:
        with open(LIB_PATH, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault("folders", [])
    d.setdefault("assign", {})
    d.setdefault("order", [])
    d.setdefault("state", {})
    return d


def save_lib(d):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = LIB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LIB_PATH)


def prune_lib(lib):
    """丢掉已经不存在的书留下的记录。"""
    alive = set()
    if os.path.isdir(OUT_DIR):
        alive = {n for n in os.listdir(OUT_DIR)
                 if os.path.isdir(os.path.join(OUT_DIR, n))}
    lib["assign"] = {k: v for k, v in lib["assign"].items() if k in alive}
    lib["order"] = [x for x in lib["order"] if x in alive]
    lib["state"] = {k: v for k, v in (lib.get("state") or {}).items() if k in alive}
    return lib


# ---------- 配置（含微信读书 API Key） ----------

def load_cfg():
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    return d if isinstance(d, dict) else {}


def save_cfg(d):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = CFG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CFG_PATH)
    try:
        os.chmod(CFG_PATH, 0o600)      # 里面存着 API Key
    except OSError:
        pass


def weread_key():
    return (load_cfg().get("weread_key") or "").strip()


# ---------- 导出位置 ----------
#
# 「正文 / 完整包」这些下载其实是**外壳**在落盘（WKDownloadDelegate），不是后端 ——
# 后端只管把这个设置存下来，外壳每次下载前现读一遍同一个 config.json。
# 所以这里不缓存，永远现读，改完立刻生效、不用重启。

def export_dir():
    return (load_cfg().get("export_dir") or "").strip()


def set_export_dir(path):
    """存导出目录。返回 (ok, 人话)。

    选一个不存在或不能写的目录是有可能的（目录被删了、选到了只读卷），
    存之前先验一次：真到下载那一刻才发现写不进去，用户看到的是「点了没反应」，
    而那时候他早忘了自己选过什么。
    """
    path = (path or "").strip()
    cfg = load_cfg()
    if not path:
        cfg.pop("export_dir", None)
        save_cfg(cfg)
        return True, "已改回系统「下载」文件夹"
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        return False, "这个文件夹不存在（或者不在本机）"
    if not os.access(path, os.W_OK):
        return False, "这个文件夹不能写，换一个吧"
    cfg["export_dir"] = path
    save_cfg(cfg)
    return True, "导出位置已设为 " + path


# ---------- 壁纸 ----------
#
# 存成文件而不是塞 localStorage：图往往几 MB，localStorage 只有 5MB 且会被字符串化翻倍。
# 平均色/亮度的判断放在前端做（canvas 采样），后端只管存与发，不引入图像库。

WALL_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
WALL_MAX = 15 * 1024 * 1024


def save_wallpaper(data_url):
    import base64
    m = re.match(r"^data:(image/[a-z+]+);base64,(.+)$", (data_url or "").strip(), re.S)
    if not m:
        raise RuntimeError("只认 base64 的图片数据")
    declared, b64 = m.group(1), m.group(2)
    try:
        raw = base64.b64decode(b64, validate=False)
    except Exception:
        raise RuntimeError("图片数据解不开")
    # 按魔数认，别信声明的类型（原来按字节数判太小会把合法的纯色小图也拒掉）
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif raw.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif raw.startswith(b"GIF8"):
        mime = "image/gif"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise RuntimeError(f"这不是有效的图片数据（声明为 {declared}）")
    if len(raw) > WALL_MAX:
        raise RuntimeError("图片太大了（请压到 15MB 以内）")
    with open(WALL_PATH, "wb") as f:
        f.write(raw)
    cfg = load_cfg()
    cfg["wall_mime"] = mime
    save_cfg(cfg)
    return len(raw)


def clear_wallpaper():
    try:
        os.remove(WALL_PATH)
    except OSError:
        pass
    cfg = load_cfg()
    cfg.pop("wall_mime", None)
    save_cfg(cfg)


# ---------- 笔记索引（划线/想法）----------
#
# 官方没有「全局搜笔记」的接口，只能逐本拉。所以本地建一份索引：
# 一次性把有笔记的书全拉一遍（含分页），之后搜索/随机漫步都走本地，
# 既快又不会反复打网关。

NOTES_IDX = os.path.join(CACHE_DIR, "notes_index.json")
APKG_DIR = os.path.join(CACHE_DIR, "apkg")
NOTES_LOCK = threading.Lock()
NOTES_STATE = {"running": False, "done": 0, "total": 0, "built_at": 0, "count": 0}
# 索引文件既会被全量重建线程整份覆盖，也会被「单本现拉」增量合并 —— 必须互斥，
# 否则合并写进去的条目会被重建结束时的那次整写吃掉。
_notes_lock = threading.Lock()


def load_notes_index():
    try:
        with open(NOTES_IDX, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("items"), list):
            return d
    except Exception:
        pass
    return {"built_at": 0, "items": [], "books": []}


def build_notes_index():
    """把有笔记的书逐本拉一遍，落成本地索引。耗时较长，放后台线程跑。"""
    if NOTES_STATE["running"]:
        return
    NOTES_STATE.update({"running": True, "done": 0, "total": 0})
    try:
        books, last = [], None
        for _ in range(20):                      # 分页上限，防跑飞
            params = {"count": 100}
            if last:
                params["lastSort"] = last
            d = weread_call("/user/notebooks", params, timeout=30)
            got = d.get("books") or []
            if not got:
                break
            books.extend(got)
            last = got[-1].get("sort")
            if not d.get("hasMore") or not last:
                break
        NOTES_STATE["total"] = len(books)
        log(f"--- 笔记索引开始：{len(books)} 本 ---")
        items = []
        for i, b in enumerate(books):
            info = b.get("book") or {}
            title = info.get("title") or b.get("bookId")
            author = info.get("author") or ""
            for kind, api in (("划线", "/book/bookmarklist"), ("想法", "/review/list/mine")):
                try:
                    d2 = weread_call(api, {"bookid" if api == "/review/list/mine" else "bookId": b["bookId"]}, timeout=25)
                except Exception:
                    continue
                if kind == "划线":
                    for x in (d2.get("updated") or []):
                        t = (x.get("markText") or "").strip()
                        if t:
                            items.append({"bookId": b["bookId"], "title": title,
                                          "author": author, "kind": kind, "text": t,
                                          "at": x.get("createTime") or 0})
                else:
                    for x in (d2.get("reviews") or []):
                        r = x.get("review") or x
                        t = (r.get("content") or r.get("abstract") or "").strip()
                        if t:
                            items.append({"bookId": b["bookId"], "title": title,
                                          "author": author, "kind": kind, "text": t,
                                          "at": r.get("createTime") or 0})
                time.sleep(0.12)                 # 别把网关打急
            NOTES_STATE["done"] = i + 1
            NOTES_STATE["count"] = len(items)      # 运行中也实时反映条数
            if (i + 1) % 10 == 0:
                log(f"--- 笔记索引 {i+1}/{len(books)}，已收 {len(items)} 条 ---")
        data = {"built_at": time.time(), "items": items,
                "books": [{"bookId": b["bookId"],
                           "title": (b.get("book") or {}).get("title") or b["bookId"]}
                          for b in books]}
        os.makedirs(CACHE_DIR, exist_ok=True)
        with _notes_lock:
            with open(NOTES_IDX, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        NOTES_STATE.update({"built_at": data["built_at"], "count": len(items)})
        log(f"--- 笔记索引完成：{len(books)} 本 / {len(items)} 条 ---")
    except Exception as e:
        log(f"--- 笔记索引失败：{str(e)[:120]} ---")
    finally:
        NOTES_STATE["running"] = False


def ensure_notes_index():
    d = load_notes_index()
    if d.get("built_at"):
        return d
    build_notes_index()
    return load_notes_index()


def book_id_pairs(any_id):
    """任一命名空间的 id → (书架 bookId, 阅读器 id)。

    划线索引与 /book/* 接口只认书架 bookId，本地目录名却是阅读器 id，
    传错一个就会「明明有划线却说没有」——所以两边都换算一次。
    """
    any_id = (any_id or "").strip()
    m = store_to_reader_map()
    if any_id in m:
        return any_id, m[any_id]
    rid = next((k for k, v in m.items() if v == any_id), "")
    return rid, any_id


def fetch_book_notes(book_id, title="", author=""):
    """直接问官方网关要这本的划线与想法（不等全量索引）。"""
    items = []
    try:
        d = weread_call("/book/bookmarklist", {"bookId": book_id}, timeout=25)
        for x in (d.get("updated") or []):
            t = (x.get("markText") or "").strip()
            if t:
                items.append({"bookId": book_id, "title": title, "author": author,
                              "kind": "划线", "text": t,
                              "at": x.get("createTime") or 0})
    except Exception:
        pass
    try:
        d = weread_call("/review/list/mine", {"bookid": book_id}, timeout=25)
        for x in (d.get("reviews") or []):
            r = x.get("review") or x
            t = (r.get("content") or r.get("abstract") or "").strip()
            if t:
                items.append({"bookId": book_id, "title": title, "author": author,
                              "kind": "想法", "text": t,
                              "at": r.get("createTime") or 0})
    except Exception:
        pass
    return items


def merge_notes_index(items):
    """把单本现拉的划线补进本地索引，下次搜索/随机漫步就带着它。"""
    if not items:
        return
    with _notes_lock:
        d = load_notes_index()
        have = {(x.get("bookId"), x.get("text")) for x in d.get("items") or []}
        added = [x for x in items if (x.get("bookId"), x.get("text")) not in have]
        if not added:
            return
        d["items"] = (d.get("items") or []) + added
        d["books"] = d.get("books") or []
        bid = added[0].get("bookId")
        if not any(b.get("bookId") == bid for b in d["books"]):
            d["books"].append({"bookId": bid, "title": added[0].get("title") or bid})
        d.setdefault("built_at", time.time())
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(NOTES_IDX, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            NOTES_STATE["count"] = len(d["items"])
            NOTES_STATE["built_at"] = d["built_at"]
        except Exception:
            pass


def notes_for_book(any_id, title="", author=""):
    """这本书的划线与想法：先查本地索引，查不到就现拉一次网关。

    返回 (items, bookId, 来源)。来源为 "" 表示两处都没有。
    """
    bid, rid = book_id_pairs(any_id)
    ids = {x for x in (any_id, bid, rid) if x}
    hit = [x for x in (load_notes_index().get("items") or [])
           if x.get("bookId") in ids]
    if hit:
        return hit, (bid or any_id), "index"
    live = fetch_book_notes(bid or any_id, title, author)
    if live:
        merge_notes_index(live)
        return live, (bid or any_id), "live"
    return [], (bid or any_id), ""


# ---------- 知识卡片：导出 .apkg ----------

def build_apkg(book_id, title):
    """把一本书的划线/想法做成 Anki 卡包：正面=原文，背面=出处与我的想法。

    划线先从本地索引找；找不到就直接问官方网关要这一本（不必等全量索引建完），
    所以「导出卡包」不会再因为「这本还没索引」而失败。
    """
    import genanki
    items, resolved, _ = notes_for_book(book_id, title)
    if not items:
        raise RuntimeError("这本没有划线或想法（确定书里有笔记？或接口 Key 未填）")
    book_id = resolved or book_id
    safe = re.sub(r"[^\w\u4e00-\u9fff-]", "_", (title or book_id))[:40] or book_id
    model = genanki.Model(
        int(hashlib.sha1(("gz" + book_id).encode()).hexdigest()[:8], 16),
        "归藏·划线卡",
        fields=[{"name": "正面"}, {"name": "背面"}],
        templates=[{
            "name": "划线",
            "qfmt": '<div class="q">{{正面}}</div>',
            "afmt": '<div class="q">{{正面}}</div><hr id=answer><div class="a">{{背面}}</div>',
        }],
        css=(".card{font-family:-apple-system,'PingFang SC',sans-serif;font-size:17px;"
             "line-height:1.75;color:#1a1e26;background:#fff;text-align:left;padding:18px}"
             ".q{font-weight:600}.a{color:#3a4150}.src{color:#8b93a3;font-size:13px;margin-top:10px}"))
    deck_id = int(hashlib.sha1(("gzd" + book_id).encode()).hexdigest()[:8], 16)
    deck = genanki.Deck(deck_id, f"归藏·{title or book_id}")
    seen = set()
    for x in items:
        t = x["text"].strip()
        if not t or t in seen:
            continue
        seen.add(t)
        when = ""
        if x.get("at"):
            try:
                when = time.strftime("%Y-%m-%d", time.localtime(x["at"]))
            except Exception:
                when = ""
        back = (f'<div class="src">{x.get("kind","")} · 《{title or book_id}》'
                + (f' · {when}' if when else '') + '</div>')
        deck.add_note(genanki.Note(model=model, fields=[t, back]))
    os.makedirs(APKG_DIR, exist_ok=True)
    dest = os.path.join(APKG_DIR, safe + ".apkg")
    genanki.Package(deck).write_to_file(dest)
    return dest, len(seen)


# ---------- 多本打包 ----------

def build_zip_multi(book_ids):
    md_dir = os.path.join(CACHE_DIR, "downloads")
    os.makedirs(md_dir, exist_ok=True)
    dest = os.path.join(md_dir, "归藏-多本打包.zip")
    n = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for bid in book_ids:
            d = safe_book_dir(bid)
            if not d:
                continue
            md = merged_markdown(bid)
            if md is None:
                continue
            title = meta_title(bid)
            safe = re.sub(r'[<>:"/\\|?*]', "_", title) or bid
            z.writestr(f"{safe}/{safe}.md", md)          # 每本一个目录，图不会互相覆盖
            img = os.path.join(d, "images")
            if os.path.isdir(img):
                for f in sorted(os.listdir(img)):
                    if not f.startswith("."):
                        z.write(os.path.join(img, f), f"{safe}/images/{f}")
            n += 1
    return dest, n



# ---------- 微信读书官方网关 ----------

def _payload(resp):
    """网关回包有的包在 data 里，有的平铺，统一取出来。"""
    if isinstance(resp, dict) and isinstance(resp.get("data"), dict):
        return resp["data"]
    return resp if isinstance(resp, dict) else {}


def weread_call(api_name, params=None, timeout=25):
    """调官方 Agent Gateway。

    官方要求：业务参数必须和 api_name、skill_version **平铺在同一层**，
    包进 params/data 里会导致参数被丢弃（文档里特别强调过这个坑）。
    """
    if api_name not in WEREAD_APIS:
        raise RuntimeError(f"不支持调用 {api_name}")
    key = weread_key()
    if not key:
        raise RuntimeError("还没填微信读书 API Key")
    body = {"api_name": api_name, "skill_version": WEREAD_SKILL_VERSION}
    body.update(params or {})
    req = urllib.request.Request(
        WEREAD_GATEWAY, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read().decode("utf-8"))
    code = resp.get("errcode")
    if code not in (0, None):
        raise RuntimeError(resp.get("errmsg") or f"网关返回 errcode={code}")
    return _payload(resp)


# ---------- 加入书架（写操作） ----------

SHELF_ADD_ID = re.compile(r"^\d{5,20}$")
_shelf_lock = threading.Lock()


def shelf_add(book_ids):
    """把书加进用户自己的微信读书书架。

    官方 Agent Gateway 是只读的（白名单里没有写接口），所以这一步复用导出用的
    登录 profile 去调网页端接口，契约见 shelf_add.py 顶部注释。
    """
    ids = []
    for x in book_ids or []:
        x = str(x or "").strip()
        if SHELF_ADD_ID.match(x) and x not in ids:
            ids.append(x)
    if not ids:
        return {"ok": False,
                "msg": "这个结果没带可添加的书籍编号（阅读器长 id 不能直接加书架）"}
    if TASK["running"]:
        return {"ok": False, "msg": "有任务正在跑，等它结束后再加书架"}

    # 同一份 profile 只能被一个浏览器握着，连点两次会互相踩锁
    if not _shelf_lock.acquire(blocking=False):
        return {"ok": False, "msg": "上一次加书架还没结束，稍等一下再点"}
    try:
        proc = subprocess.run(
            [py(), script("shelf_add.py"), *ids], cwd=DATA_DIR,
            capture_output=True, text=True, timeout=110)
        out = (proc.stdout or "").strip().splitlines()
        payload = None
        for line in reversed(out):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    payload = json.loads(line)
                except Exception:
                    payload = None
                break
        if payload is None:
            tail = (proc.stderr or "").strip().splitlines()
            detail = tail[-1][:160] if tail else f"退出码 {proc.returncode}"
            payload = {"ok": False, "msg": f"加书架没成功：{detail}"}
        log(("[书架] " + ("已添加 " if payload.get("ok") else "失败 ")
             + "、".join(ids) + " → " + str(payload.get("msg", ""))) + "\n")
        payload.setdefault("id", ids[0])
        return payload
    except subprocess.TimeoutExpired:
        log("[书架] 添加超时（110s）\n")
        return {"ok": False, "msg": "加书架超时了，检查一下网络或登录状态"}
    finally:
        _shelf_lock.release()


# ---------- 封面 ----------

def cover_path(book_id):
    return os.path.join(COVER_DIR, re.sub(r"[^0-9A-Za-z_-]", "_", book_id) + ".jpg")


def ensure_cover(book_id):
    """有 Key 时取官方封面并缓存到本地，返回本地路径；否则 None（前端回退到生成式封面）。"""
    p = cover_path(book_id)
    if os.path.isfile(p) and os.path.getsize(p) > 800:
        return p
    if not weread_key():
        return None
    try:
        info = weread_call("/book/info", {"bookId": book_id}, timeout=20)
        url = info.get("cover") or ""
        if not url:
            return None
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
        if len(raw) > 800:
            os.makedirs(COVER_DIR, exist_ok=True)
            with open(p, "wb") as f:
                f.write(raw)
            return p
    except Exception:
        return None
    return None


def warm_covers(limit=80):
    """后台把书架上还没有封面的书补齐（需要 Key）。不阻塞请求。"""
    def job():
        n = 0
        for b in list_books():
            if n >= limit:
                break
            if not b.get("cover"):
                if ensure_cover(b["id"]):
                    n += 1
        if n:
            log(f"--- 已补齐 {n} 张封面 ---")
    threading.Thread(target=job, daemon=True).start()


# ---------- 一键建立 MCP 的提示词 ----------

def mcp_prompt():
    """给 AI Agent 的接入提示词。

    刻意**不含任何个人信息**（不出现绝对路径、用户名、端口、项目目录名）：
    这份提示词是要发给别人或别人机器上用的，路径与服务地址都可能不同，
    所以只描述「做什么、找什么文件、怎么配」，具体位置让 Agent 自己确认。
    """
    return "\n".join([
        "请帮我接入一个本地 MCP 服务器，让我能用自然语言操作「归藏」——一个本地微信读书导出工具。",
        "",
        "归藏自带了一个零依赖的 Node 适配器，文件名是 guizang-mcp.mjs，",
        "就放在归藏自己的 mcp/ 目录里（和 ui_server.py 同级）。",
        "它用 stdio 说 JSON-RPC 2.0，会自己连上归藏的本机服务，不需要我额外配地址；",
        "服务没起时它还会自己拉起来。找不到这个文件就先告诉我，我确认一下位置。",
        "",
        "请在 MCP 配置里加一项（Qoder CN 写在设置文件的 mcpServers；",
        "ZCode 写在 config.json 的 mcp.servers）：",
        "",
        '  "guizang": {',
        '    "type": "stdio",',
        '    "command": "node",',
        '    "args": ["<归藏目录>/mcp/guizang-mcp.mjs"],',
        '    "timeout": 180000,',
        '    "env": { "GUIZANG_REPO": "<归藏目录>" }',
        '  }',
        "",
        "配好后重启一下让 MCP 生效（MCP 是启动时加载的，不像 skills 能热加载）。",
        "",
        "它提供 20 个工具，分四类：",
        "· 书架与状态：shelf_list、app_status、task_log、book_files、folder_create、book_move",
        "· 取书：book_fetch（开始）、task_stop（中止）、batch_fetch（排队多本）、account_connect（扫码登录）",
        "· 书与笔记：book_detail（简介/进度/划线数/是否已抓取）、search_books（本地+划线+书城三域）、",
        "  notes_index、notes_search、notes_random、book_mark（待读/在读/已读）、",
        "  shelf_add（把书城里搜到的书加进我自己的微信读书书架，唯一的写操作）",
        "· 导出：apkg_export（划线导成 Anki 卡包）、zip_export（多本打包）、cache_delete（删本地缓存，默认只列不删）",
        "",
        "两点注意：取书是分钟到小时级的长任务，book_fetch 会立即返回，",
        "进度要用 app_status / task_log 轮询；不要在这里等它跑完。",
        "删除类操作（cache_delete）要带 confirm=true 才真删，否则会先把要删的列出来。",
        "",
        "弄好后用 shelf_list 试一下，能读出书架就说明通了。",
    ])


def flomo_send(url, content, timeout=20):
    """把一条笔记发到用户自己的 flomo 记录 API（incoming webhook）。

    官方只公开了入口页（个人 webhook 地址形如 https://flomoapp.com/iwh/xxxx/），
    没有给请求示例。这里按通行约定发 JSON，失败再退回表单编码，
    并把 flomo 的原始回执带回去，方便用户当场看出问题在哪。
    """
    url = (url or "").strip()
    if not re.match(r"^https://flomoapp\.com/iwh/[\w-]+/?$", url):
        raise RuntimeError("flomo 地址看起来不对（应形如 https://flomoapp.com/iwh/xxxx/）")
    text = (content or "").strip()
    if not text:
        raise RuntimeError("内容为空")
    if len(text) > 5000:
        text = text[:5000]
    errs = []
    for ctype, body in (("application/json", json.dumps({"content": text}).encode("utf-8")),
                        ("application/x-www-form-urlencoded",
                         urllib.parse.urlencode({"content": text}).encode("utf-8"))):
        try:
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": ctype, "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
            try:
                d = json.loads(raw)
            except Exception:
                d = {}
            code = d.get("code", d.get("errcode"))
            if code in (0, None) and r.status < 400:
                return True, d.get("message") or "已发送"
            errs.append(f"{ctype}: {str(d.get('message') or raw)[:120]}")
        except Exception as e:
            errs.append(f"{ctype}: {str(e)[:120]}")
    raise RuntimeError("；".join(errs)[:280])


# ---------- 小 Agent ----------
#
# 界面右下角那颗气泡。这里不内置任何模型，只用用户自己填的「兼容 OpenAI 的接口」
# （/chat/completions 那一套）。后端只做三件事：存配置、拼上下文、把上游的流
# 原样转给页面。
#
# 上下文的来源分两半：书架与阅读状态在**前端**手上（它已经同步过一遍，后端再拉一次
# 就是白打一趟官方网关），本机的划线笔记在**后端**手上（索引文件就在磁盘上）。
# 所以前端把摘要捎过来，后端把它和最近的划线拼成一页纸，只在用户真的问话时才发出去。
#
# 界面上那句提示是承诺：不问就不发。所以这里没有「预热」「后台摘要」任何说法 ——
# 只有 agent_chat 被调到，才去拼、才去连。

AGENT_SYS = (
    "你是「归藏」里的读书小助手，只服务这一个用户。"
    "你的资料是他本机的微信读书数据：书架与读到哪、下载到本机的书的划线笔记。"
    "回答时：先给结论再给依据；引用划线要说明是哪本书；"
    "没把握就直说没把握，不要编造书里没写过的话。"
    "语气平实、简短，不用营销腔，不要堆 emoji。"
)


def agent_cfg():
    c = load_cfg()
    return ((c.get("agent_url") or "").strip(),
            (c.get("agent_key") or "").strip(),
            (c.get("agent_model") or "").strip())


def agent_state():
    """给页面的那份状态。Key 只回尾巴 —— 设置页靠它显示「已保存 ····abcd」，
    真正的 Key 永远不出这台机器。"""
    url, key, model = agent_cfg()
    return {"url": url, "model": model, "url_set": bool(url),
            "key_set": bool(key), "key_tail": key[-4:] if key else ""}


def agent_url_full(url):
    """把用户填的地址补成真正的 completions 端点。

    有人填 https://api.x.com（少了 /v1），有人填 …/v1（少了 /chat/completions），
    两种都按常见约定补齐；填全了的原样返回。
    """
    u = (url or "").strip().rstrip("/")
    if not re.match(r"^https?://", u):
        return ""
    if u.endswith("/chat/completions"):
        return u
    if re.search(r"/v\d+$", u):
        return u + "/chat/completions"
    return u + "/v1/chat/completions"


def set_agent_cfg(url, key, model):
    """存小 Agent 的三件套。返回 (ok, 人话)。

    Key 传空串表示「不改」—— 页面上那个输入框永远显示的是占位符，
    用户只改模型时不该被逼着把 Key 再贴一遍。
    """
    url = (url or "").strip()
    model = (model or "").strip()[:80]
    cfg = load_cfg()
    if not url:
        for k in ("agent_url", "agent_key", "agent_model"):
            cfg.pop(k, None)
        save_cfg(cfg)
        return True, "小 Agent 已关闭"
    full = agent_url_full(url)
    if not full:
        return False, "地址要写成 http:// 或 https:// 开头"
    if not model:
        return False, "还差一个模型名"
    cfg["agent_url"] = full
    cfg["agent_model"] = model
    if (key or "").strip():
        cfg["agent_key"] = key.strip()
    save_cfg(cfg)
    return True, "小 Agent 已连上 " + full


def local_context():
    """后端这一半的上下文：本机已取回的书 + 最近的一批划线。"""
    out = []
    books = list_books()
    if books:
        out.append("【下载到本机的书】共 %d 本" % len(books))
        for b in books[:40]:
            seg = "- 《%s》" % b["title"]
            if b.get("author"):
                seg += "（%s）" % b["author"]
            seg += "：已取 %d" % b["chapters"]
            if b.get("total"):
                seg += "/%d" % b["total"]
            seg += " 章"
            if b.get("done"):
                seg += "，已取全"
            if b.get("state"):
                seg += "，标记为「%s」" % b["state"]
            pr = b.get("progress") or {}
            if pr.get("chapters"):
                seg += "，进度到第 %s 章" % pr["chapters"]
            out.append(seg)
    idx = load_notes_index()
    items = sorted((idx.get("items") or []), key=lambda x: -(x.get("at") or 0))[:50]
    if items:
        out.append("")
        out.append("【最近的划线 / 想法】共索引 %d 条，这里给最新的 %d 条"
                   % (len(idx.get("items") or []), len(items)))
        for x in items:
            out.append("- 《%s》%s：%s" % (x.get("title") or "?", x.get("kind") or "划线",
                                          (x.get("text") or "").replace("\n", " ")[:160]))
    elif not idx.get("built_at"):
        out.append("")
        out.append("【划线笔记】用户还没在界面上建过笔记索引，所以这里没有划线可看。")
    return "\n".join(out)


def agent_payload(q, history, front_ctx):
    """拼成一次 /chat/completions 的请求体。

    front_ctx 是页面捎来的书架摘要（纯文本，前端已经压过一遍），只当资料，
    不当指令 —— 就算书名里写了「忽略上面的要求」，那也只是一本书的名字。
    """
    url, key, model = agent_cfg()
    msgs = [{"role": "system", "content": AGENT_SYS}]
    digest = []
    if (front_ctx or "").strip():
        digest.append("【书架与阅读状态（来自本机界面）】")
        digest.append(front_ctx.strip()[:6000])
    tail = local_context()
    if tail:
        digest.append("")
        digest.append(tail)
    if digest:
        msgs.append({"role": "system",
                     "content": "以下是这位用户本机的资料摘要，回答时以它为准：\n"
                                + "\n".join(digest)})
    for h in (history or [])[-8:]:
        r = (h or {}).get("role")
        c = ((h or {}).get("content") or "").strip()
        if r in ("user", "assistant") and c:
            msgs.append({"role": r, "content": c[:4000]})
    msgs.append({"role": "user", "content": (q or "").strip()[:4000]})
    return {"url": url, "key": key,
            "body": {"model": model, "messages": msgs, "stream": True}}


def agent_http_error(e):
    """上游报错时，把它自己的话带回去 —— 这类接口的提示往往比我们翻译得更准
    （是 Key 错了、模型名不对，还是余额用完了）。"""
    raw = e.read().decode("utf-8", "replace")
    try:
        m = (json.loads(raw).get("error") or {}).get("message")
    except Exception:
        m = None
    return RuntimeError("接口回了个 %s：%s" % (e.code, (m or raw)[:200]))


def agent_open(payload, stream=True, timeout=120):
    """连上游，把响应对象交出去。"""
    req = urllib.request.Request(
        payload["url"], data=json.dumps(payload["body"]).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Accept": "text/event-stream" if stream else "application/json",
                 "Authorization": "Bearer " + (payload["key"] or "-"),
                 "User-Agent": "guizang/1.0"},
        method="POST")
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise agent_http_error(e)
    except Exception as e:
        raise RuntimeError("连不上那个地址：%s" % str(e)[:160])


def agent_deltas(resp):
    """把上游的 SSE 拆成一段段正文。上游字段缺失/半行 JSON 都直接跳过 ——
    宁可少吐几个字，也不能因为一行脏数据把整段对话打断。"""
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            j = json.loads(data)
        except Exception:
            continue
        for ch in (j.get("choices") or []):
            piece = (ch.get("delta") or {}).get("content")
            if piece:
                yield piece


def agent_test():
    """「测一下」：发一句最小的话过去，走完整条链路（含 Key）。
    非流式，只为快 —— 用户要的是「通没通」，不是「说得多好」。"""
    url, key, model = agent_cfg()
    if not url or not model:
        return False, "还没填地址和模型", 0
    t0 = time.time()
    try:
        with agent_open({"url": url, "key": key,
                         "body": {"model": model, "stream": False,
                                  "messages": [{"role": "user",
                                                "content": "只回两个字：在的"}]}},
                        stream=False, timeout=45) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return False, str(e)[:200], 0
    ms = int((time.time() - t0) * 1000)
    try:
        say = (j.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    except Exception:
        say = ""
    return True, "通了，%d 毫秒。它说：%s" % (ms, (say or "（没说话）")[:60]), ms


def list_books():
    books = []
    lib = prune_lib(load_lib())
    if not os.path.isdir(OUT_DIR):
        return books
    for name in sorted(os.listdir(OUT_DIR)):
        d = os.path.join(OUT_DIR, name)
        if not os.path.isdir(d):
            continue
        ch_dir = os.path.join(d, "chapters")
        raw_dir = os.path.join(d, "raw")
        img_dir = os.path.join(d, "images")
        chapters = sorted(f for f in os.listdir(ch_dir) if f.endswith(".md")) \
            if os.path.isdir(ch_dir) else []
        images = [f for f in os.listdir(img_dir) if not f.startswith(".")] \
            if os.path.isdir(img_dir) else []
        chars = 0
        if os.path.isdir(raw_dir):
            for rf in sorted(os.listdir(raw_dir)):
                if rf.endswith(".json"):
                    try:
                        chars += int(json.load(open(os.path.join(raw_dir, rf),
                                                    encoding="utf-8")).get("text_len") or 0)
                    except Exception:
                        pass
        meta = {}
        mp = os.path.join(d, "meta.json")
        if os.path.exists(mp):
            try:
                meta = json.load(open(mp, encoding="utf-8"))
            except Exception:
                meta = {}
        # 导入的本地书没有 raw/，字数落在 meta.chars 上（取回来的书才有 raw/）
        if not chars:
            try:
                chars = int(meta.get("chars") or 0)
            except Exception:
                chars = 0
        # 目录总章数：拿来做「取回多少」的百分比分母
        total = 0
        cat = os.path.join(d, "_catalog.json")
        if os.path.exists(cat):
            try:
                total = len(json.load(open(cat, encoding="utf-8")))
            except Exception:
                total = 0
        # 导入的本地书没有「目录/已取」这层差别，总数＝现有章数
        if total < len(chapters):
            total = len(chapters)
        # 引擎实时进度（翻了多少页、还在不在跑）——进度条靠它才会在「一章内部」继续走
        progress = {}
        pp = os.path.join(d, "_progress.json")
        if os.path.exists(pp):
            try:
                progress = json.load(open(pp, encoding="utf-8"))
            except Exception:
                progress = {}
        books.append({
            "id": name,
            "title": meta.get("title") or name,
            "author": meta.get("author") or "",
            "chapters": len(chapters),
            "total": total,
            "images": len(images),
            "chars": chars,
            "updated_at": os.path.getmtime(d),
            "done": bool(meta.get("done")),
            # 来源：微信读书取回来的（默认）还是用户自己导进来的（local）。
            # 前端书架据此分组显示；local 的书没有原书链接，deep 置空。
            "source": meta.get("source") or "weread",
            "format": meta.get("format") or "",
            "progress": {k: progress.get(k) for k in
                         ("pages", "chapters", "session_chars", "running",
                          "budget_left", "stalled", "updated_at")
                         if progress.get(k) is not None},
            "folder": lib["assign"].get(name) or "",
            "state": (lib.get("state") or {}).get(name) or "",
            "deep": "" if meta.get("source") == "local"
                    else f"https://weread.qq.com/web/reader/{name}",
            # 前端据此决定要不要请求封面：没有就不请求，免得满屏 404
            "cover": os.path.isfile(cover_path(name)),
        })
    # 书架顺序：先在 order 里的按位置排，其余按最近更新排在后面
    pos = {bid: i for i, bid in enumerate(lib["order"])}
    books.sort(key=lambda b: (pos.get(b["id"], 10 ** 9), -b["updated_at"]))
    return books


_SHELF_MAP = {"at": 0.0, "m": {}}


def store_to_reader_map(max_age=600):
    """书架 bookId → 阅读器 id（deepLink 里的 v=）。

    详情页可能只拿得到 bookId（从划线搜索、书城进来），而本地目录名是阅读器 id，
    所以后端自己认一次，不依赖前端有没有同步过书架。缓存 10 分钟，失败就用旧值。
    """
    now = time.time()
    if _SHELF_MAP["m"] and now - _SHELF_MAP["at"] < max_age:
        return _SHELF_MAP["m"]
    try:
        d = weread_call("/shelf/sync", {}, timeout=25)
        m = {}
        for b in (d.get("books") or []):
            mm = re.search(r"[?&]v=([0-9A-Za-z]+)", str(b.get("deepLink") or ""))
            if b.get("bookId") and mm:
                m[b["bookId"]] = mm.group(1)
        if m:
            _SHELF_MAP["at"] = now
            _SHELF_MAP["m"] = m
    except Exception:
        pass
    return _SHELF_MAP["m"]


def merged_markdown(book_id):
    """把逐章 md 合并成单文件（图片相对路径 images/ 保持不变）。"""
    d = safe_book_dir(book_id)
    if not d:
        return None
    ch_dir = os.path.join(d, "chapters")
    files = sorted(f for f in os.listdir(ch_dir) if f.endswith(".md")) \
        if os.path.isdir(ch_dir) else []
    if not files:
        return None
    meta = {}
    mp = os.path.join(d, "meta.json")
    if os.path.exists(mp):
        try:
            meta = json.load(open(mp, encoding="utf-8"))
        except Exception:
            pass
    title = meta.get("title") or book_id
    author = meta.get("author") or ""
    buf = io.StringIO()
    buf.write(f"# {title}\n\n")
    if author:
        buf.write(f"**{author}**\n\n")
    buf.write("---\n\n")
    for fn in files:
        with open(os.path.join(ch_dir, fn), encoding="utf-8") as f:
            buf.write(f.read().rstrip("\n"))
        buf.write("\n\n---\n\n")
    return buf.getvalue()


def build_zip(book_id):
    md = merged_markdown(book_id)
    d = safe_book_dir(book_id)
    if md is None or d is None:
        return None
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    dest = os.path.join(DOWNLOAD_DIR, f"{book_id}.zip")
    meta = {}
    mp = os.path.join(d, "meta.json")
    if os.path.exists(mp):
        try:
            meta = json.load(open(mp, encoding="utf-8"))
        except Exception:
            pass
    safe = re.sub(r'[<>:"/\\|?*]', "_", meta.get("title") or book_id)
    img_dir = os.path.join(d, "images")
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{safe}.md", md)
        if os.path.isdir(img_dir):
            for f in sorted(os.listdir(img_dir)):
                if not f.startswith("."):
                    z.write(os.path.join(img_dir, f), f"images/{f}")
    return dest


def meta_title(book_id):
    d = safe_book_dir(book_id)
    if not d:
        return book_id
    mp = os.path.join(d, "meta.json")
    if os.path.exists(mp):
        try:
            return json.load(open(mp, encoding="utf-8")).get("title") or book_id
        except Exception:
            pass
    return book_id


def export_book(kind, book_id):
    """把一本书导成 EPUB / PDF，落在 DOWNLOAD_DIR 里，返回成品路径。

    kind = "epub" | "pdf"。没章节就返回 None（让路由给个准话）；真正导出时
    抛的异常不在这儿吞 —— 上层要把它原样回给用户，比笼统的「导出失败」有用。
    书名与作者跟 /api/md、/api/zip 一个规矩：都从 meta.json 取，取不到退回书 id。
    封面只在本地已经缓存下来时才嵌进 EPUB（ensure_cover 要联网，导出这条路
    不该被网络卡住；有就用，没有就排个文字封面，EPUB 允许）。
    """
    d = safe_book_dir(book_id)
    if not d:
        return None
    ch_dir = os.path.join(d, "chapters")
    if not (os.path.isdir(ch_dir)
            and any(f.endswith(".md") for f in os.listdir(ch_dir))):
        return None
    meta = {}
    mp = os.path.join(d, "meta.json")
    if os.path.exists(mp):
        try:
            meta = json.load(open(mp, encoding="utf-8"))
        except Exception:
            pass
    title = meta.get("title") or book_id
    author = meta.get("author") or ""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    dest = os.path.join(DOWNLOAD_DIR, f"{book_id}.{kind}")
    if kind == "epub":
        cp = cover_path(book_id)
        return book_export.build_epub(d, dest, title=title, author=author,
                                      cover=cp if os.path.isfile(cp) else None)
    if kind == "pdf":
        return book_export.build_pdf(d, dest, title=title, author=author)
    return None


def content_disp(name, ext):
    """HTTP 头只能放 latin-1，中文文件名必须走 RFC 5987 的 filename*。"""
    from urllib.parse import quote
    ascii_name = re.sub(r"[^A-Za-z0-9_.\-]", "_", name) or "book"
    return ("attachment; filename=\"%s.%s\"; filename*=UTF-8''%s"
            % (ascii_name, ext, quote(f"{name}.{ext}")))


# ---------- 页面内读正文（目录 / 单章 / 插图 / 随包静态资源） ----------
# 这几条只读、只认白名单：正文是抓回来的外部内容，按数据对待，绝不拿它拼路径。
_VENDOR_MIME = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf",
    ".svg": "image/svg+xml", ".png": "image/png",
}
_IMG_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
             ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
             ".svg": "image/svg+xml"}


def vendor_file(name):
    """随包分发的静态资源（如 vendor/markdown-it.min.js）。只认单层文件名与已知扩展名。"""
    if not name or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return None
    ext = os.path.splitext(name)[1].lower()
    if ext not in _VENDOR_MIME:
        return None
    p = os.path.realpath(os.path.join(VENDOR_DIR, name))
    if not p.startswith(os.path.realpath(VENDOR_DIR) + os.sep) or not os.path.isfile(p):
        return None
    return p, _VENDOR_MIME[ext]


def book_outline(book_id):
    """一本书的阅读目录：逐章文件 + 标题 + 字数。

    标题以「该章正文的第一行」为准 —— 引擎落盘时总会把章名写成开头那一行
    `# 章名`，它和这一章的内容是一起写下来的，永远对得住。原来优先用
    `_catalog.json`（网页目录）按位置取，但那份目录是网页 DOM 顺序（含版权页、
    内容提要这类没落成文件的条目，也有重复嵌套项），跟实际落盘的文件根本不是
    一一对应：实测九本书，偏移量从 0 到 21 不等，照位置取会让整本目录全错位
    （「版权信息」显示成第一章）。所以改成：先读文件首行，读不出才退回目录。
    """
    d = safe_book_dir(book_id)
    if not d:
        return None
    ch_dir = os.path.join(d, "chapters")
    if not os.path.isdir(ch_dir):
        return None
    files = sorted(f for f in os.listdir(ch_dir) if f.endswith(".md"))
    if not files:
        return None
    meta = {}
    mp = os.path.join(d, "meta.json")
    if os.path.exists(mp):
        try:
            meta = json.load(open(mp, encoding="utf-8"))
        except Exception:
            meta = {}
    catalog = []
    cp = os.path.join(d, "_catalog.json")
    if os.path.exists(cp):
        try:
            catalog = json.load(open(cp, encoding="utf-8"))
        except Exception:
            catalog = []
    chapters = []
    for i, fn in enumerate(files):
        try:
            text = open(os.path.join(ch_dir, fn), encoding="utf-8").read()
        except Exception:
            text = ""
        # 第一行非空内容就是章名（引擎写盘时带 # 前缀）
        title = ""
        for line in text.splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                title = line[:60]
                break
        # 首行读不出来（空文件等）才退回目录，聊胜于无，但不再当主力
        if not title and isinstance(catalog, list) and i < len(catalog) and catalog[i]:
            title = str(catalog[i]).strip()
        chapters.append({"i": i + 1, "file": fn, "title": title or f"第 {i + 1} 章",
                         "chars": len(text)})
    img_dir = os.path.join(d, "images")
    images = len([f for f in os.listdir(img_dir) if not f.startswith(".")]) \
        if os.path.isdir(img_dir) else 0
    return {"id": book_id,
            "title": meta.get("title") or book_id,
            "author": meta.get("author") or "",
            "done": bool(meta.get("done")),
            "chapters": chapters,
            "images": images}


def chapter_text(book_id, ch):
    """单章正文（原样返回 markdown，渲染交给前端）。"""
    d = safe_book_dir(book_id)
    if not d or not ch or not re.fullmatch(r"[A-Za-z0-9._-]+\.md", ch):
        return None
    ch_dir = os.path.realpath(os.path.join(d, "chapters"))
    p = os.path.realpath(os.path.join(ch_dir, ch))
    if not p.startswith(ch_dir + os.sep) or not os.path.isfile(p):
        return None
    try:
        return open(p, encoding="utf-8").read()
    except Exception:
        return None


def book_image(book_id, name):
    """章内插图：output/<id>/images/<name> 白名单读取。文件名是抓取时定的，不含路径。"""
    d = safe_book_dir(book_id)
    if not d or not name or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return None
    ext = os.path.splitext(name)[1].lower()
    if ext not in _IMG_MIME:
        return None
    root = os.path.realpath(os.path.join(d, "images"))
    p = os.path.realpath(os.path.join(root, name))
    if not p.startswith(root + os.sep) or not os.path.isfile(p):
        return None
    return p, _IMG_MIME[ext]



def write_meta(book_id, title, author, done):
    d = safe_book_dir(book_id)
    if not d:
        return
    mp = os.path.join(d, "meta.json")
    cur = {}
    if os.path.exists(mp):
        try:
            cur = json.load(open(mp, encoding="utf-8"))
        except Exception:
            cur = {}
    cur.update({"title": title or cur.get("title") or book_id,
                "author": author or cur.get("author") or "",
                "done": bool(done), "updated_at": time.time()})
    with open(mp, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)


# ---------- task runner ----------

def _reader(proc):
    title, author = None, None
    done = False
    for raw in iter(proc.stdout.readline, ""):
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        m = re.search(r"📖\s*(.+?)\s*—\s*(.*)$", line)
        # 「未知」是引擎读不到书名时的占位，不能拿它覆盖 meta 里已有的真书名。
        if m and not title and m.group(1).strip() != "未知":
            title = m.group(1).strip()
            author = m.group(2).strip()
        if "全书导出完成" in line:
            done = True
        log(line)
    proc.wait()
    with _lock:
        TASK["exit_code"] = proc.returncode
        TASK["running"] = False
        book = TASK["book"]
    if TASK.get("kind") in ("export",) and book:
        write_meta(book, title, author, done)
    log(f"--- 任务结束（退出码 {proc.returncode}）---")


def start_task(kind, argv, book=None, env_extra=None):
    with _lock:
        if TASK["running"]:
            return False, "已有任务在运行"
        TASK.update({"running": True, "kind": kind, "book": book,
                     "started_at": time.time(), "exit_code": None})
    log(f"--- 启动 {kind}" + (f" · {book}" if book else "") + " ---")
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    # 引擎里 output/ 是相对路径，这里明确指到书库，保证「写进去的」和
    # 「书架读的」是同一个地方（不然装成 app 后引擎会写到源码目录旁边）。
    env["GUIZANG_OUTPUT"] = OUT_DIR
    if env_extra:
        env.update(env_extra)
    try:
        proc = subprocess.Popen(
            argv, cwd=DATA_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, errors="replace",
            env=env, **pc.spawn_kwargs())
    except Exception as e:
        # 起不来就把状态回滚。原来这里直接抛异常：请求线程带着「running=True」一起死掉，
        # 于是第一次点击毫无反应（连接被掐断），之后每次点击都只回「已有任务在运行」。
        with _lock:
            TASK.update({"running": False, "proc": None, "exit_code": -1})
        log(f"--- 起不来（{kind}）：{type(e).__name__}: {e} ---")
        return False, f"起不来：{str(e)[:160]}"
    with _lock:
        TASK["proc"] = proc
    threading.Thread(target=_reader, args=(proc,), daemon=True).start()
    return True, "已启动"


def _finish_stop(proc, grace=60):
    """后台等引擎自己收尾；超过 grace 秒还没退，才连浏览器一起强制结束。"""
    t0 = time.time()
    while proc.poll() is None and time.time() - t0 < grace:
        time.sleep(0.5)
    if proc.poll() is None:
        # POSIX 杀进程组；Windows 用 taskkill /T，把浏览器子进程一起收掉
        pc.hard_kill(proc)
        log("--- 收尾超时，已强制结束（之前落盘的章节都在）---")


def stop_task():
    with _lock:
        proc = TASK.get("proc")
        running = TASK["running"]
    if not running or proc is None:
        return False, "当前没有运行中的任务"
    # 只给引擎进程发停止信号，不连浏览器一起杀：引擎收到后会把手上正在攒的那一章
    # 落盘再退（export_precise 里的协作式停止）。以前的写法是 killpg 整组 + 5 秒就
    # SIGKILL，浏览器被一起打死，几万字在手的章节直接蒸发 —— 用户看到的「中止=白抓」。
    # Windows 没有可捕获的 SIGTERM，改发 CTRL_BREAK；发不出去才会退化为硬杀。
    pc.send_stop(proc)
    for _ in range(40):
        if proc.poll() is not None:
            log("--- 已停止（已抓到的章节都在盘上）---")
            return True, "已停止"
        time.sleep(0.25)
    threading.Thread(target=_finish_stop, args=(proc,), daemon=True).start()
    log("--- 已请求停止，正在把在手的内容落盘 ---")
    return True, "正在收尾（把手上这一章落盘就退），已导出的内容不会丢"


# ---------- http ----------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, ctype, body, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        extra = extra or {}
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 绝大多数响应是「刚算出来的」，必须 no-store；但插图这类可以长缓存的
        # 由调用方通过 extra 显式指定，这里就不再叠一条默认值（重复头会被浏览器忽略）。
        self.send_header("Cache-Control", extra.get("Cache-Control") or "no-store")
        for k, v in extra.items():
            if k == "Cache-Control":
                continue
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False))

    # 小 Agent 的回复是一段段吐出来的（上游 SSE），所以这条响应没法先算长度。
    # 走 chunked：每段正文一个 JSON 行，最后来一个收尾块。写完主动断连 ——
    # 本机自己的页面，重连一次的开销远小于把 chunked 的半开连接留在那儿的风险。
    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _chunk(self, text):
        b = text.encode("utf-8")
        self.wfile.write(b"%x\r\n" % len(b) + b + b"\r\n")
        self.wfile.flush()

    def _chunk_end(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path

        if path in ("/", "/index.html"):
            try:
                with open(UI_PATH, encoding="utf-8") as f:
                    return self._send(200, "text/html; charset=utf-8", f.read())
            except FileNotFoundError:
                return self._send(500, "text/plain; charset=utf-8", "ui.html 缺失")

        if path.startswith("/vendor/"):
            hit = vendor_file(unquote(path[len("/vendor/"):]))
            if not hit:
                return self._send(404, "text/plain; charset=utf-8", "not found")
            fp, mime = hit
            with open(fp, "rb") as f:
                return self._send(200, mime, f.read(),
                                  {"Cache-Control": "public, max-age=86400"})

        if path == "/api/state":
            with _lock:
                task = {k: v for k, v in TASK.items() if k != "proc"}
            lib = load_lib()
            return self._json({
                "chromium": chromium_ready(),
                "login": read_login_state(),
                "task": task,
                "books": list_books(),
                "folders": lib["folders"],
                # 状态标签可能打在还没抓取的书上，所以整张表也给前端
                "states": lib.get("state") or {},
                "repo": REPO,
                "out": OUT_DIR,
                "version": VERSION,
                "weread": {"key_set": bool(weread_key()),
                           "key_tail": weread_key()[-4:] if weread_key() else ""},
                "flomo": {"url_set": bool(load_cfg().get("flomo_url")),
                          "tail": (load_cfg().get("flomo_url") or "")[-8:]},
                "wallpaper": {"set": os.path.isfile(WALL_PATH),
                              "v": int(os.path.getmtime(WALL_PATH)) if os.path.isfile(WALL_PATH) else 0},
                "export": {"dir": export_dir()},
                "agent": agent_state(),
            })

        if path == "/api/log":
            with _lock:
                base = LOG_BASE
                total = len(LOG)
                tail = q.get("tail", [None])[0]
                if tail is not None:
                    k = max(0, min(int(tail), total))
                    lines = LOG[total - k:] if k else []
                else:
                    since = int(q.get("since", ["0"])[0])
                    lines = LOG[max(0, since - base):]
                nxt = base + total
            return self._json({"base": base, "lines": lines, "next": nxt})

        if path == "/api/cover":
            book = q.get("book", [""])[0]
            if not re.fullmatch(r"[0-9A-Za-z_-]{6,}", book or ""):
                return self._send(404, "text/plain; charset=utf-8", "bad id")
            p = ensure_cover(book)
            if not p:
                return self._send(404, "text/plain; charset=utf-8", "no cover")
            with open(p, "rb") as f:
                return self._send(200, "image/jpeg", f.read())

        if path == "/api/wallpaper":
            if not os.path.isfile(WALL_PATH):
                return self._send(404, "text/plain; charset=utf-8", "no wallpaper")
            mime = load_cfg().get("wall_mime") or "image/jpeg"
            with open(WALL_PATH, "rb") as f:
                return self._send(200, mime, f.read())

        if path == "/api/mcp":
            return self._json({"prompt": mcp_prompt(),
                               "adapter": os.path.join(REPO, "mcp", "guizang-mcp.mjs")})

        if path == "/api/notes_index":
            d = load_notes_index()
            st = dict(NOTES_STATE)
            st["built_at"] = d.get("built_at") or st.get("built_at") or 0
            # 重建期间依然报旧索引的条数：否则搜索/随机漫步会以为"没索引"而用不了
            st["count"] = max(len(d.get("items") or []), st.get("count") or 0)
            return self._json({"ok": True, "data": st,
                               "books": d.get("books") or []})

        if path == "/api/notes_search":
            kw = (q.get("q", [""])[0] or "").strip()
            lim = int(q.get("limit", ["80"])[0])
            if not kw:
                return self._json({"ok": True, "data": [], "total": 0})
            low = kw.lower()
            items = load_notes_index().get("items") or []
            hit = [x for x in items
                   if low in (x.get("text") or "").lower()
                   or low in (x.get("title") or "").lower()]
            return self._json({"ok": True, "data": hit[:lim], "total": len(hit)})

        if path == "/api/notes_random":
            n = max(1, min(int(q.get("n", ["6"])[0]), 30))
            items = load_notes_index().get("items") or []
            if not items:
                return self._json({"ok": True, "data": []})
            return self._json({"ok": True, "data": random.sample(items, min(n, len(items)))})

        if path == "/api/notes_of":
            bid = q.get("book", [""])[0]
            # 索引里没有就现拉这一本，两套 id 都认（否则详情页会显示「没有划线」）
            items, _resolved, _src = notes_for_book(bid)
            return self._json({"ok": True, "data": items})

        if path == "/api/detail":
            bid = q.get("book", [""])[0]
            rid = q.get("reader", [""])[0]
            out = {"local": None}
            # 只给了 bookId 时，自己认一下阅读器 id，本地抓取状态才不会显示成「尚未抓取」
            if bid and (not rid or not safe_book_dir(rid)):
                rid = store_to_reader_map().get(bid) or rid
            if rid:
                out["reader"] = rid
            if rid and safe_book_dir(rid):
                out["local"] = next((x for x in list_books() if x["id"] == rid), None)
            # 反过来：只有阅读器 id 时，查出 bookId，简介/进度才读得到
            if not bid and rid:
                bid = next((k for k, v in store_to_reader_map().items() if v == rid), "")
                if bid:
                    out["bookId"] = bid
            if bid:
                try:
                    out["info"] = weread_call("/book/info", {"bookId": bid}, timeout=20)
                except Exception as e:
                    out["info_err"] = str(e)[:120]
                try:
                    out["progress"] = weread_call("/book/getprogress", {"bookId": bid},
                                                  timeout=20)
                except Exception as e:
                    out["progress_err"] = str(e)[:120]
                try:
                    # 笔记数：能找到就带上；找不到不算错（这本可能没笔记）
                    hit, last = None, None
                    for _ in range(4):              # 最多翻 4 页，别无限翻
                        params2 = {"count": 100}
                        if last:
                            params2["lastSort"] = last
                        nb = weread_call("/user/notebooks", params2, timeout=25)
                        page = nb.get("books") or []
                        hit = next((x for x in page if x.get("bookId") == bid), None)
                        if hit or not nb.get("hasMore") or not page:
                            break
                        last = page[-1].get("sort")
                    if hit:
                        out["notes"] = {"noteCount": hit.get("noteCount") or 0,
                                        "reviewCount": hit.get("reviewCount") or 0,
                                        "bookmarkCount": hit.get("bookmarkCount") or 0,
                                        "readingProgress": hit.get("readingProgress"),
                                        "markedStatus": hit.get("markedStatus")}
                except Exception as e:
                    out["notes_err"] = str(e)[:120]
            return self._json({"ok": True, "data": out})

        if path == "/api/apkg":
            bid = q.get("book", [""])[0]
            title = q.get("title", [bid])[0]
            try:
                dest, n = build_apkg(bid, title)
            except Exception as e:
                return self._send(400, "text/plain; charset=utf-8", str(e)[:200])
            # json=1：只回路径不下文件，给 MCP / 脚本用（同一台机器，路径可直接打开）
            if q.get("json", [""])[0] in ("1", "true"):
                return self._json({"ok": True, "path": dest, "count": n,
                                   "size": os.path.getsize(dest)})
            data = open(dest, "rb").read()
            return self._send(200, "application/octet-stream", data,
                              {"Content-Disposition": content_disp(title or bid, "apkg")})

        if path == "/api/zip-multi":
            ids = [x for x in (q.get("books", [""])[0] or "").split(",") if x]
            if not ids:
                return self._send(400, "text/plain; charset=utf-8", "没选书")
            dest, n = build_zip_multi(ids)
            if not n:
                return self._send(404, "text/plain; charset=utf-8", "选中的书都没有可打包的内容")
            if q.get("json", [""])[0] in ("1", "true"):
                return self._json({"ok": True, "path": dest, "count": n,
                                   "size": os.path.getsize(dest)})
            data = open(dest, "rb").read()
            return self._send(200, "application/zip", data,
                              {"Content-Disposition": content_disp(f"归藏-{n}本", "zip")})

        if path == "/api/read":
            book = q.get("book", [""])[0]
            ch = q.get("ch", [""])[0]
            if ch:                       # 取某一章正文
                md = chapter_text(book, ch)
                if md is None:
                    return self._send(404, "text/plain; charset=utf-8", "没有这一章")
                return self._json({"ok": True, "book": book, "file": ch, "md": md})
            out = book_outline(book)     # 不给 ch 就是阅读目录
            if out is None:
                return self._send(404, "text/plain; charset=utf-8", "这本书还没有可读的章节")
            return self._json({"ok": True, "data": out})

        if path == "/api/img":
            hit = book_image(q.get("book", [""])[0], q.get("name", [""])[0])
            if not hit:
                return self._send(404, "text/plain; charset=utf-8", "no image")
            fp, mime = hit
            with open(fp, "rb") as f:
                return self._send(200, mime, f.read(),
                                  {"Cache-Control": "public, max-age=604800"})

        if path == "/api/md":
            book = q.get("book", [""])[0]
            md = merged_markdown(book)
            if md is None:
                return self._send(404, "text/plain; charset=utf-8", "没有章节内容")
            # 没点名就用书名（跟 /api/zip 一个规矩）。原来退回书 id，下载下来是
            # 一串 00a32ec0… 的目录名，用户根本认不出是哪本书。
            fn = (q.get("name", [""])[0] or meta_title(book))
            return self._send(200, "text/markdown; charset=utf-8", md,
                              {"Content-Disposition": content_disp(fn, "md")})

        if path == "/api/zip":
            book = q.get("book", [""])[0]
            dest = build_zip(book)
            if not dest:
                return self._send(404, "text/plain; charset=utf-8", "没有可打包的内容")
            size = os.path.getsize(dest)
            if q.get("json", [""])[0] in ("1", "true"):
                return self._json({"ok": True, "path": dest, "size": size,
                                   "title": meta_title(book)})
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", content_disp(meta_title(book), "zip"))
            self.end_headers()
            with open(dest, "rb") as f:
                shutil.copyfileobj(f, self.wfile)
            return

        if path in ("/api/epub", "/api/pdf"):
            # 跟 /api/zip 一条路子：先落盘再决定是回 JSON 还是直接下流。
            # PDF 走铬内核打印，没装好先拦一道，别让用户点了半分钟才报错。
            kind = path.rsplit("/", 1)[1]
            book = q.get("book", [""])[0]
            if kind == "pdf" and not chromium_ready():
                return self._send(400, "text/plain; charset=utf-8",
                                  "导出 PDF 要用到内置的铬内核，它还没装好")
            try:
                dest = export_book(kind, book)
            except Exception as e:
                return self._send(500, "text/plain; charset=utf-8",
                                  f"导出没做成：{e}")
            if not dest:
                return self._send(404, "text/plain; charset=utf-8",
                                  "这本书还没有可导出的章节")
            size = os.path.getsize(dest)
            if q.get("json", [""])[0] in ("1", "true"):
                return self._json({"ok": True, "path": dest, "size": size,
                                   "title": meta_title(book), "kind": kind})
            mime = "application/epub+zip" if kind == "epub" else "application/pdf"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", content_disp(meta_title(book), kind))
            self.end_headers()
            with open(dest, "rb") as f:
                shutil.copyfileobj(f, self.wfile)
            return

        return self._send(404, "text/plain; charset=utf-8", "not found")

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        # 只收 JSON：挡掉跨站表单那种「顺手提交」（本机服务不该被网页悄悄调用）
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in ("application/json", ""):
            self.rfile.read(n)
            return self._send(415, "text/plain; charset=utf-8", "只接受 application/json")
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}

        if u.path == "/api/notes_index":
            rebuild = bool((body or {}).get("rebuild"))
            if NOTES_STATE["running"]:
                return self._json({"ok": True, "msg": "索引正在建立中", "running": True})
            if rebuild:
                # 整本重建前先留一份上次的：中途失败也不至于把已有划线弄丢
                try:
                    if os.path.isfile(NOTES_IDX):
                        shutil.copy2(NOTES_IDX, NOTES_IDX + ".prev")
                except Exception:
                    pass
                threading.Thread(target=build_notes_index, daemon=True).start()
                return self._json({"ok": True, "msg": "开始重建笔记索引"})
            if not load_notes_index().get("built_at"):
                threading.Thread(target=build_notes_index, daemon=True).start()
                return self._json({"ok": True, "msg": "开始建立笔记索引"})
            return self._json({"ok": True, "msg": "索引已是最新",
                               "count": len(load_notes_index().get("items") or [])})

        if u.path == "/api/import":
            # 把用户手里的书收进书库：前端读成 base64 再 POST 过来（不经过 multipart，
            # 保持「只认 application/json」这条统一的入口约定）。
            raw = (body or {}).get("data") or ""
            name = str((body or {}).get("name") or "").strip()
            if not raw:
                return self._json({"ok": False, "msg": "没有收到文件内容"}, 400)
            try:
                blob = base64.b64decode(raw, validate=False)
            except Exception:
                return self._json({"ok": False, "msg": "文件内容解码失败"}, 400)
            try:
                info = book_import.import_book(
                    OUT_DIR, name, blob,
                    title=str((body or {}).get("title") or "").strip(),
                    author=str((body or {}).get("author") or "").strip(),
                    cover_dir=COVER_DIR,
                )
            except ValueError as e:
                return self._json({"ok": False, "msg": str(e)}, 400)
            except Exception as e:
                traceback.print_exc()
                return self._json({"ok": False, "msg": "导入失败：%s" % (str(e)[:160])}, 500)
            log(f"导入本地书：{info['title']}（{info['label']}，{info['chapters']} 章）")
            return self._json({"ok": True, "book": info,
                               "msg": f"已导入《{info['title']}》，共 {info['chapters']} 章"})

        if u.path == "/api/book_state":
            ids = (body or {}).get("books") or []
            st = ((body or {}).get("state") or "").strip()[:8]
            lib = load_lib()
            lib.setdefault("state", {})
            n = 0
            for b in ids:
                # 状态只是 library.json 里的标签，不要求本地已抓取（书架上的书也能标）
                if not re.fullmatch(r"[A-Za-z0-9_\-]{4,}", str(b or "")):
                    continue
                if st:
                    lib["state"][b] = st
                else:
                    lib["state"].pop(b, None)
                n += 1
            save_lib(lib)
            skipped = len(ids) - n
            msg = f"已更新 {n} 本" + (f"（{skipped} 本 id 不合法，已跳过）" if skipped > 0 else "")
            return self._json({"ok": True, "msg": msg})

        if u.path == "/api/delete_many":
            ids = (body or {}).get("books") or []
            n = 0
            lib = load_lib()
            for b in ids:
                d = safe_book_dir(b)
                if not d:
                    continue
                title = meta_title(b)
                shutil.rmtree(d)
                stray = os.path.join(OUT_DIR, re.sub(r'[<>:"/\\|?*]', "_", title) + ".md")
                if (os.path.isfile(stray)
                        and os.path.realpath(stray).startswith(os.path.realpath(OUT_DIR) + os.sep)):
                    try:
                        os.remove(stray)
                    except OSError:
                        pass
                lib["assign"].pop(b, None)
                lib["order"] = [x for x in lib["order"] if x != b]
                (lib.get("state") or {}).pop(b, None)
                n += 1
            save_lib(lib)
            return self._json({"ok": True, "msg": f"已删除 {n} 本的本地缓存"})

        if u.path == "/api/agent/chat":
            q = ((body or {}).get("q") or "").strip()
            if not q:
                return self._json({"ok": False, "msg": "说点什么吧"})
            url, key, model = agent_cfg()
            if not url or not model:
                return self._json({"ok": False, "msg": "小 Agent 还没配：去设置 → 小 Agent 里填个地址和模型"})
            payload = agent_payload(q, (body or {}).get("history"), (body or {}).get("ctx"))
            # 从这一行起就转成流式了，之后没法再改 HTTP 状态码 —— 出错只能当作
            # 流里的一条消息发出去，所以先把「是不是配好了」这类问题在前头挡掉。
            self._sse_start()
            try:
                with agent_open(payload) as r:
                    for piece in agent_deltas(r):
                        self._chunk(json.dumps({"d": piece}, ensure_ascii=False) + "\n")
                self._chunk('{"done":true}\n')
            except Exception as e:
                log(f"--- 小 Agent 出错：{str(e)[:200]} ---")
                self._chunk(json.dumps({"err": str(e)[:300]}, ensure_ascii=False) + "\n")
            self._chunk_end()
            return

        if u.path == "/api/action":
            return self._action(body)
        if u.path == "/api/weread":
            api_name = (body or {}).get("api_name") or ""
            params = (body or {}).get("params") or {}
            try:
                return self._json({"ok": True, "data": weread_call(api_name, params)})
            except Exception as e:
                return self._json({"ok": False, "msg": str(e)[:300]})
        if u.path == "/api/config":
            key = ((body or {}).get("weread_key") or "").strip()
            flomo = ((body or {}).get("flomo_url") or "").strip()
            cfg = load_cfg()
            if flomo is not None and ("flomo_url" in (body or {})):
                if flomo and not re.match(r"^https://flomoapp\.com/iwh/[\w-]+/?$", flomo):
                    return self._json({"ok": False, "msg": "flomo 地址看起来不对（应形如 https://flomoapp.com/iwh/xxxx/）"})
                if flomo:
                    cfg["flomo_url"] = flomo
                else:
                    cfg.pop("flomo_url", None)
                save_cfg(cfg)
                return self._json({"ok": True, "msg": "已保存 flomo 地址" if flomo else "已清除 flomo 地址"})
            if key:
                if not re.match(r"^wrk-[0-9A-Za-z_-]{6,}$", key):
                    return self._json({"ok": False, "msg": "看起来不是有效的 Key（应以 wrk- 开头）"})
                cfg["weread_key"] = key
                save_cfg(cfg)
                try:
                    shelf = weread_call("/shelf/sync", {}, timeout=25)
                    n = len(shelf.get("books") or []) + len(shelf.get("albums") or [])
                    warm_covers()
                    return self._json({"ok": True, "msg": f"已保存并连通，书架 {n} 本"})
                except Exception as e:
                    return self._json({"ok": False, "msg": f"已保存，但验证失败：{str(e)[:160]}"})
            cfg.pop("weread_key", None)
            save_cfg(cfg)
            return self._json({"ok": True, "msg": "已清除 Key"})
        if u.path == "/api/wallpaper":
            if (body or {}).get("clear"):
                clear_wallpaper()
                return self._json({"ok": True, "msg": "已恢复默认背景"})
            try:
                n = save_wallpaper((body or {}).get("data") or "")
                log("--- 壁纸已更新 ---")
                return self._json({"ok": True, "msg": f"壁纸已保存（{round(n/1024)} KB）"})
            except Exception as e:
                return self._json({"ok": False, "msg": str(e)[:160]})
        if u.path == "/api/flomo":
            items = (body or {}).get("items") or []
            if not items:
                return self._json({"ok": False, "msg": "没有选中任何内容"})
            url = (body or {}).get("url") or load_cfg().get("flomo_url") or ""
            okn, fails = 0, []
            for it in items[:200]:
                try:
                    flomo_send(url, it)
                    okn += 1
                    time.sleep(0.25)      # 别把 flomo 打急了
                except Exception as e:
                    fails.append(str(e)[:80])
                    if len(fails) >= 3:
                        break
            if okn and not fails:
                return self._json({"ok": True, "msg": f"已导入 {okn} 条"})
            if okn:
                return self._json({"ok": True, "msg": f"导入 {okn} 条，另有 {len(fails)} 条失败：{fails[0]}"})
            return self._json({"ok": False, "msg": f"都没成功：{fails[0] if fails else '未知原因'}"})
        return self._send(404, "text/plain; charset=utf-8", "not found")

    def _action(self, body):
        """所有手写操作的统一入口 —— 兜底必须在这里。

        原来这里没有 try：任何一个操作抛异常（最典型的是起子进程失败），异常会穿过
        do_POST 逃出去，连接被直接掐断；前端拿到的是 RemoteDisconnected，
        `await post(...)` 立刻 reject，而 onclick 里没有 catch，页面于是**毫无反应**。
        这条链路是实测过的（起一个必定抛异常的 do_POST，客户端收到
        「Remote end closed connection without response」），所以宁可多一层：
        让「没做成」永远是一句人话 + 几行可追的日志。
        """
        try:
            return self._dispatch(body)
        except Exception as e:
            log(f"--- 操作失败（{(body or {}).get('action')}）：{type(e).__name__}: {e} ---")
            for line in traceback.format_exc().strip().splitlines()[-4:]:
                log("    " + line)
            return self._json({"ok": False,
                               "msg": f"这个操作没做成：{type(e).__name__}: {str(e)[:140]}"})

    def _dispatch(self, body):
        action = (body or {}).get("action")
        book = (body or {}).get("book", "")

        if action == "stop":
            ok, msg = stop_task()
            return self._json({"ok": ok, "msg": msg})

        if action == "log.clear":
            return self._json(clear_log())

        if action == "export_dir":
            ok, msg = set_export_dir(body.get("dir"))
            return self._json({"ok": ok, "msg": msg, "dir": export_dir()})

        if action == "agent.save":
            ok, msg = set_agent_cfg(body.get("url"), body.get("key"), body.get("model"))
            return self._json({"ok": ok, "msg": msg, "agent": agent_state()})

        if action == "agent.test":
            ok, msg, _ms = agent_test()
            return self._json({"ok": ok, "msg": msg})

        if action == "install":
            # 只有这一步需要把系统代理翻成环境变量：playwright 的下载器是 Node，
            # 不读系统代理设置，只认 HTTP_PROXY / HTTPS_PROXY。
            # 取书任务不能带这个 —— 那是浏览器的流量，走代理反而可能把
            # weread.qq.com 绕远甚至绕坏，浏览器有自己的代理配置。
            notes = []
            env_extra = pc.proxy_env(note=notes)
            for n in notes:
                log(f"      · {n}")
            ok, msg = start_task("install", [py(), "-m", "playwright", "install", "chromium"],
                                 env_extra=env_extra)
            return self._json({"ok": ok, "msg": msg})

        if action == "login":
            ok, msg = start_task("login", [py(), script("login.py")])
            return self._json({"ok": ok, "msg": msg})

        if action == "export":
            book_id = extract_book_id(book)
            if not book_id:
                return self._json({"ok": False, "msg": "没能从里面认出书籍编号，看看是不是粘错了"})
            ok, msg = start_task("export", [py(), script("export_precise.py"), book_id], book=book_id)
            return self._json({"ok": ok, "msg": msg, "id": book_id})

        if action == "images":
            book_id = (book or "").strip()
            if not safe_book_dir(book_id):
                return self._json({"ok": False, "msg": "找不到这本书的导出目录"})
            ok, msg = start_task("images", [py(), script("download_images.py"), book_id], book=book_id)
            return self._json({"ok": ok, "msg": msg})

        if action == "reveal":
            d = safe_book_dir(book)
            if not d:
                return self._json({"ok": False, "msg": "目录不存在"})
            pc.open_in_file_manager(d)
            return self._json({"ok": True, "msg": "已在文件管理器打开"})

        # 打开整个书库根目录：本地书库那一屏用，和「定位某本书」区分开
        if action == "openbooks":
            ensure_books_dir()
            pc.open_in_file_manager(OUT_DIR)
            return self._json({"ok": True, "msg": "已打开书库文件夹"})

        if action == "folder.new":
            name = (body.get("name") or "").strip()[:40]
            if not name:
                return self._json({"ok": False, "msg": "文件夹需要一个名字"})
            lib = load_lib()
            fid = "f" + uuid.uuid4().hex[:8]
            lib["folders"].append({"id": fid, "name": name})
            save_lib(lib)
            return self._json({"ok": True, "id": fid, "msg": "已新建"})

        if action == "folder.rename":
            fid = (body.get("id") or "").strip()
            name = (body.get("name") or "").strip()[:40]
            if not (fid and name):
                return self._json({"ok": False, "msg": "缺少参数"})
            lib = load_lib()
            if not any(f["id"] == fid for f in lib["folders"]):
                return self._json({"ok": False, "msg": "文件夹不存在"})
            for f in lib["folders"]:
                if f["id"] == fid:
                    f["name"] = name
            save_lib(lib)
            return self._json({"ok": True, "msg": "已改名"})

        if action == "folder.drop":
            fid = (body.get("id") or "").strip()
            lib = load_lib()
            keep = [f for f in lib["folders"] if f["id"] != fid]
            if len(keep) == len(lib["folders"]):
                return self._json({"ok": False, "msg": "文件夹不存在"})
            lib["folders"] = keep
            for bid, f in list(lib["assign"].items()):
                if f == fid:
                    lib["assign"].pop(bid, None)
            save_lib(lib)
            return self._json({"ok": True, "msg": "文件夹已删除，书回到未归类"})

        if action == "book.move":
            bid = (body.get("book") or "").strip()
            folder = (body.get("folder") or "").strip()
            if not safe_book_dir(bid):
                return self._json({"ok": False, "msg": "找不到这本书"})
            lib = load_lib()
            if folder and not any(f["id"] == folder for f in lib["folders"]):
                return self._json({"ok": False, "msg": "文件夹不存在"})
            if folder:
                lib["assign"][bid] = folder
            else:
                lib["assign"].pop(bid, None)
            save_lib(lib)
            return self._json({"ok": True, "msg": "已归入"})

        if action == "book.order":
            bid = (body.get("book") or "").strip()
            before = (body.get("before") or "").strip()
            if not safe_book_dir(bid):
                return self._json({"ok": False, "msg": "找不到这本书"})
            lib = load_lib()
            ids = [b["id"] for b in list_books() if b["id"] != bid]
            if before and before in ids:
                ids.insert(ids.index(before), bid)
            else:
                ids.append(bid)
            lib["order"] = ids
            save_lib(lib)
            return self._json({"ok": True, "msg": "顺序已调整"})

        if action == "book.delete":
            bid = (body.get("book") or "").strip()
            d = safe_book_dir(bid)
            if not d:
                return self._json({"ok": False, "msg": "找不到这本书"})
            title = meta_title(bid)
            shutil.rmtree(d)
            # 上游在 output/ 下还会留一份合并稿，一并清掉（限定在 output 内）
            stray = os.path.join(OUT_DIR, re.sub(r'[<>:"/\\|?*]', "_", title) + ".md")
            if (os.path.isfile(stray)
                    and os.path.realpath(stray).startswith(os.path.realpath(OUT_DIR) + os.sep)):
                try:
                    os.remove(stray)
                except OSError:
                    pass
            lib = load_lib()
            lib["assign"].pop(bid, None)
            lib["order"] = [x for x in lib["order"] if x != bid]
            save_lib(lib)
            return self._json({"ok": True, "msg": "已从书架移除"})

        if action == "shelf.add":
            ids = body.get("ids") or []
            if isinstance(ids, str):
                ids = [ids]
            one = (body.get("book") or "").strip()
            if one:
                ids = list(ids) + [one]
            return self._json(shelf_add(ids))

        return self._json({"ok": False, "msg": "未知操作"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    args = ap.parse_args()
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    ensure_books_dir()
    # 先把这几行打出来：平台不对时，一眼就能看出用的是哪个解释器、浏览器装在哪
    print(f"  解释器  : {py()}", flush=True)
    print(f"  浏览器  : {MS_PLAYWRIGHT}", flush=True)
    print(f"  书库    : {OUT_DIR}", flush=True)
    for p in range(args.port, args.port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            print(f"  微信读书导出 → http://127.0.0.1:{p}", flush=True)
            srv.serve_forever()
            return
        except OSError:
            continue
    print("  找不到可用端口", flush=True)


if __name__ == "__main__":
    main()
