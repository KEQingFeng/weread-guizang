# -*- coding: utf-8 -*-
"""画板（看书时涂鸦）的存储与导出：一块板 = 书目录里 boards/<id>.json 一份 fabric 画布。

为什么单独一个文件：画布本身是浏览器里的事（fabric 的 toJSON / toSVG / toDataURL），
服务端一样都画不了，也不需要能画 —— 它只做校验、落盘、读回、列目录、删、拼 Markdown。
把这几件事收在一处，是为了让「路径纪律」只有一个地方需要审：board id 一律先过 slug()，
产物只能落在 <书目录>/boards/ 下面。

数据跟着书走，与 book_notes.py 同一条理由：拷走这本书的目录就拷走了它所有的画板，
不藏进数据库，也不在数据目录里另起一处。JSON 是真相（可再编辑），SVG / PNG 是导出物。
"""

import base64
import binascii
import json
import os
import re
import time
import urllib.parse
import uuid

BOARD_DIR = "boards"
BOARD_FILE_EXT = ".json"
SVG_EXT = ".svg"
PNG_EXT = ".png"
PAPERS = ("plain", "grid", "dots", "lines", "dark")
SCHEMA = 1

# 上限：正常画布撑不满，防的是把 PNG 当 dataURL 塞进 canvas 里存、把整本相册写进一个标题。
MAX_JSON_BYTES = 6_000_000
MAX_TITLE = 80
MAX_CAPTION = 2000
MAX_BOARDS = 200                 # 一个文件夹里别堆几百块板
MAX_SVG_BYTES = 8_000_000
MAX_PNG_BYTES = 25_000_000
ID_LEN = 40

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")
# SVG 允许前面有 XML 声明、注释、空白；判完前缀再决定收不收（见 _strip_svg_head）。
# XML 声明、注释、DOCTYPE 都在 <svg 前面：fabric.js 导出的每一份 SVG 都带 DOCTYPE，
# 早先这里只跳得过前两种，结果「存 SVG」按钮被自己的校验挡在外面（画板 Gate 抓到过）。
# DOCTYPE 允许带内部子集（<!DOCTYPE svg [ ... ]>），不然又会卡在同一个地方。
_XML_HEAD = re.compile(r"(?:<\?[^>]*\?>|<!--.*?-->|<!DOCTYPE(?:[^>\[]|\[[^\]]*\])*>)\s*",
                       re.I | re.S)


# ─────────────────────────── 名字与路径 ───────────────────────────

def slug(v) -> str:
    """把任意外来字符串收敛成能安全拼进文件名的画板 id：只留 [A-Za-z0-9_-]，长度 ≤40。

    为什么这么狠：id 来自界面和 URL，`../../escape`、`%2e%2e`、中文都可能被拼进路径，
    一旦拼进去，写出的就是书目录外面的文件。所以先删非法字符（点号一起删，`..` 自然没了；
    `%2e%2e` 删掉 % 后变成 `2e2e`，也不含点），删空了就自己生成一个 —— 宁可换个名字，
    也不能让调用方拿到一个「看起来存成功了、其实写到外面」的结果。
    """
    s = UNSAFE.sub("", str(v if v is not None else ""))
    return s[:ID_LEN] or uuid.uuid4().hex[:8]


# 开发规范那份任务书里把这道清洗叫 `_slug()`，界面/其它模块可能按那个名字调；
# 只留一个实现，避免两处规则漂移。
_slug = slug


def dir_of(book_dir) -> str:
    """这本书的画板目录（只拼路径，不建目录、不校验，跟 notes_path() 一样）。"""
    return os.path.join(str(book_dir or ""), BOARD_DIR)


def _file_of(book_dir, name) -> str:
    """把已经过 slug 的文件名拼进 boards/，并确认结果仍在书目录里；越界回空串。

    slug() 之后名字里不可能再有 / 或 ..，这道检查看着多余。留着是因为它是所有写入
    唯一的一处「目录拼装」出口 —— 万一以后放宽命名（比如允许中文名），越界仍会被兜住。
    """
    folder = os.path.join(os.path.abspath(str(book_dir or "")), BOARD_DIR)
    path = os.path.normpath(os.path.join(folder, name))
    if not path.startswith(folder + os.sep):
        return ""
    return path


def _ext_of(fmt) -> str:
    return SVG_EXT if fmt == "svg" else PNG_EXT


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _text(v, limit) -> str:
    """文本字段收敛：非字符串先 str，去首尾空白，按字符数裁到上限。"""
    if v is None:
        return ""
    return str(v).strip()[:limit]


def _human_size(n) -> str:
    """给人看的字节数：界面提示里用「340 KB」而不是「348160」。

    按十进制算（1 KB = 1000 B），跟上面那几个上限常量同一口径 —— 用 1024 折算的话，
    MAX_JSON_BYTES = 6_000_000 会报成「上限 5.7 MB」，用户对着任务书看就是两个数。
    """
    n = max(int(n or 0), 0)
    if n >= 1000 * 1000:
        return "%s MB" % ("%.1f" % (n / 1000000.0)).rstrip("0").rstrip(".")
    if n >= 1000:
        return "%d KB" % round(n / 1000.0)
    return "%d B" % n


def _json_bytes(obj):
    """序列化后的字节数；序列化不了回 None（画布里塞了不能存的东西，交给调用方处置）。

    allow_nan=False 是认真的：NaN / Infinity 只有 Python 的 json 写得出来也读得回去，
    浏览器的 JSON.parse 见到就报错 —— 存下去等于存了一块再也打不开的板，宁可拒收。
    """
    try:
        return len(json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError):
        return None


def _file_size(path) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _over_msg(limit, actual, label) -> str:
    """超上限的人话：说清上限多少、超了多少，别让人去猜是不是自己画太多。"""
    return "%s超过上限 %s（这份 %s，超了 %s），没存" % (
        label, _human_size(limit), _human_size(actual), _human_size(actual - limit))


# ─────────────────────────── 信封 ───────────────────────────

def empty(board_id=None, title="") -> dict:
    """一块能直接存的新画板：字段齐、canvas 是 fabric loadFromJSON 认得了的空画布。"""
    canvas = {"objects": []}
    return {
        "schema": SCHEMA,
        "id": slug(board_id),
        "title": _text(title, MAX_TITLE),
        "caption": "",
        "paper": "plain",
        "canvas": canvas,
        "bytes": _json_bytes(canvas) or 0,
        "created": _now(),
        "updated": _now(),
    }


def normalize_env(doc) -> dict:
    """清洗外来信封：字段裁剪、时间补齐、条数与体积交给上层判；canvas 原样保留。

    幂等 —— 洗过的结果再洗一次一模一样，所以界面把 load 回来的信封原样送存也不会漂。
    canvas 是我们完全不解析的东西（fabric 的版本、对象、路径都随它去），只要求它是个
    对象；不是对象（缺字段、被写成字符串了）就换成空画布，否则读回来界面无从下手。
    """
    src = doc if isinstance(doc, dict) else {}
    canvas = src.get("canvas")
    if not isinstance(canvas, dict):
        canvas = {"objects": []}
    created = _text(src.get("created"), 40) or _now()
    updated = _text(src.get("updated"), 40) or created
    size = _json_bytes(canvas)
    return {
        "schema": SCHEMA,
        "id": slug(src.get("id")),
        "title": _text(src.get("title"), MAX_TITLE),
        "caption": _text(src.get("caption"), MAX_CAPTION),
        "paper": src.get("paper") if src.get("paper") in PAPERS else "plain",
        "canvas": canvas,
        # bytes 现算，不信前端给的数：它说的是画布 JSON 的大小，只有我们知道序列化口径。
        "bytes": size if size is not None else 0,
        "created": created,
        "updated": updated,
    }


# ─────────────────────────── 读写 ───────────────────────────

def _write_json(path, obj):
    """原子写 JSON：先写临时名再 os.replace，中途崩了也不会留下半截文件。

    与 book_notes._write_json 同一路子，覆盖前留一份 .prev —— 画板是用户画出来的东西，
    存坏了比笔记更心疼。（.prev 结尾不是 .json，不进列表、不占 MAX_BOARDS 的名额。）
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, allow_nan=False)
    if os.path.exists(path):
        try:
            os.replace(path, path + ".prev")
        except OSError:
            pass                    # 留档失败不该拦下这次保存
    os.replace(tmp, path)


def _write_bytes(path, raw):
    """二进制导出物走同一个原子路子，但不留 .prev：导出物随时能从画布重画，不值得占双倍盘。"""
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)          # 半截的临时文件必须收掉，不然目录里全是垃圾
        except OSError:
            pass
        raise


def _ensure_dir(book_dir):
    """boards/ 目录：没有就建，建不了（书目录被删了、盘只读）回一句人话。"""
    folder = dir_of(book_dir)
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        return False
    return True


def board_path(book_dir, board_id) -> str:
    """这块板 JSON 的路径；id 越界或非法时也会回一个安全路径（slug 兜底）。"""
    return _file_of(book_dir, slug(board_id) + BOARD_FILE_EXT)


def load_board(book_dir, board_id) -> dict | None:
    """读一块板；文件不存在、读坏、不是对象都回 None，绝不因为一块坏板把请求打崩。

    不动盘：坏档不去改名（改名等于让它从列表里消失，用户反而找不到、删不掉），
    留在那儿让 list_boards 报出来，由用户决定删不删。
    """
    path = board_path(book_dir, board_id)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    return normalize_env(doc)


def _env_row(book_dir, name, path):
    """列表用的一行：只取信封字段，不返回 canvas（200 块板的画布全拖给界面毫无意义）。"""
    doc = None
    try:
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
        doc = got if isinstance(got, dict) else None
    except (OSError, ValueError, UnicodeDecodeError):
        doc = None
    if doc is None:
        # 读不出来的板也要出现在列表里（否则用户既看不见也删不掉），用文件大小和
        # 磁盘时间兜出可读的一行。
        try:
            st = os.stat(path)
        except OSError:
            return None
        doc = {"updated": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime)),
               "bytes": st.st_size}
    svg = slug(name) + SVG_EXT
    png = slug(name) + PNG_EXT
    svg_ok = os.path.exists(os.path.join(dir_of(book_dir), svg))
    png_ok = os.path.exists(os.path.join(dir_of(book_dir), png))
    size = doc.get("bytes")
    if not isinstance(size, int) or size < 0:
        # 信封里的 bytes 丢了（老档、手改过）就用磁盘大小兜个近似，列表只显示量级。
        size = _file_size(path)
    return {
        "id": name,
        "title": _text(doc.get("title"), MAX_TITLE),
        "caption": _text(doc.get("caption"), MAX_CAPTION),
        "paper": doc.get("paper") if doc.get("paper") in PAPERS else "plain",
        "updated": _text(doc.get("updated"), 40),
        "bytes": size,
        "has_svg": svg_ok,
        "has_png": png_ok,
        # 只给文件名，路径由界面按 boards/ 自己拼；没有导出物给空串，省得判 None。
        "svg_name": svg if svg_ok else "",
        "png_name": png if png_ok else "",
    }


def _board_files(book_dir) -> list:
    """boards/ 里被认作画板的那些名字（不含扩展名）。

    只认 `<id>.json`：readme.txt 这类随手放的、`.tmp` / `.prev` 这类写到一半或留档的、
    还有名字里带点带中文的野文件，都不算画板。列目录和数条数共用这一条判据 ——
    列表里看不见的板不该占 MAX_BOARDS 的名额，反过来也不该出现「数得过来、列不出来」。
    """
    folder = dir_of(book_dir)
    if not os.path.isdir(folder):
        return []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    out = []
    for fn in names:
        if not fn.endswith(BOARD_FILE_EXT):
            continue
        stem = fn[:-len(BOARD_FILE_EXT)]
        if stem and slug(stem) == stem:
            out.append(stem)
    return out


def list_boards(book_dir) -> list[dict]:
    """列这本书的画板，按 updated 新→旧。目录不存在回空列表，不抛。"""
    folder = dir_of(book_dir)
    rows = []
    for sid in _board_files(book_dir):
        row = _env_row(book_dir, sid, os.path.join(folder, sid + BOARD_FILE_EXT))
        if row:
            rows.append(row)
    # 同一秒里存的板按 id 排，保证两次刷新顺序不抖（readdir 的顺序本身是不定的）。
    rows.sort(key=lambda r: (r["updated"], r["id"]), reverse=True)
    return rows


def board_counts(book_dir) -> int:
    """这本书已有几块板：只数文件名，不去读每份画布（与 counts() 一样是「条数」口径）。"""
    return len(_board_files(book_dir))


def save_board(book_dir, doc) -> dict:
    """存一块板。返回 {"ok","path","id","bytes","msg"}，msg 是给界面直接显示的人话。

    updated 由服务端盖时间：界面 load 回来的旧信封带着上一次的时间，照抄的话
    「按最后编辑排序」就永远不往前走。created 只在缺失时补。
    """
    env = normalize_env(doc)
    size = _json_bytes(env["canvas"])
    if size is None:
        return {"ok": False, "path": "", "id": env["id"], "bytes": 0,
                "msg": "画布里有存不进 JSON 的内容，没存"}
    if size > MAX_JSON_BYTES:
        return {"ok": False, "path": "", "id": env["id"], "bytes": size,
                "msg": _over_msg(MAX_JSON_BYTES, size, "画布内容")}
    path = board_path(book_dir, env["id"])
    if not path:
        return {"ok": False, "path": "", "id": env["id"], "bytes": size,
                "msg": "画板名字不能用，没存"}
    if not os.path.exists(path) and board_counts(book_dir) >= MAX_BOARDS:
        # 只挡新增：已经存在的板必须还能存回去，否则用户改一笔就再也保存不了。
        # 处置口径是「拒绝并存报人话」，不是悄悄删掉最旧的一块 —— 画出来的东西不该被程序扔。
        return {"ok": False, "path": "", "id": env["id"], "bytes": size,
                "msg": "这本书的画板到上限了（%d 张），先删掉几张再存" % MAX_BOARDS}
    if not _ensure_dir(book_dir):
        return {"ok": False, "path": "", "id": env["id"], "bytes": size,
                "msg": "画板目录建不出来，检查一下这本书还在不在"}
    env["bytes"] = size
    env["updated"] = _now()
    try:
        _write_json(path, env)
    except OSError:
        return {"ok": False, "path": "", "id": env["id"], "bytes": size,
                "msg": "画板没存进去，磁盘或权限出问题了"}
    return {"ok": True, "path": path, "id": env["id"], "bytes": size,
            "msg": "画板已存好（%s）" % _human_size(size)}


def delete_board(book_dir, board_id, purge=True) -> dict:
    """删一块板：purge=True 连它的 SVG / PNG 导出物一起清；False 只收画板、留图。

    文件本来就不存在不算失败 —— 界面点两次删除、或者板早已被清掉，都不该看到红字。
    """
    sid = slug(board_id)
    folder = dir_of(book_dir)
    names = ["%s%s" % (sid, BOARD_FILE_EXT)]
    if purge:
        names += ["%s%s" % (sid, SVG_EXT), "%s%s" % (sid, PNG_EXT)]
    removed, failed = [], []
    for name in names:
        path = os.path.join(folder, name)
        if not os.path.exists(path):
            continue
        try:
            os.remove(path)
        except OSError:
            failed.append(name)
            continue
        removed.append(name)
    if failed:
        return {"ok": False, "msg": "有 %d 个文件删不掉：%s" % (len(failed), "、".join(failed)),
                "removed": removed}
    if not removed:
        return {"ok": True, "msg": "这张板已经不在了", "removed": []}
    if not purge and any(os.path.exists(os.path.join(folder, n))
                         for n in (sid + SVG_EXT, sid + PNG_EXT)):
        msg = "画板已删掉，导出文件留在原处"
    else:
        msg = "画板已删掉" if len(removed) == 1 else "画板已删掉（连带 %d 个文件）" % len(removed)
    return {"ok": True, "msg": msg, "removed": removed}


# ─────────────────────────── 导出物 ───────────────────────────

def _strip_svg_head(text) -> str:
    """跳过前面的 BOM / 空白 / XML 声明 / 注释，把真正决定「这是不是 SVG」的那一段露出来。"""
    s = text.lstrip("\ufeff \t\r\n")
    while True:
        m = _XML_HEAD.match(s)
        if not m:
            return s
        s = s[m.end():]


def _as_text(data):
    """导出载荷归一成文本：字符串直接用，bytes 按 UTF-8 解；解不了回 None。"""
    if isinstance(data, str):
        return data
    if isinstance(data, (bytes, bytearray)):
        try:
            return bytes(data).decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


def _split_data_url(text):
    """拆 data URL：回 (头部, 逗号后的正文)；不是 data URL 就回 ("", 原文)。"""
    s = text.strip()
    if not s.startswith("data:"):
        return "", s
    head, _, rest = s.partition(",")
    return head, rest


def save_export(book_dir, board_id, fmt, data) -> dict:
    """收下浏览器导出的 SVG / PNG，落盘成 <id>.svg / <id>.png。

    返回 {"ok","path","bytes","fmt","msg"}。只认类型不认来源：SVG 看前缀、PNG 看魔数 ——
    声明成 image/png 的东西完全可能是 GIF 或一串文本，存下去用户迟早被坑一次。
    任何一种拒绝都在写盘之前发生，所以不会有「拒了但留下半个文件」这种状态。
    """
    fmt = str(fmt or "").strip().lower()
    sid = slug(board_id)
    if fmt not in ("svg", "png"):
        return {"ok": False, "path": "", "bytes": 0, "fmt": fmt,
                "msg": "只认 SVG 和 PNG 两种导出"}
    path = _file_of(book_dir, sid + _ext_of(fmt))
    if not path:
        return {"ok": False, "path": "", "bytes": 0, "fmt": fmt, "msg": "画板名字不能用，没存"}

    if fmt == "svg":
        text = _as_text(data)
        if text is None:
            return {"ok": False, "path": "", "bytes": 0, "fmt": fmt,
                    "msg": "SVG 内容得是文本，没存"}
        head, body = _split_data_url(text)
        if head and ";base64" in head.lower():
            try:
                body = base64.b64decode(body, validate=True).decode("utf-8")
            except (binascii.Error, ValueError, UnicodeDecodeError):
                return {"ok": False, "path": "", "bytes": 0, "fmt": fmt,
                        "msg": "这份 SVG 数据解不开，没存"}
        elif head:
            body = urllib.parse.unquote(body)
        # 前缀校验放在解码之后：不管来的是裸串还是 dataURL，最终必须是 <svg 开头才收。
        svg = _strip_svg_head(body)
        if not svg.startswith("<svg"):
            return {"ok": False, "path": "", "bytes": 0, "fmt": fmt, "msg": "这不是 SVG，没存"}
        # 落盘的就是校验过的这一份：fabric 导出的 <svg 前面挂着 XML 声明和一条指向
        # w3.org 的 DOCTYPE，留着它们，文件第一眼看过去不是 SVG，而且严格点的解析器
        # 会去联网取那个 DTD —— 一张本地图纸凭啥出门联网。砍掉头部，图形内容一个不动。
        raw = svg.encode("utf-8")
        limit, label = MAX_SVG_BYTES, "SVG "
    else:
        text = _as_text(data)
        if text is None:
            return {"ok": False, "path": "", "bytes": 0, "fmt": fmt,
                    "msg": "PNG 内容得是 base64 文本，没存"}
        head, body = _split_data_url(text)
        if head and ";base64" not in head.lower():
            return {"ok": False, "path": "", "bytes": 0, "fmt": fmt,
                    "msg": "PNG 得用 base64 形式给，没存"}
        # 浏览器/工具常把 base64 按行折出来，空白不是内容，去掉再验。
        b64 = re.sub(r"\s+", "", body)
        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            return {"ok": False, "path": "", "bytes": 0, "fmt": fmt,
                    "msg": "这份 PNG 数据解不开，没存"}
        if not raw.startswith(PNG_MAGIC):
            return {"ok": False, "path": "", "bytes": 0, "fmt": fmt, "msg": "这不是 PNG，没存"}
        limit, label = MAX_PNG_BYTES, "PNG "

    if len(raw) > limit:
        return {"ok": False, "path": "", "bytes": len(raw), "fmt": fmt,
                "msg": _over_msg(limit, len(raw), label)}
    if not _ensure_dir(book_dir):
        return {"ok": False, "path": "", "bytes": len(raw), "fmt": fmt,
                "msg": "画板目录建不出来，检查一下这本书还在不在"}
    try:
        _write_bytes(path, raw)
    except OSError:
        return {"ok": False, "path": "", "bytes": len(raw), "fmt": fmt,
                "msg": "导出文件没写进去，磁盘或权限出问题了"}
    return {"ok": True, "path": path, "bytes": len(raw), "fmt": fmt,
            "msg": "已导出 %s（%s）" % (fmt.upper(), _human_size(len(raw)))}


# ─────────────────────────── 拼进笔记 ───────────────────────────

def _one_line(text) -> str:
    """标题压成一行：换行进 Markdown 会把标题截断成两半。"""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _alt(text) -> str:
    """图片替代文字里不能出现方括号和反斜杠，否则链接语法被自己的标题弄坏。"""
    return _one_line(text).replace("\\", "").replace("[", "").replace("]", "")


def to_markdown(board, base_dir=None) -> str:
    """一块板 → 一条笔记条目的 Markdown（标题 + 说明 + 指向导出图片的相对链接）。

    只写真的存在的导出物：给了 base_dir 就以磁盘为准，没给就认 list_boards 送来的
    has_png / has_svg 标记；两样都没有就写一句「还没有导出图片」。宁可少一张图，
    也不能在 notes.md 里留一个点开是坏图链接的坑 —— 那份 md 是要被别的软件读走的。
    """
    doc = board if isinstance(board, dict) else {}
    title = _one_line(doc.get("title")) or "未命名画板"
    caption = _text(doc.get("caption"), MAX_CAPTION)
    # 没有 id 就别现编一个：编出来的名字指向一个不存在的文件，正是要避免的那种链接。
    sid = slug(doc.get("id")) if doc.get("id") else ""
    lines = ["### 画板：%s" % title]
    if caption:
        lines.append(caption)
    lines.append("")

    rel = ""
    for ext, flag in ((PNG_EXT, "has_png"), (SVG_EXT, "has_svg")):
        if not sid:
            break
        name = "%s%s" % (sid, ext)
        if base_dir:
            exists = os.path.exists(os.path.join(dir_of(base_dir), name))
        else:
            exists = bool(doc.get(flag))
        if exists:
            rel = name
            break
    if rel:
        # 链接一律用 /：notes.md 会进 Git、阅读器、flomo，反斜杠在 Windows 之外都不认。
        lines.append("![%s](%s/%s)" % (_alt(title), BOARD_DIR, rel))
    else:
        lines.append("（这块板还没有导出图片）")
    return "\n".join(lines).rstrip() + "\n"
