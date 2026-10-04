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
import ai_sum
import board as board_mod
import book_export
import book_import
import book_layout
import book_notes
import cleanup
import clip_article
import feed as feed_mod
import ffmpeg_tool
import flomo_notes
import media_setup
import mindmap
import sync as cloudsync
import video_note
import web_parse

REPO = os.path.dirname(os.path.abspath(__file__))

# 版本号只写在这一处：shell/build_macos.sh 会把它读出来盖进 Info.plist，
# 打的 dmg 也就跟着叫同一个名字，不会再出现「界面一个数、访达另一个数」。
# 界面「关于」那一类要显示它 —— 用户报问题时先问「你装的哪一版」，界面上能直接看到。
VERSION = "1.0.1"


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
# flomo 导进来的那份账（笔记条目 + 附件）单独一格：它是「一条条笔记」而不是「一本书」，
# 收进书架才变成 书库/便签/ 下的目录。账本放缓存里，删了可以重新导，用户的数据源在 flomo。
FLOMO_DIR = os.path.join(CACHE_DIR, "flomo")
WALL_PATH = os.path.join(CACHE_DIR, "wallpaper.bin")
# 本机阅读时长账本：界面里翻书的时间按天累计在这儿（微信读书那边的时间另有来源）。
READSTAT_PATH = os.path.join(CACHE_DIR, "readstat.json")
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
        "exit_code": None, "proc": None, "result": None}

# 子进程回传结构化结果的行前缀，见 _reader。真源在 video_note（产出方），这里引用它，
# 免得协议字符串在两处各写一遍、改了一处另一处静默失效。
RESULT_MARK = video_note.RESULT_MARK


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
    """这本书在磁盘上的目录 —— 全后端唯一的一道「按 id 找书」的闸。

    1.0.1 起书库按模块分了文件夹（`书库/微信读书/<id>`、`书库/剪藏/<id>` …），
    所以这里不能再拼 `OUT_DIR/<id>`：改成问 `book_layout.resolve()`，它按「先猜
    模块、再退到没搬动的平铺旧位置」找一遍。id 里带 `../` 之类的仍然先被形状闸挡掉。
    """
    if not book_id or not re.fullmatch(r"[A-Za-z0-9_\-]+", book_id):
        return None
    return book_layout.resolve(OUT_DIR, book_id)


# 模块 id → 界面上那格的名字。回话里要说「已打开「剪藏」的文件夹」而不是笼统一句
# 「已打开书库文件夹」—— 三格独立以后，用户点的就是其中一格，答复也得落到那一格。
MODULE_NAME = dict(book_layout.MODULES)


def module_dir(module):
    """要往某个模块里写东西之前先建出来：`<书库>/<模块文件夹>`。

    四个写入方（取书引擎 / 剪藏 / 订阅 / 导入）拿它当「书库」用，各自内部仍是
    `<给定的根>/<书号>` 那一层平铺 —— 于是一次目录结构的改动只落在这一个文件里，
    那些引擎与脚本一行不用跟着改。
    """
    return book_layout.book_dir(OUT_DIR, module)


def task_env(module, extra=None):
    """起子进程时要塞进环境的那一份：把 `GUIZANG_OUTPUT` 指到该模块的文件夹。

    引擎与图片脚本里写的都是 `output/<书号>`（它们不该知道书库分了层），
    所以「分层」这件事在启动任务这一刻由环境变量兜住：谁的书谁回自己那格。
    """
    env = {"GUIZANG_OUTPUT": module_dir(module)}
    if extra:
        env.update(extra)
    return env


def book_module(book_id):
    """这本书属于哪个模块（界面上分区、进度分母、写盘位置都问它）。"""
    d = safe_book_dir(book_id)
    if d:
        name = os.path.basename(os.path.dirname(d))
        if book_layout.is_module_dir(name):
            return book_layout.MODULE_OF[name]
    return book_layout.module_of(book_id, book_meta(book_id) if d else None)


def ensure_books_dir():
    """把书库目录建出来，并把老位置里的书搬过来一次。

    0.9.1 及更早把书存在 ~/Library/Application Support/归藏/output —— 藏得深，
    用户基本找不到。0.9.3 起改存用户文档下的「归藏」，取过的书一眼可见。
    升级时若新位置还空着、老位置却有书，就整目录搬过去；不搬的话用户升完级
    会以为「我的书全没了」。只在新书库不存在（或为空）时搬，绝不动已有内容的书库。

    1.0.1 再加一刀：把还平铺在书库根上的书按模块归进各自的文件夹（`book_layout`）。
    同样只在「目标位是空的」时搬，全程不删不覆盖；搬不动的留在原地也照样读得到。
    """
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
    except OSError:
        return
    old = os.path.join(DATA_DIR, "output")
    if os.path.realpath(old) != os.path.realpath(OUT_DIR) and os.path.isdir(old):
        try:
            if not os.listdir(OUT_DIR):   # 新书库已经有东西，别去覆盖
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
    for mod in book_layout.ORDER:         # 六个模块文件夹一次建出来，访达里一眼能认
        try:
            os.makedirs(book_layout.module_path(OUT_DIR, mod), exist_ok=True)
        except OSError:
            pass
    try:
        moved = book_layout.migrate(OUT_DIR, log)
        if moved:
            log(f"--- 书库按模块归置：{len(moved)} 本搬进了各自的文件夹 ---")
    except OSError as e:
        log("--- 书库归置没跑完（不影响阅读）：%s ---" % str(e)[:120])


# ---------- 书架（文件夹 / 归类 / 排序 / 标签） ----------
#
# 1.0.0 只有一套夹子、一套状态标签、一条排序，五个模块（微信读书、本地书架、剪藏、
# 订阅、视频）挤在一起用：给公众号文章建的「随笔」夹子会出现在整本书的分组里，
# 挪一下剪藏的顺序会连带改动微信读书那一堆。1.0.1 起账本按模块分格存：
#
#     {"modules": {"weread": {"folders":…,"assign":…,"order":…,"state":…,"tags":…},
#                  "clip": {…}, "feed": {…}, "video": {…}, "local": {…}, "flomo": {…}}}
#
# 老版把四样摊在顶层，`load_lib()` 读进来时按书号折进各自那一格（前缀认得出的都归对，
# 认不出的算微信读书），落盘后顶层不再留副本 —— 一份数据只有一个地方能改，
# 免得「两处各写一半、下次读哪都对」。

LIB_KEYS = ("folders", "assign", "order", "state", "tags")


def empty_scope():
    return {"folders": [], "assign": {}, "order": [], "state": {}, "tags": {}}


def normalize_lib(d):
    mods = d.get("modules")
    if not isinstance(mods, dict):
        mods = {}
    legacy = {}
    for k in LIB_KEYS:
        v = d.pop(k, None)
        if v not in (None, [], {}, ""):
            legacy[k] = v
    for m in book_layout.ORDER:
        s = mods.get(m)
        if not isinstance(s, dict):
            s = {}
        for k in LIB_KEYS:
            if k in ("folders", "order"):
                if not isinstance(s.get(k), list):
                    s[k] = []
            elif not isinstance(s.get(k), dict):
                s[k] = {}
        mods[m] = s
    if legacy:
        for bid, fid in (legacy.get("assign") or {}).items():
            if isinstance(fid, str):
                mods[book_layout.module_of(bid)]["assign"].setdefault(str(bid), fid)
        for bid in (legacy.get("order") or []):
            s = mods[book_layout.module_of(str(bid))]
            if bid not in s["order"]:
                s["order"].append(bid)
        for bid, st in (legacy.get("state") or {}).items():
            if isinstance(st, str):
                mods[book_layout.module_of(bid)]["state"].setdefault(str(bid), st)
        for bid, tg in (legacy.get("tags") or {}).items():
            if isinstance(tg, list):
                mods[book_layout.module_of(bid)]["tags"].setdefault(str(bid), tg)
        # 顶层那套夹子是「所有模块共用」的：被谁引用就抄给谁；一个都没被引用的
        # （刚建好还没放书）留在微信读书那格，别让它凭空消失。
        defs = [f for f in (legacy.get("folders") or [])
                if isinstance(f, dict) and f.get("id")]
        used_by = {fid: {m for m in book_layout.ORDER if fid in mods[m]["assign"].values()}
                   for fid in [f["id"] for f in defs]}
        for f in defs:
            takers = used_by.get(f["id"]) or {"weread"}
            for m in takers:
                have = {x.get("id") for x in mods[m]["folders"]}
                if f["id"] not in have:
                    mods[m]["folders"].append(dict(f))
    d["modules"] = mods
    return d


def lib_scope(lib, module=None):
    """某模块那一格账；模块名不认识就退回微信读书那格（与旧界面行为一致）。"""
    mods = lib.get("modules") or {}
    key = module if module in book_layout.DIR_OF else "weread"
    s = mods.get(key)
    if not isinstance(s, dict):
        s = empty_scope()
        mods[key] = s
        lib["modules"] = mods
    for k in LIB_KEYS:
        if k not in s:
            s[k] = [] if k in ("folders", "order") else {}
    return s


def load_lib():
    try:
        with open(LIB_PATH, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    return normalize_lib(d)


# 服务多线程跑，几个小账本（书架、配置、阅读时长）都会落盘。原来它们共用
# 固定的「xxx.tmp」，两个请求同时写就会有一个的 os.replace 扑空、直接抛错把连接带崩。
# 统一走这个原子写：临时名带 pid+随机串互不打架，整体再串行化一次，稳。
_WRITE_LOCK = threading.RLock()


def _write_json(path, obj, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.%s.tmp" % (path, os.getpid(), uuid.uuid4().hex[:8])
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    if mode is not None:
        try:
            os.chmod(path, mode)
        except OSError:
            pass


def save_lib(d):
    with _WRITE_LOCK:
        _write_json(LIB_PATH, d)


def prune_lib(lib):
    """丢掉已经不存在的书留下的记录（每个模块那一格各自清，认的是同一份「还在的书」）。"""
    alive = {bid for bid, _d, _m in book_layout.walk(OUT_DIR)}
    for m in book_layout.ORDER:
        s = lib_scope(lib, m)
        s["assign"] = {k: v for k, v in s["assign"].items() if k in alive}
        s["order"] = [x for x in s["order"] if x in alive]
        s["state"] = {k: v for k, v in s["state"].items() if k in alive}
        s["tags"] = {k: v for k, v in s["tags"].items() if k in alive}
        # 空夹子不裁：刚建好还没放东西的文件夹被这一刀削掉，界面上就是
        # 「点了新建、闪了一下没影」；夹子只能由人自己删。
    return lib


def lib_forget(lib, book_id, module=None):
    """把一本书从账本上抹干净（删书、或书换了模块时用）。

    不指定模块就五格都清一遍：一本书只住一格，多清几格是空操作，
    但漏清一格就会留下「一个不存在的书还占着夹子和排序」的幽灵记录。
    """
    for m in ([module] if module in book_layout.DIR_OF else book_layout.ORDER):
        s = lib_scope(lib, m)
        s["assign"].pop(book_id, None)
        s["order"] = [x for x in s["order"] if x != book_id]
        s["state"].pop(book_id, None)
        s["tags"].pop(book_id, None)
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
    with _WRITE_LOCK:
        _write_json(CFG_PATH, d, 0o600)      # 里面存着 API Key，落盘后就收紧权限


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


# ---------- 本机阅读时长 ----------
#
# 微信读书那边的时间只认在它 App 里读的；用户在「归藏」里翻本地书的那些时间它不知道。
# 所以这里另立一本账：界面开着阅读器时，前端按心跳把「刚过去了几秒」报过来，
# 后端按天、按书累加。它和微信读书的时间是两笔，合并在一起才是真正的「总阅读时长」。

# 服务是多线程的，前端心跳可能并发打进来。账本是「读—改—写」，不锁就会出现
# 两个请求各自读旧值再各自写回，把对方那几秒抹掉；多线程共用同一个 .tmp 名还会让
# os.replace 找不到文件直接抛错。锁住整段、并用带随机后缀的临时文件，两个毛病都断掉。
_READSTAT_LOCK = threading.RLock()


def load_readstat():
    try:
        with open(READSTAT_PATH, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault("total", 0)
    d.setdefault("days", {})
    d.setdefault("books", {})
    return d


def save_readstat(d):
    with _WRITE_LOCK:
        _write_json(READSTAT_PATH, d)


def readstat_tick(book, title, seconds):
    """记一笔本机阅读时间。seconds 由前端算好（两次心跳的间隔），这里只累加。

    夹在 0..120 之间：前端卡住/切到后台再回来，可能报一个很大的间隔，
    不能让它一次把一整段挂机时间算进阅读时长。负数和 0 直接丢。
    """
    try:
        sec = int(round(float(seconds)))
    except Exception:
        return False
    if sec <= 0:
        return False
    sec = min(sec, 120)
    # 整段「读—改—写」都在锁里，并发心跳才不会互相覆盖
    with _READSTAT_LOCK:
        d = load_readstat()
        day = time.strftime("%Y-%m-%d")
        d["total"] = int(d.get("total", 0)) + sec
        days = d.setdefault("days", {})
        days[day] = int(days.get(day, 0)) + sec
        bid = book if isinstance(book, str) else ""
        bid = bid.strip()
        if bid:
            b = d.setdefault("books", {}).setdefault(bid, {"title": "", "seconds": 0, "last": 0})
            ttl = title if isinstance(title, str) else ""
            if ttl.strip():
                b["title"] = ttl.strip()[:120]
            b["seconds"] = int(b.get("seconds", 0)) + sec
            b["last"] = int(time.time())
        # 只留最近 400 天，别让账本无限长
        if len(days) > 400:
            for k in sorted(days.keys())[:len(days) - 400]:
                days.pop(k, None)
        save_readstat(d)
    return True


def readstat_summary():
    """把账本整理成界面要的样子：总秒数、逐日秒数、每本书的秒数。

    逐日那份全给（账本本就只留最近 400 天）：界面按「本周／本月／本年／总计」
    从这份逐日表里自己求和，跟微信读书那几档对齐，不必后端再按档位算一遍。
    """
    d = load_readstat()
    days = sorted((d.get("days") or {}).items())
    books = sorted(((d.get("books") or {}).items()),
                   key=lambda kv: -(kv[1].get("seconds") or 0))[:100]
    return {"total": int(d.get("total", 0)),
            "days": [[k, int(v)] for k, v in days],
            "books": [{"id": k, "title": (v.get("title") or ""),
                       "seconds": int(v.get("seconds", 0)),
                       "last": int(v.get("last", 0))} for k, v in books]}


def readstat_clear():
    with _READSTAT_LOCK:
        save_readstat({"total": 0, "days": {}, "books": {}})
    return True, "本机阅读时长已清零"


# ---------- 云同步 ----------
#
# 通道与协议都在 sync.py 里，这边只做三件事：读写配置、给页面状态、把结果记进 config。

_SYNC_PENDING = {}      # OneDrive 设备码：授权进行中时暂存在内存，重启即失效（本就要重来）


def sync_state():
    return cloudsync.state(load_cfg().get("sync") or {})


def _sync_store(cfg):
    c = load_cfg()
    c["sync"] = cfg
    save_cfg(c)


def sync_save(incoming):
    old = load_cfg().get("sync") or {}
    ok, msg, cfg = cloudsync.save(old, incoming)
    if ok:
        _sync_store(cfg)
    return ok, msg


def sync_test():
    c = cloudsync._norm(load_cfg().get("sync") or {})
    return cloudsync.test(c)


def sync_run_now():
    """立刻跑一次同步。跑完把结果记进配置，界面上那行「上次同步」就有得看。"""
    cfg = cloudsync._norm(load_cfg().get("sync") or {})
    ok, msg = cloudsync.run(cfg, CACHE_DIR, OUT_DIR, log=log)
    cfg["last"] = {"at": int(time.time()), "msg": msg}
    _sync_store(cfg)
    return ok, msg


def sync_device_start():
    ok, msg, info = cloudsync.device_start(load_cfg().get("sync") or {})
    if ok and info:
        _SYNC_PENDING["device_code"] = info.get("device_code")
        _SYNC_PENDING["expires"] = time.time() + info.get("expires_in", 900)
    return ok, msg, info


def sync_device_poll():
    code = _SYNC_PENDING.get("device_code")
    if not code or time.time() > _SYNC_PENDING.get("expires", 0):
        return False, "没在等授权，或者代码过期了，重新来一次"
    cfg = load_cfg().get("sync") or {}
    status, msg, updated = cloudsync.device_poll(cfg, code)
    if status == "ok" and updated:
        _sync_store(updated)
        _SYNC_PENDING.pop("device_code", None)
        return True, msg
    return False, msg


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


def cache_meta_cover(book_id):
    """按 meta.json 里那个远端封面地址，把图落到 cache/covers/<id>.jpg。

    为什么单列一层：剪藏入库、订阅入库、视频转完、后台补封面，四处都要做同一件事
    ——「这本书的封面地址写在 meta 里，去取回来」。以前只有剪藏和订阅那两处显式调了
    cache_cover，视频那条线转完就没人管，于是用户点名要的「视频封面当笔记封面」落不了地。
    """
    if not book_id:
        return False
    return cache_cover(book_id, str((book_meta(book_id) or {}).get("cover") or ""))


def warm_covers(limit=80):
    """后台把书架上还没有封面的书补齐（需要 Key）。不阻塞请求。"""
    def job():
        n = 0
        for b in list_books():
            if n >= limit:
                break
            if b.get("cover"):
                continue
            if book_layout.module_of(b["id"]) != "weread":
                # 剪藏 / 视频 / 订阅的封面本来就是 meta.json 里那个远端地址：入库时取过
                # 一次，失败过的（当时没网、图床抽风）在这里补上，旧书也能长出封面。
                if cache_meta_cover(b["id"]):
                    n += 1
                continue
            if ensure_cover(b["id"]):
                n += 1
        if n:
            log(f"--- 已补齐 {n} 张封面 ---")
    threading.Thread(target=job, daemon=True).start()


# ---------- 一键建立 MCP 的提示词 ----------

def mcp_tool_names():
    """从适配器源码里数出「一共有几个工具」—— 界面上那句说明要用它。

    为什么不写死一个数：这份提示词是拿去给别人的 agent 读的，工具加一个它旧一次。
    上一版写死「20 个工具」，这一轮画板 / 导图 / 转写三条线加完已经是 45 个，
    说明却还停在 20 —— 用户照着念给 agent 听，一句准话就变成了误导。
    唯一真源是 TOOL_DEFS 那份清单，读一次就有，永远跟着代码走。
    """
    path = os.path.join(REPO, "mcp", "guizang-mcp.mjs")
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read()
    except OSError:
        return []
    tail = src.split("const TOOL_DEFS = [", 1)
    if len(tail) < 2:
        return []
    return re.findall(r'^\s{4}name: "([^"]+)"', tail[1], re.M)


def mcp_prompt():
    """给 AI Agent 的接入提示词。

    刻意**不含任何个人信息**（不出现绝对路径、用户名、端口、项目目录名）：
    这份提示词是要发给别人或别人机器上用的，路径与服务地址都可能不同，
    所以只描述「做什么、找什么文件、怎么配」，具体位置让 Agent 自己确认。
    """
    n = len(mcp_tool_names())
    head = ("它提供 %d 个工具，分这些类：" % n) if n else "它提供的工具分这些类："
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
        head,
        "· 书架与状态：shelf_list、app_status、task_log、book_files、folder_create、book_move",
        "· 取书与账号：book_fetch（开始）、task_stop（中止）、batch_fetch（排队多本）、account_connect（扫码登录）",
        "· 书与笔记：book_detail、search_books（本地 + 划线 + 书城三域）、notes_index、notes_search、",
        "  notes_random、book_mark（待读 / 在读 / 已读）",
        "· 写回与导出：shelf_add（把书城搜到的书加进我自己的微信读书书架）、apkg_export（划线 → Anki 卡包）、",
        "  zip_export（多本打包）、cache_delete（删本地缓存，默认只列不删）",
        "· 网页剪藏：clip_url（知乎 / 小红书 / X / 公众号链接 → 一本本地书）",
        "· 订阅（RSS）：feed_list、feed_discover、feed_add、feed_entries、feed_entry、feed_refresh、",
        "  feed_to_shelf、feed_remove",
        "· 视频转笔记：video_capability、video_plan、video_to_shelf（开始转写）、video_books、",
        "  video_transcript（读，每段带 id）、video_transcript_save（改）、video_rebuild（重建正文）、video_export（导字幕）",
        "· 思维导图：map_show、map_from_notes（只给不存）、map_save",
        "· 画板：board_list、board_show、board_new、board_save、board_delete",
        "· 便签（flomo）：flomo_notes（读，按标签 / 关键字筛，带 id 取全文）、",
        "  flomo_portrait（读「用户记忆画像」，只有数字与标签，不含原文）",
        "",
        "五点注意，都是这个工具的设计口径，请照着做：",
        "1. 取书、转写、批量取书、刷新订阅都是分钟到小时级的长任务：调用会立即返回，",
        "   进度用 app_status / task_log 轮询，不要在调用里等它跑完。",
        "2. 改转写请只用 video_transcript_save 的 edits / drop / add —— 只说改了哪几段，",
        "   工具自己读回整本再写盘；后端那个接口是整本覆盖的，别把整本抄一遍发上去。",
        "3. map_save 不给 nodes 就只改标题 / 形态，board_save 不带 canvas 就不动画面 ——",
        "   用户手画的图和涂鸦覆盖不了第二次。",
        "4. 删除类要显式确认：cache_delete 带 confirm=true 才真删，board_delete 是不可恢复的。",
        "5. 只要这件事和用户的笔记有关，先调一次 flomo_portrait 再动手：那份画像是",
        "   「他怎么记东西」的账（节奏、标签层级、长短、常打的标签），读完再决定翻几条、",
        "   按哪个标签找、回复写多长。便签一律只读 —— 改笔记是用户自己的事，没有写口。",
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


ASR_ENGINES = ("auto", "mlx", "faster", "cloud")


def set_media_cfg(engine, lang):
    """存「视频转笔记」这两项偏好：转写引擎与语言。

    为什么不放在 video_note 内部读盘：转写这条线本来就能脱离界面单独跑（命令行、
    MCP 都走同一条路），配置由调用方通过 opts 传进去，它不越权回读应用配置。界面
    这一侧把它存下来，起任务时随 opts 一起发过去 —— 两边各管一段，谁都不猜。
    """
    engine = (engine or "").strip().lower()
    if engine not in ASR_ENGINES:
        return False, "转写引擎只认 auto / mlx / faster / cloud 这几种"
    cfg = load_cfg()
    cfg["asr_engine"] = engine
    lang = (lang or "").strip()[:8]
    if lang:
        cfg["asr_lang"] = lang
    else:
        cfg.pop("asr_lang", None)
    save_cfg(cfg)
    return True, "已保存（转写引擎：%s%s）" % (engine, "，语言：" + lang if lang else "")


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


class AgentStreamCut(RuntimeError):
    """上游话说一半就断了线。

    单独一个类，是为了让调用方能把它和「连不上」「回 500」分开处理：那两种一个字
    都没收到，这一种屏幕上半截都在 —— 半截不能当成品存盘，否则上一次那份好的被
    覆盖掉，用户以为内容还在，其实已经少了尾巴。
    """


def agent_deltas(resp):
    """把上游的 SSE 拆成一段段正文。上游字段缺失/半行 JSON 都直接跳过 ——
    宁可少吐几个字，也不能因为一行脏数据把整段对话打断。

    但收尾必须认账：连接结束之前既没见过 data: [DONE]、也没见过 finish_reason，
    那就是中途拔线，不是答完了。实测（/tmp/probe_cut.py）这种断法不抛任何异常，
    urllib 把 EOF 读成「正常结束」，所以只能自己记 —— 不记的话调用方会把半截
    当完整答案存进 _ai/，界面上还写着「小结好了」。
    """
    ended = False        # 见过 data: [DONE]（OpenAI 系的标准收尾）
    finished = False     # 见过 finish_reason（有些中转不发 [DONE]，只发这个，不能当断了）
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            ended = True
            break
        try:
            j = json.loads(data)
        except Exception:
            continue
        for ch in (j.get("choices") or []):
            piece = (ch.get("delta") or {}).get("content")
            if piece:
                yield piece
            if ch.get("finish_reason"):
                finished = True
    if not (ended or finished):
        raise AgentStreamCut("上游话说一半就断了线，这次这份没存（上一次的那份还在）")


def agent_once(messages, timeout=90):
    """问一次、等整段答案（不流式）。要 JSON 的那些活儿用它 —— 导图、画像这类，
    半棵树在屏幕上蹦出来没有意义，拼完整了才能解析。失败抛人话，由调用方翻成提示。"""
    url, key, model = agent_cfg()
    if not url or not model:
        raise RuntimeError("还没配 AI 接口：设置 → 读书小助手，填地址和模型名")
    body = {"model": model, "messages": messages, "stream": False}
    with agent_open({"url": url, "key": key, "body": body},
                    stream=False, timeout=timeout) as r:
        j = json.loads(r.read().decode("utf-8", "replace"))
    try:
        say = (j.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    except Exception:
        say = ""
    say = str(say).strip()
    if not say:
        raise RuntimeError("那边没给出内容，模型名或地址可能不对")
    return say


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


# ---------- 划词/点词问答（翻译、查词） ----------
#
# 与 agent_chat 同源：都走用户自己配的那个兼容 OpenAI 的接口。分别只在「问什么」——
# 这里问的是「把这段翻成中文」或「这个词什么意思」，一次问完，所以不流式（要的是完整答案）。

QUICK_SYS = {
    # 长选正文 → 直接给中文。只译文，不解释、不寒暄，省得用户还要自己挑。
    "translate": (
        "你是翻译。把用户给的外文译成自然、准确的中文。"
        "只输出译文本身，不要加引号、不要解释、不要复述原文。"
        "若给了上下文，用上下文消歧，但译文只覆盖用户选中的那段。"
    ),
    # 点单个英文词 → 一个能一眼看完的小卡片：词性释义 + 一个例句。
    "word": (
        "你是英汉词典。用户点了一个英文词，给出简短答案："
        "先给词性与中文释义（多个义项分行），再用这个词造一个简短例句并给中文。"
        "若有常用搭配或易混点，用一句话点明。不要长篇大论，不要客套。"
    ),
}


def agent_quick(mode, text, ctx=""):
    """划词/点词问一次，等完整答案。返回 (ok, 文本或人话)。

    非流式：这类问题是「一次问完、一次看全」，流式的逐字蹦反而不好读。
    """
    url, key, model = agent_cfg()
    if not url or not model:
        return False, "还没配 AI 接口：设置 → 读书小助手，填地址和模型名"
    text = (text or "").strip()
    if not text:
        return False, "没有选中内容"
    sys = QUICK_SYS.get(mode) or QUICK_SYS["translate"]
    user = text[:4000]
    if (ctx or "").strip():
        user = "上下文：%s\n\n需要处理的：%s" % (ctx.strip()[:1500], text[:4000])
    body = {"model": model, "stream": False,
            "messages": [{"role": "system", "content": sys},
                         {"role": "user", "content": user}]}
    try:
        with agent_open({"url": url, "key": key, "body": body},
                        stream=False, timeout=60) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return False, str(e)[:200]
    try:
        say = (j.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    except Exception:
        say = ""
    say = (say or "").strip()
    return (True, say) if say else (False, "那边没给出内容，换个模型名试试")


# ---------- AI 小结：读书 / 剪藏 / 视频共用一个入口 ----------
#
# 界面上只有一个「小结」，范围和模式都由 ai_sum 定：读书一次只读当前这一章，
# 剪藏 / 订阅 / 视频这类「本来就是一篇」的整篇读。这里只负责三件事 ——
# 把正文取出来、把流转到用户屏幕上、把结果存回这本书的 _ai/。
# 规则本身不写在这儿，免得前后端各写一套「什么算一章」。

def ai_source(book_id, chapter=""):
    """这一次小结要读什么。返回 (True, {正文,标题,范围,来路,章}) 或 (False, 人话)。"""
    meta = book_meta(book_id)
    module = ai_sum.module_of(meta, book_id)
    scope, ch = ai_sum.scope_for(module, chapter)
    if not scope:
        return False, "读书这类一次只小结当前这一章：先在阅读页翻到要读的那一章"
    if scope == "chapter":
        text = chapter_text(book_id, ch)
        miss = "没找到这一章的正文（%s）" % (ch or "")
    else:
        text = merged_markdown(book_id)
        miss = "这篇内容还没在本地落成文件，先重新入库一次"
    if not text or not text.strip():
        return False, miss
    return True, {"text": text, "title": meta.get("title") or book_id,
                  "scope": scope, "module": module, "chapter": ch}


def ai_summary_stream(book_id, style, chapter, emit):
    """边生成边往外递，收完存进这本书的 _ai/。返回 (ok, 一句话)。

    存盘只在拿到内容之后：半路失败时留下上一次的旧结果比留一个空文件好 ——
    用户重开这一页还能看见上次那份，不会以为笔记被自己点没了。
    """
    ok, src = ai_source(book_id, chapter)
    if not ok:
        return False, src
    url, key, model = agent_cfg()
    if not url or not model:
        return False, "还没配 AI 接口：设置 → 读书小助手，填地址和模型名"
    msgs = ai_sum.messages(style, src["scope"], src["title"], src["text"], src["module"])
    body = {"model": model, "messages": msgs, "stream": True}
    got = []
    try:
        with agent_open({"url": url, "key": key, "body": body}) as r:
            for piece in agent_deltas(r):
                got.append(piece)
                emit(piece)
    except Exception as e:
        return False, str(e)[:200]
    text = "".join(got).strip()
    if not text:
        return False, "那边一个字都没回，模型名或地址可能不对"
    d = safe_book_dir(book_id)
    if d:
        ai_sum.save_result(d, src["scope"], style, src["chapter"], text)
    return True, "小结好了（已存进这本书的 _ai/）"


def ai_mindmap(book_id, chapter=""):
    """这篇内容 → 一棵导图树 → 直接存成这本书可编辑的那张图。返回 (ok, 给界面的话 + doc)。

    和「从笔记生成」不同，这一步不先问一遍：用户点的是「让 AI 画一张」，等的就是图。
    覆盖前 mindmap 那边会把旧的一份换成 .prev 留着（见 _write_json），手画的那张因此
    不是点一下就没了；界面另有一句「已覆盖，原来那张还在 .prev 里」的提示兜底。
    """
    ok, src = ai_source(book_id, chapter)
    if not ok:
        return False, {"msg": src}
    url, _, model = agent_cfg()
    if not url or not model:
        return False, {"msg": "还没配 AI 接口：设置 → 读书小助手，填地址和模型名"}
    msgs = ai_sum.map_messages(src["title"], src["text"], src["scope"])
    try:
        raw = agent_once(msgs, 180)
    except Exception as e:
        return False, {"msg": "没能问出导图：%s" % str(e)[:160]}
    tree, warn = ai_sum.parse_map(raw, src["title"], src["text"])
    d = safe_book_dir(book_id)
    if not d:
        return False, {"msg": "没找到这本书，图没地方放"}
    doc = mindmap.from_note_tree(tree, src["title"])
    try:
        res = mindmap.save_map(d, doc)
    except Exception as e:
        return False, {"msg": "这张图存不下去：%s" % str(e)[:140]}
    if not res.get("ok"):
        return False, {"msg": res.get("error") or "这张图没存进去"}
    saved = mindmap.load_map(d)
    msg = "AI 画好了（%d 个节点）" % res.get("nodes", 0)
    if warn:
        msg = "%s，%d 个节点" % (warn, res.get("nodes", 0))
    return True, {"doc": saved, "layout": mindmap.layout(saved),
                  "nodes": res.get("nodes", 0), "msg": msg}


def list_books(module=None):
    """书库清单。给 `module` 就只列那一个模块（界面上每个模块各问各的，
    剪藏的文章不会再混进微信读书那一堆）。"""
    books = []
    lib = prune_lib(load_lib())
    scopes = {m: lib_scope(lib, m) for m in book_layout.ORDER}
    for name, d, mod in book_layout.walk(OUT_DIR):
        if module and mod != module:
            continue
        s = scopes[mod]
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
        # 「谁在跑」只有一个真源：进程还在不在。引擎被强制结束时，_progress.json 里会
        # 留下一个 running:true，界面据此把这本书永远标成「正在取」，卡片上的按钮一个都不出
        # —— 用户报的「中止之后不能再取书」有一半是这里来的。
        if progress.get("running") and not (TASK.get("running") and TASK.get("book") == name):
            progress = {**progress, "running": False, "stale": True}
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
            # 来源：微信读书取回来的（默认）、用户自己导进来的（local）、剪藏来的
            # 一篇文章（clip）。前端书架据此分组显示。
            "source": meta.get("source") or "weread",
            "format": meta.get("format") or "",
            # 剪藏的书带原文地址：卡片上的「看原文」和详情页都要用
            "url": meta.get("url") or "",
            # 本地笔记条数：卡片上那行「划了 N 段 · 记了 M 条」
            "notes": notes_counts(d),
            "progress": {k: progress.get(k) for k in
                         ("pages", "chapters", "session_chars", "running",
                          "budget_left", "stalled", "stale", "updated_at")
                         if progress.get(k) is not None},
            "folder": s["assign"].get(name) or "",
            "state": (s.get("state") or {}).get(name) or "",
            "tags": s["tags"].get(name) or [],
            # 这本书住在哪个模块文件夹里：前端分区、MCP 按模块问话都靠它，
            # 不再拿 source/format 在前端自己猜一遍（猜错过一次：视频和订阅漏进「全部」）
            "module": mod,
            # 深链：微信书回原书阅读器，剪藏的文章回它的原文，导入的本地书没有来路
            "deep": book_deep_link(name, meta),
            # 前端据此决定要不要请求封面：没有就不请求，免得满屏 404
            "cover": os.path.isfile(cover_path(name)),
        })
    # 书架顺序：先在 order 里的按位置排，其余按最近更新排在后面。
    # 每个模块一条自己的顺序 —— 拖剪藏的第 3 张不该把微信读书那堆一起挪了。
    pos = {}
    for m in book_layout.ORDER:
        for i, bid in enumerate(lib_scope(lib, m)["order"]):
            pos.setdefault((m, bid), i)
    books.sort(key=lambda b: (pos.get((b["module"], b["id"]), 10 ** 9), -b["updated_at"]))
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
            # 阅读器顶栏要据此决定要不要给「看原文」：剪来的文章有来路，取回的书没有
            "source": meta.get("source") or "weread",
            "url": meta.get("url") or "",
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


def board_file(book_dir, name):
    """画板的导出物：<书>/boards/<name>，只认 <id>.svg / <id>.png / <id>.json。

    和 book_image 同一个白名单口径（名字里不许有斜杠和点号玩法），区别只在于
    这里的 SVG 是「浏览器导出来存下的东西」，不是我们自己生成的。SVG 能带脚本，
    同域直接打开就会跑 —— 所以调用方给 SVG 配一条 CSP（default-src 'none'），
    读图归读图，别让它有机会说话。
    """
    if not name or not re.fullmatch(r"[A-Za-z0-9_-]+\.(json|svg|png)", name):
        return None
    ext = os.path.splitext(name)[1].lower()
    mime = {"json": "application/json; charset=utf-8"}.get(ext) or _IMG_MIME.get(ext)
    if not mime:
        return None
    root = os.path.realpath(os.path.join(book_dir, board_mod.BOARD_DIR))
    p = os.path.realpath(os.path.join(root, name))
    if not p.startswith(root + os.sep) or not os.path.isfile(p):
        return None
    return p, mime



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


# 一次「没跑完」会留下什么：章节 md、raw 页缓存、图片、目录快照、进度快照，
# 外加引擎在书库根上写的那份合并稿。EPUB/PDF/ZIP 成品落在下载目录和 cache 里，
# 不在书目录，所以不在清理范围 —— 清残稿不该顺手把用户已经拿走的成品也带走。
RESIDUE_DIRS = ("chapters", "raw", "images")
RESIDUE_FILES = ("_catalog.json", "_progress.json")


def drop_stray_md(book_id, title=""):
    """清掉书库里属于这本书的「合并稿」，返回删掉的个数。

    引擎跑完整本会在它写书的目录旁再落一份 `<书名>.md`（早期叫 `<书号>.md`），
    那是可再生的副本，不是唯一的一份。两处都要看：新结构在模块文件夹里，
    没搬动的老书和命令行直接跑出来的还在书库根上。只删书库内的普通文件，
    名字按 `os.path.realpath` 再核一遍归属 —— 书名是用户可控的字符串，
    带 `../` 的那种不能拼成「一路删到书库外面」。
    """
    root = os.path.realpath(OUT_DIR)
    places = [os.path.join(root, book_layout.DIR_OF[book_module(book_id)]), root]
    names = {str(title or ""), str(book_id or "")}
    gone = 0
    for place in places:
        for nm in names:
            if not nm:
                continue
            p = os.path.join(place, re.sub(r'[<>:"/\\|?*]', "_", nm) + ".md")
            real = os.path.realpath(p)
            if not (real.startswith(root + os.sep) and os.path.isfile(real)):
                continue
            try:
                os.remove(real)
                gone += 1
            except OSError:
                pass
    return gone


def reset_book_output(book_id):
    """抹掉一本「取到一半」的书的残稿，给「清掉重取」让路。

    只清产物，不清身份：meta.json 留着（书名和作者在里面，丢了它卡片会退化成
    一串书号），只把 done 置回 False；notes.json（用户自己的划线与笔记）和
    library.json 里的归类、状态标签一概不动。取书失败不该让人连带丢笔记。
    """
    d = safe_book_dir(book_id)
    if not d:
        return {"ok": False, "msg": "找不到这本书的导出目录，直接取书就行"}
    removed = 0
    for sub in RESIDUE_DIRS:
        p = os.path.join(d, sub)
        if os.path.isdir(p):
            removed += sum(len(fs) for _, _, fs in os.walk(p))
            shutil.rmtree(p)
        elif os.path.exists(p):
            os.remove(p)
            removed += 1
    for f in RESIDUE_FILES:
        p = os.path.join(d, f)
        if os.path.isfile(p):
            os.remove(p)
            removed += 1
    # 合并稿有两份可能的名字：按书名的和早期按书号的，都清掉，免得留一份过期的全本假象
    removed += drop_stray_md(book_id, meta_title(book_id))
    mp = os.path.join(d, "meta.json")
    meta = {}
    if os.path.exists(mp):
        try:
            meta = json.load(open(mp, encoding="utf-8"))
        except Exception:
            meta = {}
    meta.update({"done": False, "updated_at": time.time()})
    _write_json(mp, meta)
    log(f"清掉《{meta.get('title') or book_id}》上次没跑完的残稿：{removed} 个文件")
    return {"ok": True, "removed": removed, "title": meta.get("title") or book_id,
            "msg": f"已清掉上次剩下的 {removed} 个文件"}


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
        # 子进程要交回结构化结果（比如视频那条线：书 id、字数、AI 缺席原因），
        # 就走这一行约定：`##GUIZANG## {json}`。日志照旧原样记，前端也能看到。
        # 之所以不用退出码带信息：退出码只能是 0-255 的一个数，装不下一个 json。
        if line.startswith(RESULT_MARK):
            try:
                with _lock:
                    TASK["result"] = json.loads(line[len(RESULT_MARK):])
            except Exception:
                pass
        log(line)
    proc.wait()
    with _lock:
        TASK["exit_code"] = proc.returncode
        TASK["running"] = False
        book = TASK["book"]
    if TASK.get("kind") in ("export",) and book:
        write_meta(book, title, author, done)
    if TASK.get("kind") == "video":
        # 视频封面落到 cache/covers/<id>.jpg：书架只在本地有文件时才请求 /api/cover，
        # 不顺手取一次，视频那一格里就永远是生成色块 —— 用户要的是「视频封面当笔记封面」。
        bid = str((TASK.get("result") or {}).get("book_id") or "").strip()
        if bid:
            if cache_meta_cover(bid):
                log(f"--- 视频封面已存下：{bid} ---")
            else:
                log(f"--- 视频封面没取到（{bid}），书架先用生成式封面 ---")
    log(f"--- 任务结束（退出码 {proc.returncode}）---")


def start_task(kind, argv, book=None, env_extra=None):
    with _lock:
        if TASK["running"]:
            return False, "已有任务在运行"
        TASK.update({"running": True, "kind": kind, "book": book,
                     "started_at": time.time(), "exit_code": None, "result": None})
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


# ---------- 剪藏与本地笔记 ----------
# 两件事都寄生在「书」这个已有结构上：剪回来的文章是一本 source=clip 的书，
# 笔记是同一目录里的 notes.json / notes.md / mindmap.svg。这样详情页、阅读器、
# 导出 EPUB/PDF、云同步、在访达里定位这些现成能力一行都不用改就能吃下新功能，
# 也不必为剪藏和笔记各开一套存储 —— 用户要的是「文件都在我那个文件夹里」。

MAX_CLIP_URLS = 20        # 一次最多剪这么多篇，多了容易被对方站点限流


def book_meta(book_id):
    """读一本书的 meta.json。读不到回空表，兜底交给调用方（列表页允许没 meta）。"""
    d = safe_book_dir(book_id)
    if not d:
        return {}
    try:
        with open(os.path.join(d, "meta.json"), encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def book_deep_link(book_id, meta):
    """这本书「回得去的地方」：微信书是原书阅读器，剪藏/订阅/视频是原文，导入的书没有。

    原来写死在列表里，判据只有一个 local；加剪藏之后来路变多了（现在是四种：
    weread / local / clip / feed / video），抽成一个函数比在字典字面量里套三元好读。
    """
    src = meta.get("source") or "weread"
    if src in ("clip", "feed", "video"):
        return meta.get("url") or ""
    if src == "local":
        return ""
    return "https://weread.qq.com/web/reader/%s" % book_id


def clip_targets(body):
    """从请求里收链接：单条 url 和批量 urls 都认，去重去空、封顶。

    剪藏是这台机器主动去访问外网，所以入口只认这一个函数拿到的链接 ——
    scheme 和内网地址由 clip_article.fetch 再挡一道，两层各管一件事。
    """
    raw = list(body.get("urls") or []) if isinstance(body.get("urls"), list) else []
    one = str(body.get("url") or "").strip()
    if one:
        raw.insert(0, one)
    out, seen = [], set()
    for x in raw:
        u = str(x.get("url") if isinstance(x, dict) else x or "").strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out[:MAX_CLIP_URLS]


_NOTES_COUNTS = {}


def notes_counts(book_dir):
    """书架上那句「划了 12 段 · 记了 3 条」。

    /api/state 是轮询的，所以按 (mtime, size) 缓一份：没改过的书不重复解析 JSON。
    """
    p = os.path.join(book_dir, book_notes.NOTES_FILE)
    try:
        st = os.stat(p)
    except OSError:
        return {"marks": 0, "entries": 0}
    key = (int(st.st_mtime), st.st_size)
    hit = _NOTES_COUNTS.get(book_dir)
    if hit and hit[0] == key:
        return hit[1]
    out = {"marks": 0, "entries": 0}
    try:
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
        if isinstance(doc, dict):
            out = {"marks": len(doc.get("marks") or []),
                   "entries": len(doc.get("entries") or [])}
    except Exception:
        pass
    if len(_NOTES_COUNTS) > 500:
        _NOTES_COUNTS.clear()
    _NOTES_COUNTS[book_dir] = (key, out)
    return out


# ---------- 订阅 / 视频 / 平台剪藏（三条新来路）----------
# 三个新功能全都在「书」这个已有结构上收口，和剪藏是一套道理：
#   · 知乎 / 小红书 / X：只换一个提取器，其余和剪藏同一条路（clip_extract）
#   · RSS 订阅：条目 → feed.to_shelf() → 一本 source=feed 的书
#   · 视频转笔记：video_note.run() → 一本 source=video 的书
# 所以详情页、阅读器、笔记、导出 EPUB/PDF、云同步这些现成能力一行都不用改。

def clip_extract(url):
    """链接 → 文章。知乎 / 小红书 / X 走专门的解析，其余走通用剪藏。

    web_parse 是「尽量免登录」的路子：X 有公开接口能稳拿；知乎、小红书能拿就拿，
    拿不到会抛一句人话（要验证 / 要登录），原样递出去。**不悄悄退回通用提取器** ——
    那样只会把对方的验证页当成正文收进书架，用户以为剪到了，其实剪回来一张空壳。
    """
    if web_parse.platform_of(url):
        return web_parse.extract(url)
    return clip_article.extract(url)


def cache_cover(book_id, url):
    """把封面图落到 cache/covers/<id>.jpg —— 书架上一张真封面顶十行文字。

    剪藏 / 订阅 / 视频这三条来路的封面都是远端的 og:image 或视频缩略图，而书架
    只在本地有文件时才请求 /api/cover（免得满屏 404）。所以入库时顺手取一次，
    失败了也不吭声：封面是锦上添花，不该把「这本书存下了」变成「这本没存下」。
    """
    u = (url or "").strip()
    if not (book_id and re.match(r"^https?://", u)):
        return False
    # 地址是别人网页里写的 og:image / 正文首图 / 视频缩略图，不是用户自己粘的链接，
    # 所以内网与本机地址必须在下载前挡掉（clip_article 里那套规则，一处说了算）。
    if not clip_article.safe_image_url(u):
        return False
    p = cover_path(book_id)
    if os.path.isfile(p) and os.path.getsize(p) > 800:
        return True
    try:
        req = urllib.request.Request(u, headers={"User-Agent": clip_article.UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read(4 * 1024 * 1024)
        if len(raw) > 800:
            os.makedirs(COVER_DIR, exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "wb") as f:
                f.write(raw)
            os.replace(tmp, p)
            return True
    except Exception:
        pass
    return False


_FEED_SUMMARY = {"k": None, "v": None}


def feed_summary():
    """书架上那句「订阅 N 源 · 未读 M 条」。

    /api/state 是轮询的，按 (mtime, size) 缓一份：feed.json 里可能躺着几千条，
    每两秒重解析一遍纯属白烧 CPU。
    """
    try:
        st = os.stat(feed_mod.STORE)
    except OSError:
        return {"subs": 0, "unread": 0}
    key = (int(st.st_mtime), st.st_size)
    if _FEED_SUMMARY["k"] == key and _FEED_SUMMARY["v"] is not None:
        return _FEED_SUMMARY["v"]
    out = {"subs": 0, "unread": 0}
    try:
        rows = feed_mod.subs()
        out = {"subs": len(rows),
               "unread": sum(int(r.get("unread") or 0) for r in rows)}
    except Exception:
        pass
    _FEED_SUMMARY.update({"k": key, "v": out})
    return out


_VIDEO_AVAIL = {"at": 0.0, "v": None}
# 上一次问「转写组件在忙吗」得到的答案。只在「忙 → 闲」的那一刻才去作废 available 的
# 缓存，平时一直闲就不动它（动一下就是一次大包 import）。
_MEDIA_BUSY = {"v": False}


def video_avail_forget():
    """把那份 30 秒的缓存作废。装完组件、下完模型之后必须调一次 ——
    否则用户点完「补齐组件」，那几盏灯还要再绿不绿半分钟，看着像没生效。"""
    _VIDEO_AVAIL.update({"at": 0.0, "v": None})


def video_avail():
    """视频这条线现在能走到哪一步（下载器 / ffmpeg / 本地或云 ASR / LLM / 模型）。

    available() 要试 import 几个大包（mlx_whisper 那类），一次几十毫秒；状态栏是
    轮询的，所以缓存 30 秒 —— 用户刚装完组件时最迟半分钟就能看到绿灯。
    """
    now = time.time()
    if _VIDEO_AVAIL["v"] is not None and now - _VIDEO_AVAIL["at"] < 30:
        return _VIDEO_AVAIL["v"]
    try:
        v = video_note.available()
    except Exception:
        v = {"ytdlp": False, "ffmpeg": False, "asr": {"local": False, "cloud": True},
             "llm": False, "engines": []}
    # 引擎/模型准备到哪一步了（装引擎、后台下模型都走 media_setup）。它自带锁和
    # 一次目录扫描，几十毫秒量级，跟 available() 一起缓在同一个 30 秒里。
    try:
        v["setup"] = media_setup.status()
    except Exception:
        pass
    _VIDEO_AVAIL.update({"at": now, "v": v})
    return v


_FEED_RF = {"lock": threading.Lock(), "run": False, "stop": None,
            "p": {"running": False, "done": 0, "total": 0, "new": 0,
                  "note": "", "errors": [], "at": "", "scope": ""}}


def _feed_now():
    return time.strftime("%H:%M:%S")


def feed_refresh_status():
    """给界面轮询的进度。刷新是批量网络活，几百个源能跑一分钟起步，
    塞在一个 POST 里等它返就只能够转圈、也没法中途停 —— 所以放后台线程，
    这边只报状态。"""
    with _FEED_RF["lock"]:
        out = dict(_FEED_RF["p"])
        out["running"] = _FEED_RF["run"]
        out["errors"] = list(out.get("errors") or [])[:5]
        return out


def feed_refresh_start(scope):
    """scope 是 {feed_id, group, force, fulltext}；一次只许跑一轮。"""
    with _FEED_RF["lock"]:
        if _FEED_RF["run"]:
            return {"ok": False, "msg": "正在刷新，先等它跑完（或点停止）",
                    "progress": feed_refresh_status()}
        stop = threading.Event()
        _FEED_RF["stop"] = stop
        _FEED_RF["run"] = True
        _FEED_RF["p"] = {"running": True, "done": 0, "total": 0, "new": 0,
                         "note": "开始刷新…", "errors": [], "at": _feed_now(),
                         "scope": scope.get("label") or ""}

    def prog(done, total, label=""):
        with _FEED_RF["lock"]:
            _FEED_RF["p"].update({"done": int(done or 0), "total": int(total or 0),
                                  "note": str(label)[:160], "at": _feed_now()})

    def job():
        r = {}
        try:
            r = feed_mod.refresh(feed_id=scope.get("feed_id"),
                                 force=bool(scope.get("force")),
                                 group=scope.get("group"),
                                 progress=prog,
                                 should_stop=stop.is_set,
                                 fulltext=scope.get("fulltext"))
            _FEED_RF["p"].update({"new": int(r.get("new") or 0),
                                  "errors": list(r.get("errors") or []),
                                  "note": ("已停止" if r.get("stopped")
                                           else "刷完了"),
                                  "at": _feed_now()})
            log("订阅刷新%s：跑了 %d 个源，新增 %d 条"
                % ("中止" if r.get("stopped") else "完成",
                   r.get("feeds", 0), r.get("new", 0)))
        except Exception as e:
            _FEED_RF["p"].update({"note": "刷新出错：%s" % str(e)[:120],
                                  "at": _feed_now()})
            log("--- 订阅刷新失败：%s: %s ---" % (type(e).__name__, e))
        finally:
            _FEED_SUMMARY["k"] = None
            with _FEED_RF["lock"]:
                _FEED_RF["run"] = False
                _FEED_RF["stop"] = None

    threading.Thread(target=job, daemon=True).start()
    return {"ok": True, "started": True, "msg": "开始刷新了",
            "progress": feed_refresh_status()}


def feed_refresh_stop():
    with _FEED_RF["lock"]:
        stop = _FEED_RF["stop"]
        if not _FEED_RF["run"] or stop is None:
            return {"ok": True, "msg": "这会儿没在刷新"}
        stop.set()
    return {"ok": True, "msg": "正在收尾，这一轮跑完当前那个源就停"}


def _tri(raw):
    """三态筛选：没传 → None（这个条件不参与），传 1/true → 是，其余 → 否。
    收藏和稍后读都得能筛「有」和「没有」两种，二元开关筛不出后者。"""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in ("", "all", "any", "any^"):
        return None
    return s in ("1", "true", "yes", "on")


def feed_view(q):
    """GET /api/feed?mode=… —— 只读，自己不抛（抛了前端就是「点了没反应」）。"""
    mode = (q.get("mode", ["list"])[0] or "list").strip()

    def one(key, default=""):
        return (q.get(key, [default])[0] or default)

    try:
        if mode == "entry":
            return {"ok": True, "entry": feed_mod.entry(one("id"))}
        if mode == "entries":
            # 「未分组」在库里存的是空串，所以这几个键要看「传没传」而不是「空不空」，
            # 否则用户点侧栏那个「未分组」就会筛成「不限」。
            kw = dict(
                feed_id=(one("feed") or None),
                unread_only=one("unread") in ("1", "true"),
                q=one("q"),
                starred=_tri(q.get("starred", [None])[0]),
                later=_tri(q.get("later", [None])[0]),
                unread=_tri(q.get("unread2", [None])[0]),
                group=(q.get("group", [None])[0] if "group" in q else None),
                tag=(q.get("tag", [None])[0] if "tag" in q else None),
                tag_match=one("tag_match", "any"),
                since=(one("since") or None),
                until=(one("until") or None),
                sort=one("sort", "published_desc"))
            out = feed_mod.entries(
                limit=max(1, min(500, int(q.get("limit", ["200"])[0] or 200))),
                offset=max(0, int(q.get("offset", ["0"])[0] or 0)), **kw)
            # total 用同一套筛法数一遍：前端「这一档共 N 条 · 已铺 M 条」要说得出
            # 还剩多少，让用户知道「再来一批」还有没有东西，而不是点了没反应。
            return {"ok": True, "entries": out, "total": feed_mod.count_entries(**kw)}
        if mode == "summary":
            return {"ok": True, "summary": feed_mod.summary()}
        if mode == "groups":
            return {"ok": True, "groups": feed_mod.groups(),
                    "subs": feed_mod.subs(order="manual")}
        if mode == "tags":
            return {"ok": True, "tags": feed_mod.tag_list()}
        if mode == "settings":
            return {"ok": True, "settings": feed_mod.settings(),
                    "fulltext": feed_mod.fulltext_enabled()}
        if mode == "snapshots":
            return {"ok": True, "snapshots": feed_mod.snapshots()}
        if mode == "refresh_status":
            return {"ok": True, "progress": feed_refresh_status()}
        if mode == "opml":
            return {"ok": True, "xml": feed_mod.export_opml(
                include_empty=one("empty", "1") in ("1", "true"))}
        return {"ok": True, "subs": feed_mod.subs(), "summary": feed_summary(),
                "reader": feed_mod.summary(), "groups": feed_mod.groups(),
                "tags": feed_mod.tag_list(), "settings": feed_mod.settings(),
                "fulltext": feed_mod.fulltext_enabled(),
                "refresh": feed_refresh_status(),
                # 撤销是「这一步做错了」唯一的退路，前端要在铺列表时就知道有没有得撤，
                # 为此多开一次 mode=snapshots 不值得（快照本来就在内存里）。
                "snapshots": feed_mod.snapshots()}
    except Exception as e:
        return {"ok": False, "msg": "读订阅出错：%s" % str(e)[:140]}


def _feed_ids(body, key="ids"):
    v = body.get(key)
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()][:500]
    one = str(body.get("id") or "").strip()
    return [one] if one else []


def _feed_tags(body, key="tags"):
    v = body.get(key)
    if isinstance(v, str):
        v = [x for x in re.split(r"[,，\s]+", v) if x]
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if str(x).strip()][:12]


def feed_do(body):
    """POST /api/feed —— 订阅管理 + 阅读动作。界面里的每个按钮都对应这里一个 act。"""
    act = str((body or {}).get("act") or "").strip()
    try:
        if act == "discover":
            return {"ok": True, "cands": feed_mod.discover(str(body.get("url") or ""))}
        if act == "add":
            f = feed_mod.add(str(body.get("url") or ""),
                             folder=str(body.get("folder") or "")[:40],
                             group=(str(body.get("group"))[:40]
                                    if body.get("group") is not None else None))
            _FEED_SUMMARY["k"] = None
            return {"ok": True, "feed": f, "subs": feed_mod.subs(),
                    "msg": "已订阅《%s》" % (f.get("title") or f.get("url"))}
        if act == "remove":
            fid = str(body.get("id") or "")
            feed_mod.snapshot("退订前")
            ok = feed_mod.remove(fid)
            _FEED_SUMMARY["k"] = None
            return {"ok": ok, "subs": feed_mod.subs(),
                    "undo": True,
                    "msg": ("已退订，可以撤销" if ok else "没找到这个订阅源")}
        if act == "mark":
            fid = str(body.get("id") or "")
            if body.get("drop"):
                feed_mod.snapshot("退订前")
                feed_mod.remove(fid)
                title = None
            else:
                title = body.get("title")
            if (body.get("folder") is not None or title is not None
                    or body.get("group") is not None):
                feed_mod.mark(fid, title=(str(title).strip()[:80] if title is not None else None),
                              folder=(str(body.get("folder"))[:40]
                                      if body.get("folder") is not None else None),
                              group=(str(body.get("group"))[:40]
                                     if body.get("group") is not None else None))
            _FEED_SUMMARY["k"] = None
            return {"ok": True, "subs": feed_mod.subs(), "msg": "已更新"}
        if act == "refresh":
            scope = {"feed_id": (str(body.get("id") or "") or None),
                     "group": (str(body.get("group")) if body.get("group") is not None else None),
                     "force": bool(body.get("force")),
                     "fulltext": (bool(body["fulltext"]) if "fulltext" in body else None)}
            scope["label"] = ("这一组" if scope["group"] else
                              ("这个源" if scope["feed_id"] else "全部"))
            return feed_refresh_start(scope)
        if act == "refresh_stop":
            return feed_refresh_stop()
        if act == "refresh_status":
            return {"ok": True, "progress": feed_refresh_status()}
        if act == "read":
            ids = _feed_ids(body)
            if not ids:
                return {"ok": False, "msg": "没说要标哪几条"}
            n = feed_mod.mark_read(ids, read=bool(body.get("read", True)))
            _FEED_SUMMARY["k"] = None
            return {"ok": True, "n": n, "msg": "已标 %d 条" % n}
        if act == "read_all":
            r = feed_mod.mark_read_all(feed_id=(str(body.get("id") or "") or None),
                                       group=(str(body.get("group"))
                                              if body.get("group") is not None else None),
                                       read=bool(body.get("read", True)),
                                       older_than=body.get("older_than"))
            _FEED_SUMMARY["k"] = None
            return {"ok": True, "n": r.get("n", 0), "undo_token": r.get("undo_token"),
                    "msg": "已标记 %d 条，可以撤销" % int(r.get("n") or 0)}
        if act == "star":
            ids = _feed_ids(body)
            if not ids:
                return {"ok": False, "msg": "没说要收藏哪几条"}
            n = feed_mod.mark_star(ids, starred=bool(body.get("on", True)))
            _FEED_SUMMARY["k"] = None
            return {"ok": True, "n": n,
                    "msg": ("已收藏 %d 条" % n) if body.get("on", True) else ("已取消收藏 %d 条" % n)}
        if act == "star_toggle":
            out = feed_mod.toggle_star(str(body.get("id") or "")) or {}
            _FEED_SUMMARY["k"] = None
            on = bool(out.get("starred"))
            return {"ok": True, "starred": on,
                    "msg": "已收藏" if on else "已取消收藏"}
        if act == "later":
            ids = _feed_ids(body)
            if not ids:
                return {"ok": False, "msg": "没说要放哪几条"}
            n = feed_mod.mark_later(ids, later=bool(body.get("on", True)))
            _FEED_SUMMARY["k"] = None
            return {"ok": True, "n": n,
                    "msg": ("已放进稍后读 %d 条" % n) if body.get("on", True)
                    else ("已从稍后读取出 %d 条" % n)}
        if act == "later_toggle":
            out = feed_mod.toggle_later(str(body.get("id") or "")) or {}
            _FEED_SUMMARY["k"] = None
            on = bool(out.get("later"))
            return {"ok": True, "later": on,
                    "msg": "已放进稍后读" if on else "已从稍后读取出"}
        if act == "tag_add":
            ids = _feed_ids(body)
            tags = _feed_tags(body)
            if not ids or not tags:
                return {"ok": False, "msg": "要给哪几条挂哪个标签，没说清"}
            n = feed_mod.tag_add(ids, tags)
            _FEED_SUMMARY["k"] = None
            return {"ok": True, "n": n, "tags": feed_mod.tag_list(),
                    "msg": "已给 %d 条挂上标签" % n}
        if act == "tag_remove":
            ids = _feed_ids(body)
            n = feed_mod.tag_remove(ids, tags=_feed_tags(body) or None)
            _FEED_SUMMARY["k"] = None
            return {"ok": True, "n": n, "tags": feed_mod.tag_list(),
                    "msg": "已去掉标签"}
        if act == "tag_set":
            eid = str(body.get("id") or "")
            if not eid:
                return {"ok": False, "msg": "没说要改哪一条"}
            tags = feed_mod.tag_set(eid, _feed_tags(body))
            _FEED_SUMMARY["k"] = None
            return {"ok": True, "tags": tags, "all": feed_mod.tag_list(), "msg": "已改"}
        if act == "delete":
            ids = _feed_ids(body)
            if not ids:
                return {"ok": False, "msg": "没说要删哪几条"}
            r = feed_mod.delete_entries(ids, purge_seen=bool(body.get("purge_seen")),
                                        undo=True)
            _FEED_SUMMARY["k"] = None
            n = r.get("n") if isinstance(r, dict) else r
            return {"ok": True, "n": n, "undo_token": (r or {}).get("undo_token"),
                    "msg": "已删 %s 条，可以撤销" % (n if n is not None else len(ids))}
        if act == "undo":
            r = feed_mod.undo(str(body.get("token") or "") or None)
            r = r if isinstance(r, dict) else {}
            _FEED_SUMMARY["k"] = None
            return {"ok": bool(r.get("ok")), "restored": r,
                    "subs": feed_mod.subs(), "snapshots": feed_mod.snapshots(),
                    "msg": ("已撤销%s" % (("：" + r["label"]) if r.get("label") else ""))
                    if r.get("ok") else "没得撤销了（撤销只记这一次开着程序的几步）"}
        if act == "snapshots":
            return {"ok": True, "snapshots": feed_mod.snapshots()}
        if act == "group_create":
            return {"ok": True, "groups": feed_mod.group_create(
                str(body.get("name") or ""))}
        if act == "group_rename":
            return {"ok": True, "groups": feed_mod.group_rename(
                str(body.get("old") or ""), str(body.get("name") or ""))}
        if act == "group_delete":
            return {"ok": True, "groups": feed_mod.group_delete(
                str(body.get("name") or ""), ungroup=not body.get("drop_feeds"))}
        if act == "reorder":
            feed_mod.reorder(str(body.get("id") or ""),
                             index=(int(body["index"]) if body.get("index") is not None else None),
                             after=(str(body.get("after")) if body.get("after") else None))
            return {"ok": True, "subs": feed_mod.subs(), "msg": "顺序已改"}
        if act == "fetch_content":
            r = feed_mod.fetch_content(entry_ids=_feed_ids(body) or None,
                                       feed_id=(str(body.get("id") or "") or None),
                                       group=(str(body.get("group"))
                                              if body.get("group") is not None else None),
                                       only_missing=bool(body.get("only_missing", True)),
                                       limit=max(1, min(200, int(body.get("limit") or 50))))
            _FEED_SUMMARY["k"] = None
            r = r if isinstance(r, dict) else {}
            return {"ok": True, "result": r,
                    "msg": "补全了 %s 条，%s 条没补上，另有 %s 条本来就有全文"
                           % (r.get("n", 0), len(r.get("failed") or []),
                              r.get("skipped", 0))}
        if act == "fulltext":
            on = bool(body.get("on"))
            feed_mod.set_fulltext(on)
            return {"ok": True, "fulltext": on,
                    "msg": "以后刷新会回源补全文" if on else "以后刷新只用源给的内容"}
        if act == "settings":
            s = feed_mod.set_settings(fulltext=(bool(body["fulltext"]) if "fulltext" in body else None),
                                      policy=body.get("policy")
                                      if isinstance(body.get("policy"), dict) else None)
            return {"ok": True, "settings": s, "msg": "偏好已存"}
        if act == "prune":
            feed_mod.snapshot("清理旧条目前")
            r = feed_mod.prune(days=(int(body["days"]) if body.get("days") else None),
                               max_entries=(int(body["max_entries"])
                                            if body.get("max_entries") else None),
                               keep_starred=bool(body.get("keep_starred", True)),
                               feed_id=(str(body.get("id") or "") or None),
                               group=(str(body.get("group"))
                                      if body.get("group") is not None else None),
                               dry_run=bool(body.get("dry_run")))
            _FEED_SUMMARY["k"] = None
            r = r if isinstance(r, dict) else {}
            return {"ok": True, "result": r, "undo_token": r.get("undo_token"),
                    "msg": r.get("note") or
                           ("预览：会清掉 %s 条" % r.get("removed", 0) if body.get("dry_run")
                            else "已清掉 %s 条，可以撤销" % r.get("removed", 0))}
        if act == "opml_write":
            p = feed_mod.write_opml(path=(str(body.get("path") or "") or None),
                                    title=str(body.get("title") or "归藏订阅"))
            return {"ok": bool(p), "path": p or "",
                    "msg": ("已导出 OPML：%s" % p) if p else "OPML 没写出去"}
        if act == "opml_import":
            xml = str(body.get("xml") or "")
            if not xml.strip():
                return {"ok": False, "msg": "OPML 内容是空的"}
            r = feed_mod.import_opml(xml, default_group=str(body.get("group") or ""),
                                     dry_run=bool(body.get("dry_run")))
            _FEED_SUMMARY["k"] = None
            return {"ok": True, "result": r, "subs": feed_mod.subs(),
                    "msg": ("预览：新订 %s 个，已有 %s 个，失败 %s 个"
                            % (r.get("added", 0), r.get("existed", 0),
                               len(r.get("failed") or [])))}
        if act == "shelf":
            eid = str(body.get("id") or "")
            info = feed_mod.to_shelf(eid, module_dir("feed"))
            cache_cover(info["id"], (book_meta(info["id"]) or {}).get("cover") or "")
            log(f"订阅入库：{info['title']}（{info.get('site') or ''}，"
                f"{info.get('chars') or 0} 字）")
            _FEED_SUMMARY["k"] = None
            return {"ok": True, "book": info,
                    "msg": "已收进书架：《%s》" % info["title"]}
        return {"ok": False, "msg": "订阅这块没说要干什么"}
    except Exception as e:
        msg = str(e)[:180] or "订阅操作没做成"
        log(f"--- 订阅失败（{act}）：{msg} ---")
        return {"ok": False, "msg": msg}


def _vnum(v):
    """meta 里的数字收成 int，脏值当 0 —— 清单只是展示用，不该因为一个怪值就整栏崩掉。"""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return 0
    return int(n) if n == n else 0


def _video_exports(book_dir):
    """这本书已经导出过哪些转写文件（exports/ 下）：名字、格式、多大、什么时候。

    列给界面用，让用户看得见「上次导的是哪一份」，而不是每次点导出都盲写一个文件。
    """
    holder = os.path.join(book_dir, video_note.EXPORT_DIR)
    out = []
    if not os.path.isdir(holder):
        return out
    for name in sorted(os.listdir(holder)):
        p = os.path.join(holder, name)
        if not os.path.isfile(p):
            continue
        ext = os.path.splitext(name)[1].lstrip(".").lower()
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({"name": name, "fmt": ext, "bytes": st.st_size,
                    "at": int(st.st_mtime)})
    return out


def _count_chapter_files(book_dir):
    """数一数 chapters/ 下有几个 .md —— meta 没记节数时的兜底，和 local_books 同一套数法。"""
    holder = os.path.join(book_dir, "chapters")
    if not os.path.isdir(holder):
        return 0
    return sum(1 for f in os.listdir(holder) if f.endswith(".md"))


def video_books():
    """转出来的视频书，连各自转写的状态一起回 —— 工作台那一栏靠这个挑书。

    只读 meta.json 里已有的字段，不去解析 transcript.json 那份大文件：清单要的是
    「有没有带时间戳的转写、多少段、重建过没有」，全文等用户点进某一本书再说。
    """
    out = []
    for name, d, mod in book_layout.walk(OUT_DIR):
        if mod != "video":
            continue
        m = book_meta(name)
        if (m.get("source") or "") != "video":
            continue
        try:
            upd = float(m.get("updated_at") or 0)
        except (TypeError, ValueError):
            upd = 0.0
        out.append({
            "id": name,
            "title": m.get("page_title") or m.get("title") or name,
            "book_title": m.get("title") or "",
            "uploader": m.get("uploader") or "", "site": m.get("site") or "",
            "url": m.get("url") or "", "cover": m.get("cover") or "",
            "duration": _vnum(m.get("duration")), "words": _vnum(m.get("words")),
            # 节数优先信 meta；老书 / 中途改过名的书 meta 里可能是 0，而章节就在 chapters/
            # 下摆着 —— 数一眼目录比在界面上写「0 节」诚实，那会让人以为这本没分出章。
            "chapters": _vnum(m.get("chapters")) or _count_chapter_files(d),
            "segments": _vnum(m.get("segments")),
            "page": _vnum(m.get("page")) or 1, "pages": _vnum(m.get("pages")) or 1,
            "engine": m.get("asr_engine") or "", "language": m.get("language") or "",
            "edited": bool(m.get("transcript_edited")),
            "ai_error": m.get("ai_error") or "",
            "has_json": os.path.isfile(os.path.join(d, video_note.TRANS_FILE)),
            "has_txt": os.path.isfile(os.path.join(d, video_note.TRANS_TXT)),
            "exports": _video_exports(d), "updated_at": upd,
        })
    out.sort(key=lambda x: -(x["updated_at"] or 0))
    return out


def video_export_file(book_id, name):
    """<书>/exports/<name> → (绝对路径, MIME)。名字不对、目录越界都回 None。

    导出物是转写本身（字幕 / 纯文本 / Markdown / JSON），同域打开不会跑脚本，
    但还是按 board_file 那套白名单来：只认 exports/ 这一层里的文件，
    扩展名限定在已知的那五种，`..` 和子目录一概进不来。
    """
    d = safe_book_dir(book_id)
    if not d or not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    ext = os.path.splitext(name)[1].lstrip(".").lower()
    if ext not in video_note.EXPORT_EXT:
        return None
    root = os.path.realpath(os.path.join(d, video_note.EXPORT_DIR))
    p = os.path.realpath(os.path.join(root, name))
    if not p.startswith(root + os.sep) or not os.path.isfile(p):
        return None
    mime = {"srt": "application/x-subrip", "vtt": "text/vtt; charset=utf-8",
            "txt": "text/plain; charset=utf-8", "md": "text/markdown; charset=utf-8",
            "json": "application/json; charset=utf-8"}.get(ext) or "application/octet-stream"
    return p, mime


# 一次回给前端的段落上限：一小时的视频约几百段，真整本长文（几小时直播）一次全吐
# 会把浏览器卡住。切在这个数上，并把「还有多少」如实报出去，界面好写「再来一屏」。
VIDEO_SEG_PAGE = 1200


def video_view(q):
    """GET /api/video?mode=… —— 认链接 / 报可用性 / 列转出来的书 / 读一本的转写。

    全部只读。抛出去的东西前端只会看到「点了没反应」，所以这里每个分支都自己兜住，
    把人话放进 msg。
    """
    mode = (q.get("mode", ["status"])[0] or "status").strip()

    def one(key, default=""):
        return (q.get(key, [default])[0] or default)

    try:
        if mode == "plan":
            return {"ok": True, "plan": video_note.plan(one("url"))}
        if mode == "books":
            return {"ok": True, "books": video_books()}
        if mode == "transcript":
            d = safe_book_dir(one("book"))
            if not d:
                return {"ok": False, "msg": "没找到这本书，或它不在书库里"}
            raw_from, raw_to = one("from"), one("to")
            frm = video_note.parse_stamp(raw_from)
            to = video_note.parse_stamp(raw_to)
            if (raw_from.strip() and frm is None) or (raw_to.strip() and to is None):
                return {"ok": False, "msg": "时间没看懂：写 1:23 这样的，或直接写秒数"}
            offset = max(0, _vnum(one("offset")))
            # 前端可以只要一小屏（界面按 240 段一屏铺，一次建两千个节点会卡住滚动）；
            # 上限仍是 VIDEO_SEG_PAGE，谁也不能靠这个参数把整本几万字一次拖走。
            limit = _vnum(one("limit")) or VIDEO_SEG_PAGE
            limit = max(1, min(VIDEO_SEG_PAGE, limit))
            try:
                t = video_note.load_transcript(d, keyword=one("q"), start=frm, end=to)
            except ValueError as e:
                return {"ok": False, "msg": str(e)[:180]}
            rows = t.pop("segments", [])
            # 全文（整本转写能到几十万字）和段落是同一份东西的两种写法，跟着筛出来的段落
            # 一起发会让人误解一次，还白占一大块带宽。界面上要全文的地方（重建、导出）
            # 都在后端自己做，这里一条都不发 —— 不弹掉的话上面那句注释就只是愿望。
            t.pop("text", None)
            page = rows[offset:offset + limit]
            t.update({"segments": page, "offset": offset,
                      "returned": len(page), "has_more": offset + len(page) < len(rows)})
            return {"ok": True, "transcript": t}
        return {"ok": True, "available": video_avail()}
    except Exception as e:
        msg = str(e)[:180] or "视频这块没读到东西"
        log(f"--- 视频查询失败（{mode}）：{type(e).__name__}: {msg} ---")
        return {"ok": False, "msg": msg}


def video_do(body):
    """POST /api/video —— 起转写任务 / 下 ffmpeg / 存改过的转写 / 重建 / 导字幕。"""
    act = str((body or {}).get("act") or "start").strip()

    def book_dir():
        d = safe_book_dir(str((body or {}).get("book") or "").strip())
        return d or None

    try:
        if act == "ffmpeg":
            notes = []
            env_extra = pc.proxy_env(note=notes)
            for n in notes:
                log(f"      · {n}")
            ok, msg = start_task("ffmpeg", [py(), script("ffmpeg_tool.py"), "--ensure"],
                                 env_extra=env_extra)
            return {"ok": ok, "msg": msg}
        if act == "stop":
            ok, msg = stop_task()
            return {"ok": ok, "msg": msg}
        if act == "save_transcript":
            d = book_dir()
            if not d:
                return {"ok": False, "msg": "没找到这本书，转写没处存"}
            segs = (body or {}).get("segments")
            if not isinstance(segs, (list, tuple)):
                return {"ok": False, "msg": "转写段落得是一个列表，这次没存"}
            try:
                r = video_note.save_transcript(
                    d, segs, engine=str(body.get("engine") or "") or None,
                    language=str(body.get("language") or "") or None)
            except ValueError as e:
                return {"ok": False, "msg": str(e)[:180]}
            return {"ok": True, "msg": r["msg"], "saved": r}
        if act == "rebuild":
            d = book_dir()
            if not d:
                return {"ok": False, "msg": "没找到这本书，重建不了"}
            try:
                r = video_note.rebuild_book(d)
            except ValueError as e:
                return {"ok": False, "msg": str(e)[:180]}
            log(f"--- 视频书重建：{os.path.basename(d)} → {r['chapters']} 节 · 旧文件进 {r['backup']} ---")
            return {"ok": True, "msg": r["msg"], "rebuilt": r}
        if act == "export":
            d = book_dir()
            if not d:
                return {"ok": False, "msg": "没找到这本书，导不出东西"}
            fmt = str((body or {}).get("fmt") or "srt").strip().lower()
            try:
                r = video_note.export_transcript(d, fmt)
            except ValueError as e:
                return {"ok": False, "msg": str(e)[:180]}
            r["downloads"] = _video_exports(d)
            return {"ok": True, "msg": "导好了：%(name)s" % r, "export": r}
        if act in ("media_status", "media_engine", "media_model", "media_all"):
            if act != "media_status":
                kind = {"media_engine": "engine", "media_model": "model", "media_all": "auto"}[act]
                ok, msg = media_setup.start(kind)
                if not ok:
                    return {"ok": False, "msg": msg, "setup": media_setup.status()}
            # 状态现取（不走那个 30 秒缓存）：下载百分比要跳着往上走，缓存了就成了定格照。
            st = media_setup.status()
            # 刚好从「忙」变「闲」的那一帧，把 available 那份缓存作废 —— 装完引擎、
            # 下完模型，那几盏灯必须立刻变绿，而不是再等半分钟。平时（一直闲）不动它，
            # 否则每次轮询都要重新 import 那几个大包。
            busy = bool(st.get("busy"))
            if _MEDIA_BUSY["v"] and not busy:
                video_avail_forget()
            _MEDIA_BUSY["v"] = busy
            return {"ok": True, "msg": st.get("note") or "已开始准备", "setup": st,
                    "available": video_avail()}
        url = str(body.get("url") or "").strip()
        if not url:
            return {"ok": False, "msg": "先把视频链接粘进来"}
        prog = video_avail()
        if not prog.get("ytdlp"):
            return {"ok": False, "msg": "还没装下载器（yt-dlp）：在设置里点一下「补齐组件」"}
        asr = body.get("asr") if isinstance(body.get("asr"), dict) else {}
        opts = {"asr": {k: v for k, v in asr.items() if v not in (None, "")},
                "language": str(body.get("language") or "").strip()[:8]}
        ok, msg = start_task(
            "video", [py(), script("video_note.py"), "--task", url],
            env_extra=task_env("video",
                               {"GUIZANG_VIDEO_OPTS": json.dumps(opts, ensure_ascii=False)}))
        return {"ok": ok, "msg": msg}
    except Exception as e:
        log(f"--- 视频任务失败（{act}）：{type(e).__name__}: {e} ---")
        return {"ok": False, "msg": "这个操作没做成：%s" % str(e)[:140]}


# ---------- flomo 便签 ----------

_FLOMO_EMPTY = {"memos": 0, "days": 0, "first": "", "last": "", "words": 0,
                "tags": 0, "images": 0, "top_tags": [], "busiest": []}
_FLOMO_STATS = {"k": None, "v": None}


def flomo_stats():
    """导入进来的骨架数字（多少条、多少天、多少个标签）。

    和 feed_summary 一个理由：/api/state 两秒轮一次，几百上千条笔记的账不该每次都
    重算，按 (mtime, size) 缓一份。数字里本来就不含笔记正文，写日志、做记忆画像
    都安全（用户明确要求个人信息不许留在源码与日志里）。
    """
    try:
        st = os.stat(flomo_notes.path_in(FLOMO_DIR))
    except OSError:
        return dict(_FLOMO_EMPTY, at=0)
    key = (int(st.st_mtime), st.st_size)
    if _FLOMO_STATS["k"] == key and _FLOMO_STATS["v"] is not None:
        return _FLOMO_STATS["v"]
    try:
        out = flomo_notes.summary_stats(flomo_notes.load(FLOMO_DIR))
    except Exception:
        out = dict(_FLOMO_EMPTY)
    out["at"] = int(st.st_mtime)
    _FLOMO_STATS["k"], _FLOMO_STATS["v"] = key, out
    return out


def flomo_att_path(name):
    """附件名 → 本地路径。只认 cache/flomo/att 那一层里的真实文件。

    界面里 `<img src=/api/flomo/att/xxx.jpg>` 拼的是导入时算好的哈希名，理论上没有
    路径符号；但这是读磁盘的口子，还是按 board_file 那套来：字符白名单 + realpath
    前缀校验，`..` 和子目录一概进不来。
    """
    if not name or not re.fullmatch(r"[A-Za-z0-9_.\-]{1,80}", str(name)):
        return None
    root = os.path.realpath(flomo_notes.att_dir(FLOMO_DIR))
    p = os.path.realpath(os.path.join(root, os.path.basename(str(name))))
    if not p.startswith(root + os.sep) or not os.path.isfile(p):
        return None
    return p


def flomo_view(q):
    """GET /api/flomo/notes?mode=… —— 时间线 / 单条 / 标签 / 画像，全部只读。"""
    mode = (q.get("mode", ["list"])[0] or "list").strip()

    def one(key, default=""):
        return (q.get(key, [default])[0] or default)

    try:
        if mode == "one":
            m = flomo_notes.one(flomo_notes.load(FLOMO_DIR), one("id"))
            return {"ok": True, "memo": flomo_notes.public(m) if m else None}
        if mode == "stats":
            return {"ok": True, "stats": flomo_stats()}
        if mode == "tags":
            data = flomo_notes.load(FLOMO_DIR)
            return {"ok": True, "tags": flomo_notes.tag_counts(data),
                    "recent": flomo_notes.recent_tags(data)}
        if mode == "portrait":
            # 给 Agent 的那一份「用户记忆画像」：只有数字与标签，没有一条原文。
            # 顺手落盘到 cache/flomo/portrait/ —— 用户要的「首次操作时生成、存在存储目录
            # 下的指定文件夹里」就在这一下发生，之后 Agent 每次做事前读的是同一份。
            data = flomo_notes.load(FLOMO_DIR)
            wrote = flomo_notes.write_portrait(FLOMO_DIR, data)
            p = wrote["portrait"]
            return {"ok": True, "portrait": p, "stats": flomo_stats(),
                    "md": flomo_notes.portrait_markdown(p),
                    "file": wrote["md"], "json": wrote["json"]}
        data = flomo_notes.load(FLOMO_DIR)
        picked = flomo_notes.pick(
            data, tag=one("tag"), q=one("q"),
            limit=max(1, min(500, int(one("limit", "80") or 80))),
            offset=max(0, int(one("offset", "0") or 0)),
            order=one("order", "desc"))
        return {"ok": True, "memos": picked["memos"], "total": picked["total"],
                "offset": picked["offset"], "count": picked["count"],
                "stats": flomo_stats(), "tags": flomo_notes.tag_counts(data)}
    except Exception as e:
        return {"ok": False, "msg": "读便签出错：%s" % str(e)[:140]}


def flomo_do(body):
    """POST /api/flomo/notes —— 导入 / 收成书 / 忘条 / 清空 / 打标签。"""
    act = str((body or {}).get("act") or "").strip()
    try:
        if act == "import":
            raw = (body or {}).get("data") or ""
            name = str((body or {}).get("name") or "").strip()
            if not raw:
                return {"ok": False, "msg": "没有收到文件内容"}
            try:
                blob = base64.b64decode(raw, validate=False)
            except Exception:
                return {"ok": False, "msg": "文件内容解码失败"}
            # 2MB 的导出包 base64 后约 2.7MB；放到 60MB 是给「多年全量 + 一堆图」留余量，
            # 同时不至于让一个误传的整盘备份把内存吃掉。
            if len(blob) > 60 * 1024 * 1024:
                return {"ok": False, "msg": "这份文件太大了（60MB 封顶），按时间分批导吧"}
            res = flomo_notes.import_notes(FLOMO_DIR, blob, name, log)
            _FLOMO_STATS["k"] = None
            log("--- flomo 导入：%d 条（新 %d · 图 %d）---"
                % (res["total"], res["added"], res["atts"]))
            return dict({"ok": True, "stats": flomo_stats()}, **res)
        if act == "shelf":
            mid = str(body.get("id") or "")
            if not mid:
                return {"ok": False, "msg": "没说要收哪一条"}
            info = flomo_notes.to_shelf(FLOMO_DIR, mid, module_dir("flomo"))
            # 便签的封面就是它自己那张图（用户要的「以视频封面/文章首图当封面」同理）；
            # 没图就不硬塞，让书架用生成式封面兜着。
            atts = flomo_notes.one(flomo_notes.load(FLOMO_DIR), mid)
            for nm in ((atts or {}).get("atts") or []):
                p = flomo_att_path(nm)
                if p:
                    try:
                        os.makedirs(COVER_DIR, exist_ok=True)
                        with open(p, "rb") as f:
                            raw = f.read(4 * 1024 * 1024)
                        if len(raw) > 800:
                            tmp = cover_path(info["id"]) + ".tmp"
                            with open(tmp, "wb") as f:
                                f.write(raw)
                            os.replace(tmp, cover_path(info["id"]))
                        break
                    except OSError:
                        pass
            msg = ("已收进书架：《%s》" % info.get("title")) if info.get("existed") \
                else ("已收成一本：《%s》" % info.get("title"))
            return {"ok": True, "book": info, "msg": msg, "memo": flomo_notes.public(atts)}
        if act == "tag":
            mid = str(body.get("id") or "")
            tag = str(body.get("tag") or "").strip()[:40].lstrip("#")
            if not (mid and tag):
                return {"ok": False, "msg": "要给哪条打哪个标签？"}
            data = flomo_notes.load(FLOMO_DIR)
            m = flomo_notes.one(data, mid)
            if not m:
                return {"ok": False, "msg": "没有这条笔记"}
            tags = list(m.get("tags") or [])
            remove = bool(body.get("remove"))
            if remove:
                tags = [t for t in tags if t != tag]
            elif tag not in tags:
                tags.append(tag)
            m["tags"] = tags
            flomo_notes.save(FLOMO_DIR, data)
            _FLOMO_STATS["k"] = None
            return {"ok": True, "tags": tags, "msg": "标签已更新"}
        if act == "forget":
            mid = str(body.get("id") or "")
            ok = flomo_notes.forget(FLOMO_DIR, mid)
            _FLOMO_STATS["k"] = None
            return {"ok": ok, "msg": "本机已忘掉这条（flomo 那边一个字没动）"
                    if ok else "没有这条笔记"}
        if act == "clear":
            n = flomo_notes.clear(FLOMO_DIR)
            _FLOMO_STATS["k"] = None
            log("--- flomo 便签已清空（%d 条）---" % n)
            return {"ok": True, "n": n, "msg": "已清掉本机导入的 %d 条" % n}
        return {"ok": False, "msg": "不认得这个操作：%s" % act[:30]}
    except Exception as e:
        msg = str(e)[:200]
        log("--- flomo 便签失败：%s ---" % msg)
        return {"ok": False, "msg": msg or "便签这事儿没成"}


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
        # keep_blank_values：空的 group= / tag= 是「未分组」「不限标签」这一档筛选，
        # 默认的 parse_qs 会把空值整个丢掉，键就不存在了 —— 前端点「未分组」于是筛成「不限」。
        q = parse_qs(u.query, keep_blank_values=True)
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
            # 每个模块一套自己的夹子 / 状态 / 标签（1.0.1 起）。顶层那两个字段
            # 保留 = 微信读书那一格，老的 MCP 适配器与「我的书架」还按老样子读。
            libs = {m: {"folders": lib_scope(lib, m)["folders"],
                        "states": lib_scope(lib, m)["state"],
                        "tags": lib_scope(lib, m)["tags"]}
                    for m in book_layout.ORDER}
            fm = flomo_stats()
            return self._json({
                "chromium": chromium_ready(),
                "login": read_login_state(),
                "task": task,
                "books": list_books(),
                "libs": libs,
                "modules": [{"id": m, "name": book_layout.DIR_OF[m]}
                            for m in book_layout.ORDER],
                "folders": lib_scope(lib, "weread")["folders"],
                # 状态标签可能打在还没抓取的书上，所以整张表也给前端
                "states": lib_scope(lib, "weread")["state"],
                "repo": REPO,
                "out": OUT_DIR,
                "version": VERSION,
                "weread": {"key_set": bool(weread_key()),
                           "key_tail": weread_key()[-4:] if weread_key() else ""},
                "flomo": {"url_set": bool(load_cfg().get("flomo_url")),
                          "tail": (load_cfg().get("flomo_url") or "")[-8:],
                          # 导入侧的计数（条数 / 天数 / 标签数 / 最后导入时间）。
                          # 只有数字，没有一条笔记原文 —— 这个字段是轮询的，日志和
                          # 界面都会读到，隐私口径按「清除本地数据」那条红线来。
                          "notes": fm["memos"],
                          "days": fm["days"],
                          "tags": fm["tags"],
                          "images": fm["images"],
                          "first": fm["first"],
                          "last": fm["last"],
                          "at": fm["at"]},
                "wallpaper": {"set": os.path.isfile(WALL_PATH),
                              "v": int(os.path.getmtime(WALL_PATH)) if os.path.isfile(WALL_PATH) else 0},
                "export": {"dir": export_dir()},
                "agent": agent_state(),
                "sync": sync_state(),
                # 订阅与视频这两条线的「有没有 / 能不能」——书目在 books 里已经有了
                "feed": feed_summary(),
                "video": {"available": video_avail(),
                          "asr": (load_cfg().get("asr_engine") or "auto"),
                          "lang": (load_cfg().get("asr_lang") or "")},
            })

        if path == "/api/feed":
            return self._json(feed_view(q))

        if path == "/api/flomo/notes":
            return self._json(flomo_view(q))

        if path.startswith("/api/flomo/att/"):
            p = flomo_att_path(unquote(path[len("/api/flomo/att/"):]))
            if not p:
                return self._send(404, "text/plain; charset=utf-8", "没有这张图")
            ext = os.path.splitext(p)[1].lstrip(".").lower()
            mime = {"png": "image/png", "gif": "image/gif", "webp": "image/webp",
                    "bmp": "image/bmp"}.get(ext) or "image/jpeg"
            with open(p, "rb") as f:
                # 文件名是内容哈希，改一个字就换个名字，所以可以放心长缓存：
                # 时间线往下翻时不必反复重读同一张图。
                return self._send(200, mime, f.read(),
                                  {"Cache-Control": "public, max-age=31536000"})

        if path == "/api/video":
            # mode=file 是「把导出的字幕 / 纯文本给我」这一种问法：它回的是字节，
            # 不是 JSON，所以不能整包交给 video_view（那只会吐一句「不支持的模式」）。
            if (q.get("mode", [""])[0] or "") == "file":
                hit = video_export_file(q.get("book", [""])[0], q.get("name", [""])[0])
                if not hit:
                    return self._send(404, "text/plain; charset=utf-8", "没有这个导出文件")
                fp, mime = hit
                return self._send(200, mime, open(fp, "rb").read(), {
                    "Content-Disposition": "attachment; filename*=UTF-8''%s"
                    % urllib.parse.quote(os.path.basename(fp)),
                    "Cache-Control": "no-store"})
            return self._json(video_view(q))

        if path == "/api/readstat":
            return self._json(readstat_summary())

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

        if path == "/api/mynotes":
            # 这本书的本地笔记：原文（含服务端补齐的 id / 时间戳）+ 那几支笔的定义。
            # 标签和颜色由后端给，前端不写死 —— 以后加一种颜色只改一处。
            book = q.get("book", [""])[0]
            d = safe_book_dir(book)
            if not d:
                return self._json({"ok": False, "msg": "没找到这本书"}, 404)
            meta = book_meta(book)
            doc = book_notes.load_notes(d)
            return self._json({
                "ok": True, "book": book,
                "title": meta.get("title") or book,
                "author": meta.get("author") or "",
                "source": meta.get("source") or "weread",
                "url": meta.get("url") or "",
                "doc": doc, "counts": book_notes.counts(doc),
                "tags": list(book_notes.TAGS), "kinds": list(book_notes.KINDS),
                "exported": os.path.isfile(os.path.join(d, book_notes.EXPORT_MD)),
                "mapped": os.path.isfile(os.path.join(d, book_notes.EXPORT_MAP)),
            })

        if path == "/api/mynotes/map":
            # 笔记 → 思维导图。顺手把 SVG 落进书目录（用户要「文件都在文件夹里」），
            # 再把图形文本回给前端画 <img>；图是我们自己生成的，不含外部脚本。
            book = q.get("book", [""])[0]
            d = safe_book_dir(book)
            if not d:
                return self._json({"ok": False, "msg": "没找到这本书"}, 404)
            cut = q.get("cut", ["tag"])[0]
            if cut not in ("tag", "chapter", "entry"):
                cut = "tag"
            meta = book_meta(book)
            try:
                res = book_notes.mindmap(d, book_notes.load_notes(d), meta=meta,
                                         titles=book_notes.chapter_titles(d),
                                         book_label=meta.get("title") or book, cut=cut)
            except Exception as e:
                return self._json({"ok": False, "msg": "思维导图没画出来：%s" % str(e)[:140]})
            try:
                with open(res["path"], encoding="utf-8") as f:
                    svg = f.read()
            except Exception:
                svg = ""
            res.update({"ok": True, "svg": svg, "cut": cut})
            return self._json(res)

        if path == "/api/note_tpl":
            return self._json({"ok": True,
                               "items": book_notes.templates(CACHE_DIR)})

        if path == "/api/board":
            # 画板：一块板一个 <id>.json，导出物 <id>.svg / <id>.png 同在 boards/ 里。
            # 三种问法：点名 name= 回文件本身（<img src> 要的字节），点名 id= 回那份画布，
            # 都不点就回这一页清单。画板是「边看书边画」那一侧的东西，一切落在书目录里。
            book = q.get("book", [""])[0]
            d = safe_book_dir(book)
            if not d:
                return self._json({"ok": False, "msg": "没找到这本书"}, 404)
            name = q.get("name", [""])[0]
            if name:
                hit = board_file(d, name)
                if not hit:
                    return self._send(404, "text/plain; charset=utf-8", "没有这个画板文件")
                fp, mime = hit
                head = {"Cache-Control": "public, max-age=300"}
                if mime.startswith("image/svg"):
                    # 这份 SVG 是从浏览器接过来的，不是我们生成的：同域打开它就可能跑脚本。
                    # 界面只把它当 <img> 用（img 里的脚本本来就不执行），这一条是给「有人直接把
                    # URL 敲进地址栏」兜底的：什么都别许，脚本、表单、跳转一概不许。
                    head["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
                    head["X-Content-Type-Options"] = "nosniff"
                with open(fp, "rb") as f:
                    return self._send(200, mime, f.read(), head)
            bid = q.get("id", [""])[0]
            if bid:
                doc = board_mod.load_board(d, bid)
                if doc is None:
                    return self._json({"ok": False, "msg": "没有这块画板", "id": bid}, 404)
                return self._json({"ok": True, "book": book, "board": doc})
            return self._json({"ok": True, "book": book,
                               "boards": board_mod.list_boards(d),
                               "count": board_mod.board_counts(d),
                               "dir": os.path.join(d, board_mod.BOARD_DIR),
                               "papers": list(board_mod.PAPERS),
                               "max_boards": board_mod.MAX_BOARDS})

        if path == "/api/ai/summary":
            # 上次存下来的那份（这本书的 _ai/）。重开页面不必再问一次模型 ——
            # 用户要的是「我读过的书都记着」，而不是每次刷新都重新生成一遍。
            book = q.get("book", [""])[0]
            d = safe_book_dir(book)
            if not d:
                return self._json({"ok": False, "msg": "没找到这本书"}, 404)
            meta = book_meta(book)
            module = ai_sum.module_of(meta, book)
            scope, ch = ai_sum.scope_for(module, q.get("chapter", [""])[0])
            style = q.get("style", ["brief"])[0]
            if style not in ai_sum.STYLE_KEYS:
                style = "brief"
            if not scope:
                return self._json({"ok": True, "text": "", "scope": "", "style": style})
            return self._json({"ok": True, "text": ai_sum.load_result(d, scope, style, ch),
                               "scope": scope, "style": style,
                               "chapter": ch, "module": module})

        if path == "/api/mindmap":
            # 这本书自己那张可编辑的脑图（mindmap.json 一个书目录一份）。
            # 布局必须在后端算：四种形态的排布是几十行几何，前端再写一遍就是两份真相，
            # 存回来的坐标和屏幕上看见的会越用越歪。前端只管画 boxes/edges 和回传编辑。
            book = q.get("book", [""])[0]
            d = safe_book_dir(book)
            if not d:
                return self._json({"ok": False, "msg": "没找到这本书"}, 404)
            doc = mindmap.load_map(d)
            form = q.get("form", [""])[0]
            mode = q.get("mode", [""])[0]
            if mode == "svg":
                # 「存一份图片」要的是能直接发给别人的单文件：后端出 SVG，前端只管下载。
                try:
                    return self._json({"ok": True,
                                       "svg": mindmap.render_svg(doc, form or None),
                                       "form": (form or doc["form"])})
                except Exception as e:
                    return self._json({"ok": False, "msg": "导图没画出来：%s" % str(e)[:140]})
            if mode == "md":
                return self._json({"ok": True, "md": mindmap.to_markdown(doc)})
            try:
                laid = mindmap.layout(doc, form or None)
            except Exception as e:
                return self._json({"ok": False, "msg": "布局算不出来：%s" % str(e)[:140]})
            return self._json({"ok": True, "book": book, "doc": doc, "layout": laid,
                               "forms": [{"key": k, "name": n} for k, n in mindmap.FORMS],
                               "limits": {"max_nodes": mindmap.MAX_NODES,
                                          "max_depth": mindmap.MAX_DEPTH,
                                          "max_links": mindmap.MAX_LINKS,
                                          "max_text": mindmap.MAX_TEXT}})

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
        # 所有 POST 接口都当 body 是个对象来 .get()。合法 JSON 也可能是数组/字符串/数字，
        # 这里统一收成空对象，省得每个分支各自判类型（畸形体不该把连接搞崩）。
        if not isinstance(body, dict):
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
                    module_dir("local"), name, blob,
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
            want = str((body or {}).get("module") or "").strip()
            lib = load_lib()
            n = 0
            for b in ids:
                # 状态只是 library.json 里的标签，不要求本地已抓取（书架上的书也能标）
                if not re.fullmatch(r"[A-Za-z0-9_\-]{4,}", str(b or "")):
                    continue
                # 落在哪一格：前端说了不算、目录也还没建，所以按书号前缀认；
                # 一次请求点名了模块（一屏里批量标）就以它为准。
                s = lib_scope(lib, want or book_module(str(b)))
                if st:
                    s["state"][b] = st
                else:
                    s["state"].pop(b, None)
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
                mod = book_layout.MODULE_OF.get(
                    os.path.basename(os.path.dirname(d)), "weread")
                drop_stray_md(b, title)      # 先清合并稿（还要靠这本书认自己住哪格）
                shutil.rmtree(d)
                lib_forget(lib, b, mod)
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

        if u.path == "/api/agent/quick":
            # 划词翻译 / 点词查义：一次问完、一次看全，所以走普通 JSON 不走流式。
            mode = ((body or {}).get("mode") or "translate").strip()
            if mode not in QUICK_SYS:
                mode = "translate"
            ok, out = agent_quick(mode, (body or {}).get("text"),
                                  (body or {}).get("ctx"))
            return self._json({"ok": ok, "text": out if ok else "", "msg": "" if ok else out})

        if u.path == "/api/ai/summary":
            # AI 小结：三种内容共用这一个口。范围（一章 / 整篇）由后端按来路定，
            # 前端只报「我在读哪一章」—— 让页面决定读多少，早晚变成页面替用户挑章节。
            book = str((body or {}).get("book") or "")
            style = str((body or {}).get("style") or "brief")
            if style not in ai_sum.STYLE_KEYS:
                style = "brief"
            ok, src = ai_source(book, (body or {}).get("chapter"))
            if not ok:
                return self._json({"ok": False, "msg": src})
            url, key, model = agent_cfg()
            if not url or not model:
                # 和聊天一样：一旦转成流就改不了状态码，这类「没配好」必须在前头挡掉。
                return self._json({"ok": False, "msg": "还没配 AI 接口：设置 → 读书小助手，填地址和模型名"})
            self._sse_start()
            sent = []

            def emit(piece):
                sent.append(piece)
                self._chunk(json.dumps({"d": piece}, ensure_ascii=False) + "\n")

            try:
                msgs = ai_sum.messages(style, src["scope"], src["title"],
                                       src["text"], src["module"])
                with agent_open({"url": url, "key": key,
                                 "body": {"model": model, "messages": msgs,
                                          "stream": True}}) as r:
                    for piece in agent_deltas(r):
                        emit(piece)
                text = "".join(sent).strip()
                if not text:
                    raise RuntimeError("那边一个字都没回，模型名或地址可能不对")
                d = safe_book_dir(book)
                saved = ai_sum.save_result(d, src["scope"], style, src["chapter"], text) if d else ""
                self._chunk(json.dumps({"done": True, "saved": bool(saved),
                                       "scope": src["scope"], "style": style},
                                       ensure_ascii=False) + "\n")
            except Exception as e:
                log(f"--- AI 小结出错：{str(e)[:200]} ---")
                self._chunk(json.dumps({"err": str(e)[:300]}, ensure_ascii=False) + "\n")
            self._chunk_end()
            return

        if u.path == "/api/ai/mindmap":
            # 三种内容共用：范围还是后端定，画完直接落进这本书的 mindmap.json，
            # 界面上拿到的就是那张可以接着手改的图（不是只能看的图片）。
            try:
                ok, out = ai_mindmap(str((body or {}).get("book") or ""),
                                     (body or {}).get("chapter"))
                if not ok:
                    return self._json({"ok": False, "msg": out.get("msg") or "导图没生成"})
                out["ok"] = True
                out["svg"] = mindmap.render_svg(out["doc"])
                return self._json(out)
            except Exception as e:
                # 这条口一旦抛穿，前端拿到的就是「按了没反应」—— 统一收成一句人话。
                log(f"--- AI 导图出错：{type(e).__name__}: {e} ---")
                return self._json({"ok": False, "msg": "导图没生成：%s" % str(e)[:160]})

        if u.path == "/api/readstat/tick":
            ok = readstat_tick((body or {}).get("book"), (body or {}).get("title"),
                               (body or {}).get("seconds"))
            return self._json({"ok": ok})

        if u.path == "/api/readstat/clear":
            ok, msg = readstat_clear()
            return self._json({"ok": ok, "msg": msg})

        if u.path == "/api/sync/save":
            ok, msg = sync_save((body or {}).get("cfg") or {})
            return self._json({"ok": ok, "msg": msg, "sync": sync_state()})

        if u.path == "/api/sync/test":
            ok, msg = sync_test()
            return self._json({"ok": ok, "msg": msg})

        if u.path == "/api/sync/now":
            ok, msg = sync_run_now()
            return self._json({"ok": ok, "msg": msg, "sync": sync_state()})

        if u.path == "/api/sync/onedrive/start":
            ok, msg, info = sync_device_start()
            return self._json({"ok": ok, "msg": msg, "info": info or {}})

        if u.path == "/api/sync/onedrive/poll":
            ok, msg = sync_device_poll()
            return self._json({"ok": ok, "msg": msg, "sync": sync_state()})

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

        if u.path == "/api/clip":
            # 剪藏：粘贴链接 → 解析正文 → 收成书架里的一本书。
            # 三种模式：preview 只解析不入架（先让人确认抓对了），save 收单篇，
            # batch 一次收多篇。前端主用 preview + 逐篇 save —— 这样每篇落架就能
            # 立刻在列表里出现，进度是「第 3/20 篇」而不是转半分钟圈。
            mode = str((body or {}).get("mode") or "preview").strip()
            urls = clip_targets(body)
            if not urls:
                return self._json({"ok": False, "msg": "先粘贴至少一个文章链接"})
            if mode == "batch":
                try:
                    res = clip_article.clip_many(module_dir("clip"), urls,
                                                 cover_dir=COVER_DIR,
                                                 extract_fn=clip_extract)
                except Exception as e:
                    return self._json({"ok": False, "msg": "剪不动：%s" % str(e)[:160]})
                for info in res["ok"]:
                    cache_meta_cover(info.get("id"))
                    log(f"剪藏入库：{info['title']}（{info.get('site') or ''}，"
                        f"{info.get('words') or info['chars']} 字）")
                return self._json({
                    "ok": bool(res["ok"]), "done": res["done"], "failed": res["failed"],
                    "books": res["ok"], "fail": res["fail"],
                    "msg": (f"剪好 {res['done']} 篇"
                            + (f"，{res['failed']} 篇没成：{res['fail'][0]['msg']}"
                               if res["fail"] else "")) if res["ok"]
                    else (res["fail"][0]["msg"] if res["fail"] else "一篇都没成")})
            url = urls[0]
            try:
                if mode == "save":
                    info = clip_article.save_clip(
                        module_dir("clip"), url,
                        title=str((body or {}).get("title") or "").strip(),
                        author=str((body or {}).get("author") or "").strip(),
                        cover_dir=COVER_DIR, extract_fn=clip_extract)
                    cache_meta_cover(info.get("id"))
                else:
                    art = clip_extract(url)
                    return self._json({"ok": True, "preview": {
                        "title": art["title"], "author": art["author"],
                        "site": art["site"], "date": art["date"], "cover": art["cover"],
                        "url": art["url"], "words": art["words"],
                        "head": art["markdown"][:900]}})
            except Exception as e:
                # 抓取失败全是 ValueError 带的人话（要验证 / 没正文 / 内网地址），
                # 原样递出去；真出意外也不让连接断掉
                msg = str(e)[:200]
                log(f"--- 剪藏失败：{msg} ---")
                return self._json({"ok": False, "msg": msg or "这一篇剪不出来"})
            log(f"剪藏入库：{info['title']}（{info.get('site') or ''}，{info['chars']} 字）")
            return self._json({"ok": True, "book": info,
                               "msg": f"已剪进书架：《{info['title']}》{info['chars']} 字"})

        if u.path == "/api/feed":
            return self._json(feed_do(body))

        if u.path == "/api/flomo/notes":
            # 注意方向：/api/flomo 是「把内容发去 flomo」（写用户的 webhook），
            # /api/flomo/notes 才是反过来「把 flomo 导进来」（只动本机）。
            # 两条路挨着容易混，所以分开命名，日志里也各说各的。
            return self._json(flomo_do(body))

        if u.path == "/api/video":
            return self._json(video_do(body))

        if u.path == "/api/mynotes":
            # 存这本书的笔记。落盘后把「服务端认得的版本」回给前端 —— id 和时间戳
            # 是补齐过的，前端拿返回值覆盖自己的状态，就不会出现「我这边有条没 id」
            # 这种对不上的情况。
            book = str((body or {}).get("book") or "")
            d = safe_book_dir(book)
            if not d:
                return self._json({"ok": False, "msg": "没找到这本书，笔记没地方放"})
            doc = (body or {}).get("doc")
            if not isinstance(doc, dict):
                return self._json({"ok": False, "msg": "笔记内容看起来不对，没有保存"})
            try:
                book_notes.save_notes(d, doc)
            except Exception as e:
                return self._json({"ok": False, "msg": "存不下：%s" % str(e)[:140]})
            saved = book_notes.load_notes(d)
            _NOTES_COUNTS.pop(d, None)       # 计数缓存作废，书架下一次刷新就是新数字
            return self._json({"ok": True, "doc": saved,
                               "counts": book_notes.counts(saved)})

        if u.path == "/api/mynotes/export":
            # 导出 notes.md 到书目录。读的是**已保存**的版本：前端点导出前要先存一次，
            # 免得导出的内容和屏幕上看到的不一样，还让人以为笔记丢了。
            book = str((body or {}).get("book") or "")
            d = safe_book_dir(book)
            if not d:
                return self._json({"ok": False, "msg": "没找到这本书"})
            doc = book_notes.load_notes(d)
            if book_notes.counts(doc) == {"marks": 0, "entries": 0}:
                return self._json({"ok": False, "msg": "这本书还没有笔记，先划两句再导"})
            meta = book_meta(book)
            try:
                res = book_notes.export_notes(d, doc, meta=meta,
                                              titles=book_notes.chapter_titles(d),
                                              book_label=meta.get("title") or book)
            except Exception as e:
                return self._json({"ok": False, "msg": "导出没做成：%s" % str(e)[:140]})
            return self._json({"ok": True, "path": res["path"], "dir": d,
                               "chars": res["chars"], "counts": book_notes.counts(doc),
                               "msg": f"已导出 notes.md（{res['chars']} 字），就放在这本书的文件夹里"})

        if u.path == "/api/note_tpl":
            # 模板：官方那几套不能删，用户自己写的存进 cache（跨书共用一套模板库）。
            if (body or {}).get("del"):
                return self._json(book_notes.delete_template(CACHE_DIR,
                                                             str((body or {}).get("del"))))
            tpl = (body or {}).get("save")
            if isinstance(tpl, dict):
                out = book_notes.save_template(CACHE_DIR, tpl)
                if out.get("ok"):
                    out["items"] = book_notes.templates(CACHE_DIR)
                return self._json(out)
            use = (body or {}).get("apply")
            if isinstance(use, dict):
                tid = str(use.get("id") or "")
                tpl = book_notes.get_template(CACHE_DIR, tid)
                if not tpl:
                    return self._json({"ok": False, "msg": "没找到这个模板"})
                return self._json({"ok": True,
                                   "body": book_notes.apply_template(tpl, use)})
            return self._json({"ok": False, "msg": "没说要干什么：存、删还是套"})

        if u.path == "/api/board":
            # 画板只有一条路：一切落在 <书目录>/boards/ 里，界面上画的东西随时能存回去。
            book = str((body or {}).get("book") or "")
            d = safe_book_dir(book)
            if not d:
                return self._json({"ok": False, "msg": "没找到这本书，画板没地方放"})
            act = str((body or {}).get("act") or "")
            if act == "new":
                # id 由后端发。让前端自己拼时间戳会在同一秒新建两块时撞号 ——
                # 撞了就是后画的那块把前一块覆盖掉，那种丢失用户根本看不见。
                return self._json({"ok": True,
                                   "board": board_mod.empty(
                                       "b" + uuid.uuid4().hex[:12],
                                       str((body or {}).get("title") or ""))})
            if act == "save":
                doc = (body or {}).get("doc")
                if not isinstance(doc, dict):
                    return self._json({"ok": False, "msg": "画板内容看起来不对，没有保存"})
                res = board_mod.save_board(d, doc)
                if res.get("ok"):
                    res["boards"] = board_mod.list_boards(d)
                return self._json(res)
            if act == "delete":
                res = board_mod.delete_board(d, str((body or {}).get("id") or ""),
                                             purge=bool((body or {}).get("purge", True)))
                if res.get("ok"):
                    res["boards"] = board_mod.list_boards(d)
                return self._json(res)
            if act == "export":
                # 收下浏览器导出的 SVG / PNG。类型由 board.save_export 自己验（看内容不声明），
                # 所以这里不必再判一遍 —— 它拒绝的时候磁盘上不会留下半个文件。
                return self._json(board_mod.save_export(
                    d, str((body or {}).get("id") or ""),
                    str((body or {}).get("fmt") or ""), (body or {}).get("data")))
            if act == "md":
                # 「并进笔记」：把这块板写成一条笔记条目的 Markdown 给前端。
                # 图必须先导出来才谈得上进笔记，否则 notes.md 里就是一条点开是坏图的链接。
                doc = board_mod.load_board(d, str((body or {}).get("id") or ""))
                if doc is None:
                    return self._json({"ok": False, "msg": "没有这块画板"})
                md = board_mod.to_markdown(doc, base_dir=d)
                # has_image 以磁盘为准，不认信封里的标记：那两面 flag 只有 list_boards 会补，
                # load_board 回来的是原信封，照它报就会「图明明在，界面却说没有」。
                folder = os.path.join(d, board_mod.BOARD_DIR)
                has_image = any(os.path.exists(os.path.join(folder, doc["id"] + ext))
                                for ext in (board_mod.PNG_EXT, board_mod.SVG_EXT))
                return self._json({"ok": True, "md": md, "id": doc["id"],
                                   "has_image": has_image})
            return self._json({"ok": False, "msg": "没说要干什么：新建、存、删、导出还是转笔记"})

        if u.path == "/api/mindmap":
            book = str((body or {}).get("book") or "")
            d = safe_book_dir(book)
            if not d:
                return self._json({"ok": False, "msg": "没找到这本书，图没地方放"})
            act = str((body or {}).get("act") or "")
            if act == "from_notes":
                # 从这本书的笔记生成一棵树，只「给」不「存」：界面上确认了再走 save。
                # 直接盖掉的话，用户手动画的那张会在点错一下之后没了 —— 这种丢失不该由后端决定。
                cut = str((body or {}).get("cut") or "tag")
                if cut not in ("tag", "chapter", "entry"):
                    cut = "tag"
                meta = book_meta(book)
                try:
                    tree = book_notes.note_tree(
                        book_notes.load_notes(d), meta=meta,
                        titles=book_notes.chapter_titles(d),
                        book_label=meta.get("title") or book, cut=cut)
                    doc = mindmap.from_note_tree(tree, meta.get("title") or book)
                except Exception as e:
                    return self._json({"ok": False, "msg": "笔记没能变成图：%s" % str(e)[:140]})
                has = os.path.isfile(mindmap.path_of(d))
                return self._json({"ok": True, "doc": doc,
                                   "layout": mindmap.layout(doc),
                                   "has_existing": has,
                                   "msg": ("这本书已经有一张图了，换进去会盖掉它"
                                           if has else "已从笔记生成，改完记得存")})
            if act == "layout":
                # 拖完一个框只想看新坐标，不想惊动盘上那份：布局是纯函数，在这里算完还给前端。
                # 前端绝不自己再排一遍 —— 两套几何一漂，存回去的坐标和屏幕上看见的就是两回事。
                doc = (body or {}).get("doc")
                if not isinstance(doc, dict):
                    return self._json({"ok": False, "msg": "这张图看起来不对，算不出布局"})
                form = str((body or {}).get("form") or "")
                try:
                    return self._json({"ok": True, "layout": mindmap.layout(doc, form or None)})
                except Exception as e:
                    return self._json({"ok": False, "msg": "布局算不出来：%s" % str(e)[:140]})
            doc = (body or {}).get("doc")
            if not isinstance(doc, dict):
                return self._json({"ok": False, "msg": "这张图看起来不对，没有保存"})
            try:
                res = mindmap.save_map(d, doc)
                saved = mindmap.load_map(d)
                res["doc"] = saved
                res["layout"] = mindmap.layout(saved)
            except Exception as e:
                return self._json({"ok": False, "msg": "这张图存不下去：%s" % str(e)[:140]})
            if not res.get("ok"):
                res["msg"] = res.get("error") or "这张图没存进去"
            else:
                res["msg"] = "导图已存好（%d 个节点）" % res.get("nodes", 0)
            return self._json(res)

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

        if action == "cleanup":
            # 维护那一栏：先试算（只看不删），用户点头了才真删。删的是几百 MB 到 1.6GB
            # 且不可逆 —— 所以 plan 与 run 分成两步，界面必须把 plan 摆给用户看过。
            mode = str((body or {}).get("mode") or "plan").strip()
            what = str((body or {}).get("what") or "all").strip()
            if what not in ("components", "data", "all"):
                return self._json({"ok": False, "msg": "不认识要清哪一类"})
            if mode == "run":
                r = cleanup.run(what)
                log("--- 维护：卸载/清理 %s → 释放 %s ---"
                    % (what, cleanup.human(r["freed"])))
                msg = "已释放 %s" % cleanup.human(r["freed"]) if r["ok"] else \
                      ("有几样没删成：%s" % "；".join(r["errors"])[:180])
                return self._json({"ok": r["ok"], "msg": msg, "result": r,
                                   "plan": cleanup.plan("all")})
            return self._json({"ok": True, "plan": cleanup.plan(what)})

        if action == "media.save":
            ok, msg = set_media_cfg(body.get("engine"), body.get("lang"))
            return self._json({"ok": ok, "msg": msg,
                               "asr": (load_cfg().get("asr_engine") or "auto"),
                               "lang": (load_cfg().get("asr_lang") or "")})

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
            ok, msg = start_task("export", [py(), script("export_precise.py"), book_id],
                                 book=book_id, env_extra=task_env("weread"))
            return self._json({"ok": ok, "msg": msg, "id": book_id})

        if action == "export.fresh":
            # 「清掉重取」：上次中止留下的残稿先抹平，再从头取一遍。
            # 顺序很重要 —— 先看有没有任务在跑，再动文件。反过来的话，
            # 清完却起不来任务（start_task 会回「已有任务在运行」），
            # 用户手里就只剩一本被清空的书，比原来更糟。
            book_id = extract_book_id(book)
            if not book_id:
                return self._json({"ok": False, "msg": "没能从里面认出书籍编号，看看是不是粘错了"})
            with _lock:
                if TASK["running"]:
                    return self._json({"ok": False,
                                       "msg": "已经有任务在跑，先中止它再清稿重取"})
            r = reset_book_output(book_id)
            if not r.get("ok"):
                return self._json(r)
            ok, msg = start_task("export", [py(), script("export_precise.py"), book_id],
                                 book=book_id, env_extra=task_env("weread"))
            return self._json({"ok": ok, "id": book_id, "removed": r.get("removed", 0),
                               "msg": (r["msg"] + " · " + msg) if ok else msg})

        if action == "images":
            book_id = (book or "").strip()
            if not safe_book_dir(book_id):
                return self._json({"ok": False, "msg": "找不到这本书的导出目录"})
            ok, msg = start_task("images", [py(), script("download_images.py"), book_id],
                                 book=book_id, env_extra=task_env(book_module(book_id)))
            return self._json({"ok": ok, "msg": msg})

        if action == "reveal":
            d = safe_book_dir(book)
            if not d:
                return self._json({"ok": False, "msg": "目录不存在"})
            pc.open_in_file_manager(d)
            return self._json({"ok": True, "msg": "已在文件管理器打开"})

        # 打开整个书库根目录，或某个模块那一格：界面上每屏的「打开所在文件夹」
        # 各开各的，剪藏那一屏不必把微信读书那一大堆一起摊在访达里。
        if action == "openbooks":
            ensure_books_dir()
            mod = (body.get("module") or "").strip()
            named = mod if mod in book_layout.DIR_OF else ""
            pc.open_in_file_manager(module_dir(named) if named else OUT_DIR)
            return self._json({"ok": True,
                               "msg": "已打开「%s」的文件夹" % MODULE_NAME[named] if named
                               else "已打开书库文件夹"})

        if action == "folder.new":
            name = (body.get("name") or "").strip()[:40]
            if not name:
                return self._json({"ok": False, "msg": "文件夹需要一个名字"})
            lib = load_lib()
            s = lib_scope(lib, body.get("module"))
            fid = "f" + uuid.uuid4().hex[:8]
            s["folders"].append({"id": fid, "name": name})
            save_lib(lib)
            return self._json({"ok": True, "id": fid, "msg": "已新建"})

        if action == "folder.rename":
            fid = (body.get("id") or "").strip()
            name = (body.get("name") or "").strip()[:40]
            if not (fid and name):
                return self._json({"ok": False, "msg": "缺少参数"})
            lib = load_lib()
            s = lib_scope(lib, body.get("module"))
            if not any(f["id"] == fid for f in s["folders"]):
                return self._json({"ok": False, "msg": "这个模块里没有这个文件夹"})
            for f in s["folders"]:
                if f["id"] == fid:
                    f["name"] = name
            save_lib(lib)
            return self._json({"ok": True, "msg": "已改名"})

        if action == "folder.drop":
            fid = (body.get("id") or "").strip()
            lib = load_lib()
            s = lib_scope(lib, body.get("module"))
            keep = [f for f in s["folders"] if f["id"] != fid]
            if len(keep) == len(s["folders"]):
                return self._json({"ok": False, "msg": "这个模块里没有这个文件夹"})
            s["folders"] = keep
            for bid, f in list(s["assign"].items()):
                if f == fid:
                    s["assign"].pop(bid, None)
            save_lib(lib)
            return self._json({"ok": True, "msg": "文件夹已删除，书回到未归类"})

        if action == "book.move":
            bid = (body.get("book") or "").strip()
            folder = (body.get("folder") or "").strip()
            d = safe_book_dir(bid)
            if not d:
                return self._json({"ok": False, "msg": "找不到这本书"})
            lib = load_lib()
            # 归到哪一格由这本书自己住哪儿决定，不信前端传过来的模块名：
            # 前端的模块标签可能是上一次刷新时留下的，跨模块归错夹子看不出来。
            s = lib_scope(lib, book_layout.MODULE_OF.get(
                os.path.basename(os.path.dirname(d)), "weread"))
            if folder and not any(f["id"] == folder for f in s["folders"]):
                return self._json({"ok": False, "msg": "这个模块里没有这个文件夹"})
            if folder:
                s["assign"][bid] = folder
            else:
                s["assign"].pop(bid, None)
            save_lib(lib)
            return self._json({"ok": True, "msg": "已归入"})

        if action == "book.order":
            bid = (body.get("book") or "").strip()
            before = (body.get("before") or "").strip()
            mod = book_module(bid)
            if not safe_book_dir(bid):
                return self._json({"ok": False, "msg": "找不到这本书"})
            lib = load_lib()
            s = lib_scope(lib, mod)
            ids = [b["id"] for b in list_books(mod) if b["id"] != bid]
            if before and before in ids:
                ids.insert(ids.index(before), bid)
            else:
                ids.append(bid)
            s["order"] = ids
            save_lib(lib)
            return self._json({"ok": True, "msg": "顺序已调整"})

        if action == "book.tag":
            # 给一篇文章 / 一本书贴标签（可多张）。只在它自己那个模块里贴：
            # 剪藏那套「随笔 / 待读」不会冒到微信读书的整本书上去。
            bid = (body.get("book") or "").strip()
            mod = book_module(bid)
            if not safe_book_dir(bid):
                return self._json({"ok": False, "msg": "找不到这本书"})
            add = body.get("add") or []
            drop = body.get("remove") or ""
            if isinstance(add, str):
                add = [add]
            lib = load_lib()
            s = lib_scope(lib, mod)
            cur = [str(t) for t in (s["tags"].get(bid) or [])]
            for t in [str(x).strip()[:20] for x in add if str(x).strip()]:
                if t and t not in cur:
                    cur.append(t)
            drop = str(drop).strip()[:20]
            if drop:
                cur = [t for t in cur if t != drop]
            if len(cur) > 12:
                cur = cur[:12]
            if cur:
                s["tags"][bid] = cur
            else:
                s["tags"].pop(bid, None)
            save_lib(lib)
            return self._json({"ok": True, "tags": cur, "msg": "标签已更新"})

        if action == "tag.rename":
            old = str(body.get("from") or "").strip()[:20]
            new = str(body.get("to") or "").strip()[:20]
            if not (old and new):
                return self._json({"ok": False, "msg": "缺少参数"})
            lib = load_lib()
            s = lib_scope(lib, body.get("module"))
            n = 0
            for bid, tags in list(s["tags"].items()):
                if old in tags:
                    merged = [new if t == old else t for t in tags]
                    merged = list(dict.fromkeys(merged))[:12]
                    s["tags"][bid] = merged
                    n += 1
            save_lib(lib)
            return self._json({"ok": n > 0, "msg": f"已改 {n} 篇" if n else "没有用到这个标签"})

        if action == "tag.drop":
            old = str(body.get("name") or "").strip()[:20]
            if not old:
                return self._json({"ok": False, "msg": "缺少参数"})
            lib = load_lib()
            s = lib_scope(lib, body.get("module"))
            n = 0
            for bid, tags in list(s["tags"].items()):
                if old in tags:
                    rest = [t for t in tags if t != old]
                    if rest:
                        s["tags"][bid] = rest
                    else:
                        s["tags"].pop(bid, None)
                    n += 1
            save_lib(lib)
            return self._json({"ok": True, "msg": f"已从 {n} 篇摘掉"})

        if action == "book.delete":
            bid = (body.get("book") or "").strip()
            d = safe_book_dir(bid)
            if not d:
                return self._json({"ok": False, "msg": "找不到这本书"})
            title = meta_title(bid)
            mod = book_layout.MODULE_OF.get(os.path.basename(os.path.dirname(d)), "weread")
            # 上游在写书的目录旁还会留一份合并稿，一并清掉（限定在书库内）
            drop_stray_md(bid, title)
            shutil.rmtree(d)
            lib = load_lib()
            lib_forget(lib, bid, mod)
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
            # 绑到哪个口就写哪个口：默认端口被占时上面这个循环会自己往后挪，
            # 而 MCP 适配器只拿得到我们请它用的那个数。不写下来，挪完就没人找得着。
            pc.write_runtime(REPO, p, version=VERSION)
            print(f"  微信读书导出 → http://127.0.0.1:{p}", flush=True)
            # 进门之后自动补视频那条线的组件：引擎缺就装、模型缺就在后台下（1.6GB 那个）。
            # 只有壳（安装包）会设 GUIZANG_AUTO_MEDIA=1 —— 源码直接跑时不该因为起了一下
            # 服务就悄悄拉一份 GB 级权重。是后台线程，不挡服务起来。
            try:
                ok, msg = media_setup.maybe_auto_start()
                print(f"  转写组件: {msg}", flush=True)
            except Exception as e:
                print(f"  转写组件: 自动准备没起来（{type(e).__name__}: {e}）", flush=True)
            srv.serve_forever()
            return
        except OSError:
            continue
    print("  找不到可用端口", flush=True)


if __name__ == "__main__":
    main()
