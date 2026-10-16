"""写作平台：一篇稿子怎么存在磁盘上、怎么统计、怎么出去、怎么让 AI 搭手。

**一篇稿子就是「一本书」，住在书库的第七格里。**

    书库/写作/write_1a2b3c4d5e6f/
        draft.md        正文（唯一真源）
        meta.json       标题 / 字数 / rev / 时间 / 已看过的灵感角度
        versions/       快照，回退用（保留最近 30 份）

这么定不是为了省事，是为了**不再多学一套东西**：目录、分类账（夹子、标签、状态）、
封面、AI 小结、清理白名单、MCP 那一整批工具、`safe_book_dir` 那道路径闸，全都按
「书库里一个书号」在工作。稿子既然也是书号，这些能力一行接口都不用改就全跟着走 ——
1.0.1 那次「换目录不换 id」攒下的红利，这一轮继续吃。

三条要紧的规矩写在代码里，改之前先看懂：

  · **存盘认 rev，不认时间戳。** 编辑器有多个入口（界面、MCP、命令行改文件），
    拿时间比大小会出现「后写的被先写的覆盖」。每次成功保存 rev +1，写的时候带上
    「我读到的那个 rev」，对不上就不写、把服务端那份带回去让人选。
  · **快照只在真改动之后拍。** 空转的自动存盘（用户没打字、只是切了下标签页）
    不许拍快照，否则 30 个位置全被同一段内容占满，回退等于没有。
  · **给 AI 的笔记原文只进提示词，不进日志、不进回执。** 素材是从便签与划线里
    捞的用户私货，落到进展栏就等于落进仓库。
"""

import hashlib
import json
import os
import re
import time
import uuid

import book_export
import book_layout
from md_fence import prose_lines

MODULE = "write"
PREFIX = "write_"
DRAFT_NAME = "draft.md"
REV_STEP_SNAP = 20        # 每存这么多次拍一份快照
KEEP_VERSIONS = 30        # 快照最多留这么多份

# 「AI 接着写多少」由用户点选，不让模型自己决定 —— 各家模型对「继续写一段」的
# 理解差得太远，有人写 40 字有人写 800 字，只有把它换成明确的 max_tokens + stop
# 才拿得住。文案给的是「一句 / 一段 / 三个要点 / 写完这一节」。
LENGTHS = [
    ("sent", "一句", 200, ["\n"]),
    ("para", "一段", 520, ["\n\n"]),
    ("points", "三个要点", 700, ["\n\n\n"]),
    ("section", "写完这一节", 1400, ["\n## ", "\n# "]),
]
LENGTH_OF = {k: (label, tokens, stop) for k, label, tokens, stop in LENGTHS}


# ---------- 脏账本兜底 ----------
#
# meta.json 与 _books.json 都是用户能直接编辑的文本文件，旧版本也落过别的形状。
# 读它们的地方原来一律直读直用：一个 "很多" 落在 words 上，写作屏左栏整栏加载不出来
# （/api/writer 抛穿），存盘那一刀也被顶回 —— 用户以为存上了，其实一个字没落；
# "灵感角度" 这种裸字符串会被 list() 逐字拆成「灵」「感」「角」「度」再写回去，
# 提示词里点名的不再是角度。下面三把尺子只在读账本时用一次，后面照旧直读。

def _int(v, default=0):
    """账上的数字字段：认得出就转成 int，认不出回默认，绝不抛。

    `"42"`、`42.0` 这类认得出的形状照认（旧写法 `int(v or 0)` 对 `"42"` 能过、
    对 `"42.0"` 与 `null` 都会抛），只把真认不出的换成默认。
    """
    try:
        return int(v)
    except Exception:
        pass
    try:
        return int(float(str(v).strip()))
    except Exception:
        return default


def _as_list(v):
    """脏账里的容器：认 list/tuple；裸字符串收成「一个元素」。

    裸字符串要是直接 `for s in "灵感角度"` 会被逐字拆开再写回去，一个角度变成四个
    单字角度；不是容器的（数字 / null / dict）一律当空，免得后面拿它当清单遍历。
    """
    if isinstance(v, (list, tuple)):
        return list(v)
    if isinstance(v, str):
        return [v] if v.strip() else []
    return []


def _sparks(v):
    """灵感角度那一栏：收成一份干净的字符串清单（短、非空、保序去重）。

    与 `remember_sparks` 写入时用的是同一把尺（strip / 40 字 / 去重），这样
    「读出来再存回去」不会越洗越长，也不会因为手改过一次就多出重复项。
    """
    out = []
    for a in _as_list(v):
        s = str(a).strip()[:40]
        if s and s not in out:
            out.append(s)
    return out


# 落单的半个代理码点（U+D800–U+DFFF 单独出现、不成对）：浏览器的 JS 字符串被拦腰
# 截断时会留下它，JSON 那边 `json.loads` 收得下（形如 "\ud83d"），于是它一路走到
# 落盘那一步 —— 正文按 utf-8 写、账本按 ensure_ascii=False 写，两处都当场
# UnicodeEncodeError，用户看到的是「写了一大段，一存就没了」，书目录里还留一个
# 半截 .tmp。它是坏输入不是内容，洗掉比让整篇稿子存不下去强。成对的代理码点不必管：
# json 解码时已经合成一个真字符，这里只会摘到落单的那个。
# 与 mindmap / board / person / anna_state 那几处同一口径，写作这一格此前没有。
_SURROGATE = re.compile("[\ud800-\udfff]")


def _drop_surrogates(v):
    """把要落盘（或要回给界面）的东西里落单的半个代理码点摘干净，dict / list 逐层走。

    只摘落单的那半个，真 emoji、ZWJ 连字（🧑‍💻）一个字不动 —— 洗过头就把一个
    表情拆成两个乱码。返回的是新对象（原对象不就地改），免得同一份账本在两处
    看见不同的值。
    """
    if isinstance(v, str):
        return _SURROGATE.sub("", v)
    if isinstance(v, dict):
        return {k: _drop_surrogates(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_drop_surrogates(x) for x in v]
    return v


# ---------- 路径与 id ----------

def new_id():
    """新稿号：前缀 + 12 位十六进制。前缀让 book_layout 靠前缀也能认出模块。"""
    return PREFIX + uuid.uuid4().hex[:12]


def ok_id(book_id):
    return book_layout.ok_id(book_id) and str(book_id).startswith(PREFIX)


def dir_of(books_root, book_id):
    """这篇稿子的目录（会顺手把 书库/写作/ 建出来，但稿子目录要 create 才建）。"""
    return os.path.join(book_layout.book_dir(books_root, MODULE), str(book_id))


def draft_path(draft_dir):
    return os.path.join(draft_dir, DRAFT_NAME)


def meta_path(draft_dir):
    return os.path.join(draft_dir, "meta.json")


def versions_dir(draft_dir):
    return os.path.join(draft_dir, "versions")


# ---------- 统计 ----------

_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")
_DIGIT_RUN = re.compile(r"\d+(?:[.,]\d+)*")
_MD_NOISE = re.compile(r"^\s*(?:#{1,6}\s*|[-*+]\s+|\d+[.)]\s+|>\s*)", re.M)
# 写作页工具条写的内联标记。属性里那些 color / background / span 会被当成英文词
# 数进字数里 —— 染一次颜色多出三个「词」，界面上那个数字马上就骗人了。
# 代价是代码块里举例写的 <div> 也一并剥掉：那本来也不该算进正文。
_TAG_NOISE = re.compile(r"<[^>]+>")


def words(text):
    """字数统计。中文按字、英文按词、数字按组，这是写作软件通用的口径。

    给的是三个数：`total` 是界面上那个「字数」（中文字 + 英文词 + 数字组），
    `han` 与 `latin` 拆开留着，将来要按语种算字数（中译英、限 800 字）不用再改接口。
    先剥掉 markdown 的行首记号和内联标记，否则「## 标题」和染色的那层 span 会被算进字数。
    """
    body = _TAG_NOISE.sub("", _MD_NOISE.sub("", text or ""))
    han = len(_HAN.findall(body))
    latin = len(_LATIN_WORD.findall(_HAN.sub(" ", body)))
    digit = len(_DIGIT_RUN.findall(body))
    return {"han": han, "latin": latin, "numbers": digit,
            "total": han + latin + digit, "chars": len(text or "")}


# 围栏代码块：规矩只有一处，在 md_fence（CommonMark 那几条，含「反引号起的围栏、
# 信息串里不许再有反引号」）。写作页的工具条只插 ```，可粘贴进来的稿子什么写法都有。
# 下面保留 `_prose_lines` 这个名字与一层薄壳：大纲、标题兜底与门禁都按它认「哪些行
# 算正文」，实现一律转发 md_fence，不再自己抄一份状态机 —— 抄两份就会在这些边角上
# 慢慢走散（导入切章那边早就按同一条规矩判，两处对不齐就会「切章看得见、大纲看不见」）。
_FENCE_BARE = re.compile(r"[`~]{3,}\Z")


def _prose_lines(text):
    """把围栏代码块整段跳过，逐行给 (原始行号, 行内容)（口径见 md_fence）。

    **为什么标题与大纲必须跳代码块**：粘一段带 `# 注释` 的代码进稿子，若还照
    `^#+ ` 认标题，大纲里就多出并不存在的章节，稿子以代码开头时标题还会变成
    ``` 那一串反引号。`book_export.md_to_html` 导出时早就是这么分的，两处口径
    得对齐，否则「导出看到的结构」和「AI 读到的大纲」是两份。

    行号仍按**原文**数，所以要 enumerate 原始行再筛，不能筛完再编号 ——「跳到这一节」
    靠的就是原文行号。
    """
    return prose_lines(text)


def outline(text):
    """大纲：把 # 行按出现顺序列出来，给侧边目录与「跳到这一节」。

    只数代码块外面的 `#` —— 块里的井号是注释，不是章节。
    """
    out = []
    for i, line in _prose_lines(text):
        m = re.match(r"^(#{1,6})\s+(.*\S)\s*$", line)
        if m:
            out.append({"level": len(m.group(1)), "title": m.group(2).strip(), "line": i})
    return out


def title_from(text, fallback="未题名"):
    """标题兜底：先认第一行 `#`，再认第一个非空行的前 24 字。

    代码块里的行不算候选：以一段代码开头的稿子，标题不该是 ``` 或某句注释。
    整篇都是代码时给 `fallback` —— 那确实还没有标题，比硬凑一行代码诚实。
    """
    for _i, line in _prose_lines(text):
        s = line.strip()
        if not s or _FENCE_BARE.match(s):
            continue
        m = re.match(r"^#{1,6}\s+(.*\S)$", s)
        if m:
            return m.group(1).strip()[:80]
        return s[:24]
    return fallback


# ---------- 读写 ----------

def blank_meta(book_id, title=""):
    now = int(time.time())
    return {"id": book_id, "title": title or "", "source": MODULE, "format": MODULE,
            "created": now, "updated": now, "rev": 0, "words": 0, "chars": 0,
            "sparks": [], "done": True}


def load_meta(draft_dir):
    """读这一篇的 meta，顺手把手改脏的字段收成能用的形状。

    这里是读稿子唯一的入口（`read` / `brief` / `note` / `save` / 书架卡片都走它），
    所以脏字段在这一处收干净，后面各处照旧直读。收不出数的按 0 / 空清单算，
    这篇稿子不因为一个字段坏掉就整篇消失；下一次存盘会把收好的形状写回去，
    账本自己就修好了。
    """
    m = book_layout.read_meta(draft_dir)
    if not m or m.get("format") != MODULE:
        return None
    try:
        born = int(os.path.getmtime(meta_path(draft_dir)))
    except OSError:
        born = int(time.time())
    m["rev"] = _int(m.get("rev"), 0)
    m["words"] = _int(m.get("words"), 0)
    m["created"] = _int(m.get("created"), born)
    m["updated"] = _int(m.get("updated"), m["created"])
    m["sparks"] = _sparks(m.get("sparks"))
    m["title"] = str(m.get("title") or "")[:80]
    # 手改过的 meta.json 里也可能留着落单的半个代理码点（json.load 收得下它）：
    # 读出来就洗掉，否则它会跟着 read() / brief() 回给界面，而那条回包同样是
    # ensure_ascii=False 编码的，留着一个坏字符连响应都发不出去。
    return _drop_surrogates(m)


def read_text(draft_dir):
    """读正文。文件不在回空串，坏字节按书库那把共用的容错尺换成替代符。

    draft.md 是用户能直接编辑的正文，也可能被云同步 / 旧版本 / 别的编辑器写回来：
    一个手滑另存成 GBK 的稿子、或一次写到一半的断电，严格 UTF-8 读法当场
    UnicodeDecodeError —— 而这里是 read / brief / note / save / 书架卡片共用的
    唯一读口，一句异常就让整篇稿子打不开、存盘也被顶回。章节正文早就按「认不出的
    字节换 U+FFFD、正文其余照旧」的口径收在 `book_export.read_chapter_file`
    一处（书库第七格的稿子本来也是书库里的一本书），这里接过去，不再各写一把。
    """
    text = book_export.read_chapter_file(draft_path(draft_dir))
    return _drop_surrogates(text) if text is not None else ""


def read(books_root, book_id):
    """整篇读出来：元信息 + 正文 + rev。没有这篇稿子给 None（调用方说人话）。"""
    if not ok_id(book_id):
        return None
    d = dir_of(books_root, book_id)
    m = load_meta(d)
    if not m:
        return None
    m["text"] = read_text(d)
    m["outline"] = outline(m["text"])
    m["count"] = words(m["text"])
    return m


def _write_text_atomic(path, text):
    """文本落盘的唯一出口（正文 / meta / 册账都走这儿）：先写自己那份临时件再原子顶替。

    写作这条路**不止一个写手**：后端是 `ThreadingHTTPServer`，界面的自动存盘、手点
    「保存」、另一个标签页、MCP 那几个工具可以同时进 `save()`，而这条路上没有任何锁
    （`ui_server._WRITE_LOCK` 只盖配置 / 书架 / 时长 / 计划那几处）。旧写法把临时件
    写死成 `path + ".tmp"`，两个写手同时进来时有两种代价，与 `anna_state` / `flomo_notes`
    当初那一格完全一样：

      · **写坏**：后来者 `open(..., "w")` 把前一个人写了一半的临时件截断，两边各自按
        自己的文件偏移往下写，`os.replace` 把两份交织在一起的那一份顶成正式稿子 ——
        用户看到的是「保存成功了、稿子却成了两段话拼在一起」；
      · **当场抛**：先收尾的那一个 `os.replace` 把共用的临时件整个搬走，后一个写手轮到
        自己那一句时临时件已经不在，抛 `FileNotFoundError`；这条异常从 `/api/writer/save`
        直接穿出去，界面拿到 500，而它明明刚提示过「已保存」。

    临时名带 pid + 一小串随机，与 `ui_server._write_bytes`（封面那一格）同一把尺子：
    同进程两个请求线程、两个进程都不会抢同一个名字。写失败先把临时件收干净再抛，
    旧档始终是完整的那一份。
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.%d.%s.tmp" % (path, os.getpid(), uuid.uuid4().hex[:8])
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        # 兜 BaseException 而不是 Exception：Ctrl-C / 停止键走的是 KeyboardInterrupt /
        # SystemExit，它们不继承 Exception，只兜 Exception 会在退出路径上把临时件留下。
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def create(books_root, title="", text=""):
    book_id = new_id()
    d = dir_of(books_root, book_id)
    if os.path.exists(d):
        return None
    os.makedirs(versions_dir(d), exist_ok=True)
    meta = blank_meta(book_id, title or title_from(text))
    body = _drop_surrogates(text) if isinstance(text, str) else ""
    _write_text_atomic(draft_path(d), body)
    meta["rev"] = 1
    meta["words"] = words(body)["total"]
    meta = _write_meta(d, meta)
    return book_id, meta


def save(books_root, book_id, text, title=None, rev=None):
    """存正文。返回 (ok, meta 或 冲突信息)。

    `rev` 给的是「我读到的那一份的第几版」。对不上就**不写**，把服务端当前正文一起
    带回去 —— 冲突要当场可见，不能悄悄覆盖，这是编辑器最容易丢稿的一种做法。
    不给 rev（老接口、MCP 直存）时按「覆盖」处理，但仍会先拍一份快照，改坏了能回退。
    """
    d = dir_of(books_root, book_id)
    meta = load_meta(d)
    if not meta:
        return False, {"msg": "这篇稿子不在了"}
    server = int(meta.get("rev", 0))
    conflict = None
    if rev is not None:
        try:
            client = int(rev)
        except Exception:
            client = server
        if client != server:
            conflict = {"rev": server, "text": read_text(d), "updated": meta.get("updated")}
            return False, {"msg": "这份稿子在别处被改过", "conflict": conflict}
    body = _drop_surrogates(text) if isinstance(text, str) else ""
    old = read_text(d)
    if body != old:
        snapshot(d, meta)                 # 只在真有改动前留一份旧的
        meta["rev"] = server + 1
    if title is not None and str(title).strip()[:80] != str(meta.get("title") or ""):
        meta["title"] = _drop_surrogates(str(title)).strip()[:80] or title_from(body)
    elif not meta.get("title"):
        meta["title"] = title_from(body)
    meta["words"] = words(body)["total"]
    # 书目那条路（list_books）读的是 meta.chars，两个数保持一致：
    # 书架卡片、AI 小结、MCP 列书看到的字数才和写作平台里那个「字数」是同一个。
    meta["chars"] = meta["words"]
    meta["updated"] = int(time.time())
    _write_text_atomic(draft_path(d), body)
    meta = _write_meta(d, meta)
    return True, meta


def _write_meta(draft_dir, meta):
    """把 meta 落盘，回**真正写下去的那一份**（落单的坏字符已经摘掉）。

    调用方拿这份回执去回界面，而回包也是 ensure_ascii=False 编码的 —— 所以不能
    只洗要写盘的副本：留在手里那一份带着坏字符，下一步就是发响应时 UnicodeEncodeError。
    """
    meta = _drop_surrogates(dict(meta or {}))
    _write_text_atomic(meta_path(draft_dir),
                       json.dumps(meta, ensure_ascii=False, indent=2))
    return meta


def snapshot(draft_dir, meta):
    """把**当前**正文拍成一份快照（在覆盖它之前调用）。

    留多少份按 REV_STEP_SNAP 决定：rev 到整倍数才拍。手动点「存一份快照」走
    `snapshot_now()`，不受这条限制 —— 用户明说要留，就该留得住。
    """
    if int(meta.get("rev", 0)) % REV_STEP_SNAP != 0:
        return None
    return snapshot_now(draft_dir, read_text(draft_dir))


def snapshot_now(draft_dir, text):
    """把一份正文拍成快照（文件名按秒，同一秒里连点两次也分得开）。

    快照也走 `_write_text_atomic` 这同一个口子，不再裸 `open(path, "w")`：

      · **半截 .md 会被当成一份快照**。`versions()` 认的就是目录里以 `.md` 收尾的
        文件，写一半被强杀（关窗口 / 断电 / Ctrl-C）留下的那半篇，照样出现在快照
        列表里；用户点「回退到这一版」，拿回来的就是半篇稿 —— 而且 `restore()`
        先给自己拍的那一份快照，落盘的也是这段半篇正文。
      · **同一秒里两个写手抢同一个名字**。后端是 `ThreadingHTTPServer`，自动存盘
        每 `REV_STEP_SNAP` 次拍一份（`snapshot()`）、用户手点「存一份快照」也走这里，
        两条线可能落在同一秒：旧的「先看文件在不在、再打开写」之间有一个窗口，两边
        都认下同一个 `…-123456.md`，各自按自己的偏移往下写，顶上去的两篇交织在一起。
        现在写的是自己那份带 pid + 随机串的临时件，`os.replace` 一步顶替，最终落在
        这个名字上的永远是**某一份完整正文**。

    版本额度只按真快照（`.md`）算：写一半被强杀留下的临时件不该占掉一份真快照的
    名额，更不该顶掉最老的那一份。
    """
    vd = versions_dir(draft_dir)
    os.makedirs(vd, exist_ok=True)
    name = "%s.md" % time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(vd, name)
    n = 1
    while os.path.exists(path):           # 同一秒里连点两次保存也要分得开
        path = os.path.join(vd, "%s-%d.md" % (name[:-3], n))
        n += 1
    _write_text_atomic(path, text or "")
    keep = [x for x in sorted(os.listdir(vd)) if x.endswith(".md")]
    for stale in keep[:-KEEP_VERSIONS]:
        try:
            os.remove(os.path.join(vd, stale))
        except OSError:
            pass
    return os.path.basename(path)


def versions(draft_dir):
    vd = versions_dir(draft_dir)
    out = []
    if not os.path.isdir(vd):
        return out
    for name in sorted(os.listdir(vd), reverse=True):
        if not name.endswith(".md"):
            continue
        p = os.path.join(vd, name)
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        out.append({"name": name, "size": size,
                    "at": int(os.path.getmtime(p)),
                    "words": words(_read_file(p))["total"]})
    return out[:KEEP_VERSIONS]


def _read_file(p):
    """读一份快照正文：与 read_text 同一把尺（坏字节换替代符），读不到回空串。

    快照和 draft.md 是同一类东西（用户能直接改的 .md），`versions()` / 回退都从
    这里读 —— 一份另存成 GBK 的旧快照不该让整列快照列不出来、也回退不了。
    """
    text = book_export.read_chapter_file(p)
    return text if text is not None else ""


def version_text(draft_dir, name):
    """读某一份快照。文件名只认 [0-9A-Za-z._-]，越界的直接拒 —— 这是拼路径的地方。"""
    s = str(name or "")
    if not re.match(r"^[0-9A-Za-z._\-]{1,64}\.md\Z", s):
        return None
    p = os.path.join(versions_dir(draft_dir), s)
    if ".." in s:
        return None
    return _read_file(p) if os.path.isfile(p) else None


def restore(books_root, book_id, name):
    text = version_text(dir_of(books_root, book_id), name)
    if text is None:
        return False, {"msg": "那份快照找不到了"}
    # 回退也是「改」，所以先把现在这份拍下来，回错了还能再回来
    d = dir_of(books_root, book_id)
    snapshot_now(d, read_text(d))
    return save(books_root, book_id, text, rev=None)


def note(books_root, book_id):
    """这篇稿子的「一句备注」：写进书架卡片与列表，不含正文。"""
    meta = load_meta(dir_of(books_root, book_id)) if ok_id(book_id) else None
    if not meta:
        return {}
    return {"title": meta.get("title") or "", "words": int(meta.get("words") or 0),
            "rev": int(meta.get("rev") or 0), "updated": int(meta.get("updated") or 0)}


def seen_sparks(books_root, book_id):
    meta = load_meta(dir_of(books_root, book_id))
    return list((meta or {}).get("sparks") or [])


def remember_sparks(books_root, book_id, angles):
    """记下这批灵感的角度，下一批提示词里点名不要重复。

    只留 24 条：全留会让提示词越来越长、也记不到重点，滚动窗口够用了。
    """
    d = dir_of(books_root, book_id)
    meta = load_meta(d)
    if not meta:
        return False
    pool = list(meta.get("sparks") or [])
    for a in angles or []:
        a = str(a).strip()[:40]
        if a and a not in pool:
            pool.append(a)
    meta["sparks"] = pool[-24:]
    _write_meta(d, meta)
    return True


# ---------- 列表（书架那一格用） ----------

def brief(draft_dir):
    meta = load_meta(draft_dir)
    if not meta:
        return None
    return {"id": meta.get("id"), "title": meta.get("title") or title_from(read_text(draft_dir)),
            "words": int(meta.get("words") or 0), "rev": int(meta.get("rev") or 0),
            "created": int(meta.get("created") or 0), "updated": int(meta.get("updated") or 0),
            "versions": len(os.listdir(versions_dir(draft_dir)))
            if os.path.isdir(versions_dir(draft_dir)) else 0}


def list_drafts(books_root):
    out = []
    top = book_layout.module_path(books_root, MODULE)
    if not os.path.isdir(top):
        return out
    for name in sorted(os.listdir(top)):
        b = brief(os.path.join(top, name))
        if b:
            out.append(b)
    out.sort(key=lambda x: -x["updated"])
    return out


# ---------- 书与章节 ----------
#
# 「书」是写作那一格多出的一层：一本书 = 若干章按顺序排在一起。这一层只落在
# `书库/写作/_books.json` 一份账上（书号 → 章的稿号列表），章节本身仍是原来那篇稿子 ——
# 目录、rev、快照、AI 三件套、MCP 那些工具一个字都没改。
#
# 为什么不做成「一本书一个目录、章住在里面」：那样 `safe_book_dir` 那道路径闸、
# `book_layout` 的模块前缀、清理白名单、按书号工作的一整批接口全要跟着改一遍，
# 而用户要的只是「我建一本书，第一章、第二章、第三章这样排下来」。
#
# 两条规矩：
#   · **一章最多属于一本书**。在 A 书里点「加进来」会自动从 B 书出去 ——
#     界面上不该出现同一份稿子挂在两处、删一处另一处跟着没的场面。
#   · **删书只删这个「组」，不动章**。稿子是用户写的字，组是随时能重排的东西，
#     两件事的重量不一样，不能一起丢。真要丢稿子，界面另有一颗「删掉这一章」，
#     走的是原有的 `book.delete`（书号还是那个书号，那道路径闸也照旧管得住）。

BOOKS_NAME = "_books.json"
BOOK_PREFIX = "bk_"


def book_id_ok(bid):
    return bool(re.match(r"^%s[0-9a-f]{12}\Z" % BOOK_PREFIX, str(bid or "")))


def books_path(books_root):
    return os.path.join(book_layout.book_dir(books_root, MODULE), BOOKS_NAME)


def new_book_id():
    return BOOK_PREFIX + uuid.uuid4().hex[:12]


def blank_book(title):
    now = int(time.time())
    return {"id": new_book_id(), "title": _drop_surrogates(str(title or "")).strip()[:60],
            "chapters": [], "created": now, "updated": now}


def load_books(books_root):
    """读那一份册账。文件不在 / 读坏了都给空表（写作屏还能照常开）。

    顺手把已经不在盘上的章剔掉：删稿子走的是原有那条路（`book.delete`），
    它不认识册账，所以账上会留下指向空气的章号。留着它，界面上就有一颗
    点了打不开的「章节」。

    读口用 `utf-8-sig`：这本册账是用户能直接编辑的 JSON，记事本「另存为」、
    部分云同步客户端回写时会在开头多出三个字节的 BOM；按 utf-8 读会当场
    JSONDecodeError，被下面那句宽 except 当成「没有账」收成空表 —— 而紧接着任何一次
    建书 / 改名 / 加章都会把这份空表写回去，用户搭的书目就此静默消失（稿子还在，
    组没了）。仓库里其余账本（book_layout.read_meta / flomo_notes / ui_server 那几本）
    早已是这个口径，这里补齐；写盘口径不变，utf-8-sig 不会给文件加 BOM。
    """
    try:
        with open(books_path(books_root), encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        return []
    out = []
    for b in (data.get("books") if isinstance(data, dict) else None) or []:
        if not isinstance(b, dict) or not book_id_ok(b.get("id")):
            continue
        keep, seen = [], set()
        for did in _as_list(b.get("chapters")):
            did = str(did or "")
            # 同一本书里也不许重复列同一个章号：重复一次，界面上就是两行一模一样的。
            if not ok_id(did) or did in seen:
                continue
            if not os.path.isdir(dir_of(books_root, did)):
                continue
            seen.add(did)
            keep.append(did)
        out.append({"id": str(b["id"]), "title": _drop_surrogates(str(b.get("title") or "")),
                    "chapters": keep,
                    "created": _int(b.get("created"), 0),
                    "updated": _int(b.get("updated"), 0)})
    return out


def save_books(books_root, books):
    p = books_path(books_root)
    # 册账同样是 ensure_ascii=False 写的：一处落单的半个代理码点就能让整本册账
    # 写不出去（写作屏的书树跟着一起空），落盘前统一摘干净。
    _write_text_atomic(p, json.dumps(
        _drop_surrogates({"v": 1, "books": list(books or [])}),
        ensure_ascii=False, indent=2))
    return True


def find_book(books, book_id):
    return next((b for b in books if b.get("id") == str(book_id or "")), None)


def book_of(books, draft_id):
    """这一章在哪本书里；没归册给空串。"""
    for b in books:
        if draft_id in (b.get("chapters") or []):
            return b["id"]
    return ""


def touch_book(book):
    book["updated"] = int(time.time())
    return book


def create_book(books_root, title=""):
    books = load_books(books_root)
    book = touch_book(blank_book(title or "未命名的一本书"))
    books.insert(0, book)
    save_books(books_root, books)
    return book


def rename_book(books_root, book_id, title):
    books = load_books(books_root)
    book = find_book(books, book_id)
    if not book:
        return False, {"msg": "这本书不在了"}
    name = _drop_surrogates(str(title or "")).strip()[:60]
    if not name:
        return False, {"msg": "书名不能是空的"}
    book["title"] = name
    touch_book(book)
    save_books(books_root, books)
    return True, book


def delete_book(books_root, book_id):
    """只把这个「组」取消，章一篇都不动 —— 它们回到未归册那一堆里。"""
    books = load_books(books_root)
    left = [b for b in books if b["id"] != str(book_id)]
    if len(left) == len(books):
        return False, {"msg": "这本书不在了"}
    save_books(books_root, left)
    return True, {"msg": "已经不成书了，章节还在"}


def add_chapter(books_root, book_id, draft_id="", title=""):
    """往这本书里加一章：给稿号就归进来，不给就先建一篇空稿再归进来。

    从别的书里过来时**先把旧的那处摘掉**（一章只能属于一本书），并且照旧
    排在末尾 —— 顺序由 `order_chapter` 单独管，加章这件事不该顺手把人排好的序打乱。
    """
    books = load_books(books_root)
    book = find_book(books, book_id)
    if not book:
        return False, {"msg": "这本书不在了"}
    did = str(draft_id or "")
    if did and not ok_id(did):
        return False, {"msg": "这不是写作那一格的稿号"}
    if not did:
        made = create(books_root, title or "新的一章", "")
        if not made:
            return False, {"msg": "这一章建不出来"}
        did = made[0]
    elif not os.path.isdir(dir_of(books_root, did)):
        return False, {"msg": "这一章的稿子不在了"}
    for other in books:
        if did in (other.get("chapters") or []):
            other["chapters"] = [x for x in other["chapters"] if x != did]
            touch_book(other)
    if did not in (book.get("chapters") or []):
        book["chapters"] = list(book["chapters"] or []) + [did]
    touch_book(book)
    save_books(books_root, books)
    return True, {"book": book, "id": did}


def remove_chapter(books_root, draft_id):
    """把这一章从书里拿出来（稿子留着）。"""
    books = load_books(books_root)
    hit = False
    for b in books:
        if draft_id in (b.get("chapters") or []):
            b["chapters"] = [x for x in b["chapters"] if x != draft_id]
            touch_book(b)
            hit = True
    if not hit:
        return False, {"msg": "这一章没有归在任何一本书里"}
    save_books(books_root, books)
    return True, {"msg": "已经出册，稿子还在未归册那一堆里"}


def order_chapter(books_root, book_id, draft_id, delta):
    """章在书里挪一格：delta 是 ±1（上移 / 下移）。到顶到底就原样返回。"""
    books = load_books(books_root)
    book = find_book(books, book_id)
    if not book:
        return False, {"msg": "这本书不在了"}
    ids = list(book.get("chapters") or [])
    if draft_id not in ids:
        return False, {"msg": "这一章不在这本书里"}
    at = ids.index(draft_id)
    # delta 从请求体里来，可能是 "上" / null / 对象 —— 认不出就当「不挪」，
    # 不能让它一个 int() 把这一趟 500 掉（用户点了上移却什么都没发生，比报错好查）。
    to = max(0, min(len(ids) - 1, at + _int(delta, 0)))
    if to == at:
        return True, {"book": book, "moved": False}
    ids.pop(at)
    ids.insert(to, draft_id)
    book["chapters"] = ids
    touch_book(book)
    save_books(books_root, books)
    return True, {"book": book, "moved": True}


def book_detail(books_root, book, with_text=False):
    """一本书摊开：章的清单（标题 / 字数 / 更新时间）+ 合并稿的字数。

    界面上那一列「第 X 章」的行号在这里给，不在前端数 —— 前端数出来的是它看到的那份
    清单的顺序，一旦和册账不一致（有章被删过、有章刚挪过程），行号会和「第 3 章」这种
    写在标题里的字对不上，用户会以为章乱了。
    """
    ids = list(book.get("chapters") or [])
    rows = []
    total = 0
    for i, did in enumerate(ids):
        one = read(books_root, did) if with_text else None
        b = brief(dir_of(books_root, did))
        if not b and not one:
            continue
        b = b or {}
        total += int(b.get("words") or 0)
        rows.append({"no": i + 1, "id": did, "title": b.get("title") or "",
                     "words": int(b.get("words") or 0),
                     "updated": int(b.get("updated") or 0),
                     "rev": int(b.get("rev") or 0),
                     "text": (one or {}).get("text") or ""} if with_text else
                      {"no": i + 1, "id": did, "title": b.get("title") or "",
                       "words": int(b.get("words") or 0),
                       "updated": int(b.get("updated") or 0),
                       "rev": int(b.get("rev") or 0)})
    return {"id": book.get("id"), "title": book.get("title") or "",
            "created": int(book.get("created") or 0),
            "updated": int(book.get("updated") or 0),
            "count": len(rows), "words": total, "chapters": rows}


def merge_text(books_root, book_id, sep="\n\n---\n\n"):
    """整本合稿：按册账里的顺序把章接起来，每章前面补一行它自己的标题。

    章标题为什么由这里补：章文件的正文里没有「第 X 章」那一行（那是稿子的标题，
    存在 meta 里），不补的话合出来是一串没有分节的长文，导 EPUB 也切不出目录。
    已经有 `#` 开头的正文不重复补 —— 用户自己在章里写了大标题时，尊重他写的。
    """
    books = load_books(books_root)
    book = find_book(books, book_id)
    if not book:
        return None
    det = book_detail(books_root, book, with_text=True)
    parts = []
    for row in det["chapters"]:
        body = (row.get("text") or "").strip()
        head = str(row.get("title") or "").strip()
        if head and not re.match(r"^\s*#{1,6}\s", body):
            parts.append("## %s\n\n%s" % (head, body) if body else "## %s" % head)
        else:
            parts.append(body)
    return {"title": book.get("title") or "", "parts": parts,
            "text": sep.join([p for p in parts if p.strip()])}


def as_book_dir_merged(books_root, book_id, tmp_root):
    """把一本书摊成「一本书」的目录（chapters/0001.md、0002.md …）。

    一章一个文件而不是合成一篇：`book_export.build_epub` 是按 chapters 切章的，
    一章一个正好就是 EPUB 的目录，导出来的电子书能按章跳。
    """
    books = load_books(books_root)
    book = find_book(books, book_id)
    if not book:
        return None
    det = book_detail(books_root, book, with_text=True)
    dest = os.path.join(tmp_root, book_id)
    os.makedirs(os.path.join(dest, "chapters"), exist_ok=True)
    for row in det["chapters"]:
        with open(os.path.join(dest, "chapters", "%04d.md" % row["no"]),
                  "w", encoding="utf-8") as f:
            f.write((row.get("text") or "").strip() + "\n")
    with open(os.path.join(dest, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"title": book.get("title") or "未命名的一本书", "author": "",
                   "source": MODULE, "done": True}, f, ensure_ascii=False)
    return dest


# ---------- 导出：排版模板 ----------
#
# 模板只是「一套 CSS 变量 + 页眉页脚」，正文一律走 book_export.md_to_html 那条已经
# 跑通的书转 HTML 的路。这样三件事一起解决：渲染口径与导出 EPUB/PDF 完全一致、
# 不用再造第二个 markdown 解析器、模板之间不会渲染出两种正文。

TPL_LABELS = [("plain", "黑白简约"), ("business", "商务蓝"), ("verdant", "青翠"),
              ("letter", "米白书简")]

_TPL_BASE = """
:root {
  --bg: %(bg)s; --ink: %(ink)s; --faint: %(faint)s; --rule: %(rule)s;
  --accent: %(accent)s; --pad: %(pad)s;
  --serif: %(serif)s; --sans: %(sans)s;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: var(--bg); }
body { color: var(--ink); font-family: var(--serif); }
.sheet { width: %(width)s; padding: %(ptop)s var(--pad) %(pbottom)s; background: var(--bg); }
h1.title { font-size: 34px; line-height: 1.25; margin: 0 0 6px; letter-spacing: .5px;
           color: var(--ink); font-weight: 650; }
.lead { font-size: 14px; color: var(--faint); margin: 0 0 22px; letter-spacing: 1.2px; }
.rule { height: 3px; background: var(--accent); width: 46px; margin: 0 0 26px; border: 0; }
.body { font-size: 17px; line-height: 1.86; }
.body h1, .body h2, .body h3 { font-family: var(--sans); color: var(--ink);
  line-height: 1.4; margin: 30px 0 12px; }
.body h2 { font-size: 22px; padding-left: 11px; border-left: 4px solid var(--accent); }
.body h3 { font-size: 18px; }
.body p { margin: 0 0 15px; }
.body blockquote { margin: 18px 0; padding: 10px 16px; color: var(--faint);
  border-left: 3px solid var(--rule); background: rgba(0,0,0,.022); }
.body ul, .body ol { padding-left: 22px; margin: 0 0 15px; }
.body li { margin-bottom: 7px; }
.body code { font-family: ui-monospace, Menlo, monospace; font-size: 15px;
  background: rgba(0,0,0,.05); padding: 1px 5px; border-radius: 4px; }
.body pre { background: rgba(0,0,0,.045); padding: 14px 16px; border-radius: 8px;
  overflow-x: hidden; }
.body pre code { background: none; padding: 0; }
.body img { max-width: 100%%; height: auto; }
.body hr { border: 0; border-top: 1px solid var(--rule); margin: 26px 0; }
/* 工具条能插表格、下划线、删除线和待办勾，这几样在导出页上必须有自己的样子 ——
   正文由 book_export.md_to_html 渲染，它只管产出结构，长什么样归模板。 */
.body table { width: 100%%; border-collapse: collapse; margin: 0 0 16px;
  font-size: 16px; }
.body th, .body td { border: 1px solid var(--rule); padding: 8px 11px;
  text-align: left; vertical-align: top; }
.body th { font-family: var(--sans); font-weight: 600; background: rgba(0,0,0,.035); }
.body u { text-decoration: underline; }
.body s { text-decoration: line-through; color: var(--faint); }
.body li.task { list-style: none; position: relative; }
.body li.task::before { content: ""; position: absolute; left: -19px; top: .55em;
  width: 13px; height: 13px; border: 1.5px solid var(--rule); border-radius: 4px;
  background: var(--bg); }
.body li.task.done::before { border-color: var(--accent); background: var(--accent); }
.body li.task.done { color: var(--faint); }
.foot { margin-top: 34px; padding-top: 12px; border-top: 1px solid var(--rule);
  display: flex; justify-content: space-between; font-family: var(--sans);
  font-size: 12px; letter-spacing: .6px; color: var(--faint); }
"""

TEMPLATES = {
    "plain": dict(bg="#ffffff", ink="#16181d", faint="#8a8f99", rule="#e3e6ea",
                  accent="#16181d", pad="56px", width="760px", ptop="56px",
                  pbottom="40px", serif='-apple-system, "PingFang SC", "Noto Serif SC", serif',
                  sans='-apple-system, "PingFang SC", sans-serif'),
    # 商务蓝：页眉压一条深蓝带，标题换无衬线，读起来像一份正式稿
    "business": dict(bg="#f6f9fd", ink="#12233c", faint="#6b7f97", rule="#d6e2ef",
                     accent="#1f5fbf", pad="60px", width="780px", ptop="64px",
                     pbottom="44px", serif='"Songti SC", "PingFang SC", serif',
                     sans='-apple-system, "PingFang SC", sans-serif'),
    "verdant": dict(bg="#f4f9f4", ink="#17281e", faint="#6f8676", rule="#d6e6da",
                    accent="#2f8f5b", pad="58px", width="770px", ptop="58px",
                    pbottom="42px", serif='"PingFang SC", "Hiragino Sans GB", sans-serif',
                    sans='"PingFang SC", sans-serif'),
    "letter": dict(bg="#fbf7ef", ink="#2b2418", faint="#93876f", rule="#e6dcc9",
                   accent="#b9822f", pad="64px", width="720px", ptop="68px",
                   pbottom="48px", serif='"Songti SC", "STSong", "PingFang SC", serif',
                   sans='"PingFang SC", sans-serif'),
}


def tpl_width(tpl):
    """模板的版心宽度（整数像素）。截图的视口宽度必须用它，不能写死 760：
    视口比版心宽，右侧多出一条白；比版心窄，正文被挤到下一栏，导出的行数
    和在预览里看到的不一样。"""
    s = str((TEMPLATES.get(tpl) or TEMPLATES["plain"]).get("width") or "760px")
    m = re.match(r"^(\d+(?:\.\d+)?)", s)
    return int(float(m.group(1))) if m else 760


def export_html(text, title="", tpl="plain", footer=True, date_str="",
                brand="归藏", lead=""):
    """把一篇稿子拼成「能直接丢给 Chromium 渲染」的一份 HTML。

    页脚那行（工具名 + 日期）由调用方给什么就排什么：`brand` / `date_str`
    各是一列，只给一个就只排一个，两个都空（或 `footer=False`）就整行不留。
    截图发朋友圈时想要署名和日期，贴进自己博客时两样都不想要，所以这里
    不提供默认值之外的任何兜底 —— 不塞东西比塞了再让人删掉可靠。
    """
    import book_export
    css = _TPL_BASE % TEMPLATES.get(tpl, TEMPLATES["plain"])
    body = book_export.md_to_html(text or "")
    head = '<h1 class="title">%s</h1>' % _esc(title or "")
    leadline = '<p class="lead">%s</p>' % _esc(lead) if lead else ""
    rule = '<hr class="rule" />'
    cells = [x for x in (str(brand or "").strip(), str(date_str or "").strip()) if x]
    if not footer:
        cells = []
    foot = ('<div class="foot">%s</div>'
            % "".join("<span>%s</span>" % _esc(c) for c in cells)) if cells else ""
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<title>%s</title><style>%s</style></head><body>"
            "<div class=\"sheet\">%s%s%s<div class=\"body\">%s</div>%s</div>"
            "</body></html>") % (_esc(title or "未命名稿"), css, head, leadline, rule,
                                 body, foot)


def _esc(s):
    import html
    return html.escape(str(s if s is not None else ""), quote=True)


def as_book_dir(books_root, book_id, tmp_root):
    """把稿子摊成「一本书」的样子（chapters/0001.md + meta.json），交给
    book_export 现成的 EPUB / PDF 路线。临时目录由调用方负责清理。"""
    import json
    d = read(books_root, book_id)
    if not d:
        return None
    dest = os.path.join(tmp_root, book_id)
    os.makedirs(os.path.join(dest, "chapters"), exist_ok=True)
    with open(os.path.join(dest, "chapters", "0001.md"), "w", encoding="utf-8") as f:
        f.write(d["text"])
    with open(os.path.join(dest, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"title": d.get("title") or "未命名稿", "author": "",
                   "source": MODULE, "done": True}, f, ensure_ascii=False)
    return dest


# ---------- 导出：图片 / PDF ----------
#
# 两条路都用内置的铬内核渲染 `export_html()` 那份 HTML，和「导出整本书」共用一套
# 排版口径。为什么非要请浏览器：正文里的中文、代码块、列表缩进要让它们和界面上
# 一模一样，用 PIL 手搓行距是一条永远对不齐的路。
#
# 三个坑都在这段里处理掉（都是实测踩出来的）：
#   · 中文字形没加载完就截图 → 出方框。先等 `document.fonts.ready`，再用
#     `document.fonts.check()` 兜一层，探的是模板真用的那款字体。
#   · `device_scale_factor` 会连画面一起放大，所以视口宽度给「模板的 CSS 宽度」，
#     出来的像素数才等于 宽 × 倍数，不会比预览宽一档。
#   · `full_page` 截图会把 `position:fixed` 的页眉与背景逐段重画一遍；模板里没有
#     fixed 元素，所以安全 —— 往模板里加 fixed 的东西前要先想清楚这一条。

FONT_PROBE = '16px "PingFang SC"'


def _render_wait(page):
    """等字体真的可用；等不到也不失败，顶多少一种字形，不该让导出报错。"""
    try:
        page.evaluate("() => document.fonts.ready.then(() => true)")
        return bool(page.evaluate("() => document.fonts.check(%r)" % FONT_PROBE))
    except Exception:
        return False


def _launch(pw):
    """起铬内核。和 book_export.build_pdf 同一个打法：浏览器缓存位置由进程环境
    （PLAYWRIGHT_BROWSERS_PATH）决定，装成 .app 时壳已经指好了。"""
    return pw.chromium.launch()


def export_image(html, dest, width=760, scale=2):
    """一张长图：稿子整页截下来，宽度锁死在排版模板那一档。

    `scale` 1/2/3 对应「清晰但小 / 常规 / 很大」，界面只给这三档，
    因为再高的倍数发朋友圈会被压回去，白等一趟渲染。
    """
    import tempfile
    from playwright.sync_api import sync_playwright
    font_ok = False
    tmp = tempfile.mkdtemp(prefix="guizang-write-")
    path = os.path.join(tmp, "page.html")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        with sync_playwright() as pw:
            browser = _launch(pw)
            try:
                page = browser.new_page(viewport={"width": int(width), "height": 1000},
                                        device_scale_factor=max(1, min(int(scale), 3)))
                page.goto("file://" + path, timeout=120000)
                font_ok = _render_wait(page)
                page.screenshot(path=dest, full_page=True)
            finally:
                browser.close()
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return dest, font_ok


def export_pdf(html, dest, paper="A4"):
    """一份 PDF：走铬内核打印，和 build_pdf 同一套边距量级。"""
    import tempfile
    from playwright.sync_api import sync_playwright
    tmp = tempfile.mkdtemp(prefix="guizang-write-")
    path = os.path.join(tmp, "page.html")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        with sync_playwright() as pw:
            browser = _launch(pw)
            try:
                page = browser.new_page()
                page.goto("file://" + path, timeout=120000)
                _render_wait(page)
                # page.pdf() 不吃 timeout（超时由上面那一跳控制），别往上加
                page.pdf(path=dest, format=paper, print_background=True,
                         margin={"top": "16mm", "bottom": "16mm",
                                 "left": "15mm", "right": "15mm"})
            finally:
                browser.close()
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return dest


# ---------- AI：续写 / 灵感 / 润色 ----------

SYS_WRITE = (
    "你是这篇文章的作者本人，不是助手。风格、人称、句式、用词都跟着前文走，"
    "不许突然开始总结、不许加「首先其次」、不复述已经写过的话、不写「总而言之」。"
    "只用中文，不用 emoji，不要给你的续写加标题或引号。"
)

SYS_SPARK = (
    "你是作者的选题编辑。只给方向，不给成稿：每条不超过 22 个字，"
    "彼此角度不同（可以是一句追问、一个反例、一个具体场景、一个数据缺口、一段个人经历）。"
    "不说「建议你可以考虑」这类话，不用 emoji，不要编号以外的符号。"
)

SYS_POLISH = (
    "你是文字编辑。只做「改写」这一件事：保持原意、保持人称与信息点，"
    "该删的删、该顺的顺，不添加新事实、不引用外部资料、不写评论。"
    "只输出改写后的正文本身，不要前后缀、不要引号、不要解释。"
)

POLISH_TONES = [("smooth", "顺一顺"), ("tight", "删掉水分"),
                ("concrete", "写得更具体"), ("formal", "收得正式")]


def material_block(items, limit=6000):
    """把用户的便签 / 划线拼成一段素材，塞进提示词。

    夹到 `limit` 字：素材越全不一定越好，长上下文会把模型的注意力摊薄，
    而且上游按 token 收费。给的是「最近且较长」优先，纯文本，不带原文之外的解释。
    """
    keep, used = [], 0
    for it in items or []:
        s = str(it or "").strip()
        if not s:
            continue
        if used + len(s) > limit:
            # 单独一条就超过上限时旧写法直接 break，keep 还是空的，于是整段素材被
            # 丢成 ""：用户明明挂着便签，模型却一个字都没收到。夹一段带上，至少让
            # 开头那部分事实进得去。
            if not keep:
                keep.append(s[:limit])
                used = limit
            break
        keep.append(s)
        used += len(s)
    if not keep:
        return ""
    return "〔作者的笔记素材，可作事实与角度来源，不要照抄原句〕\n" + \
        "\n".join("- " + x for x in keep)


def cont_messages(text, material="", length="para", brief_hint=""):
    label, tokens, stop = LENGTH_OF.get(length, LENGTH_OF["para"])
    tail = (text or "")[-4000:]
    user = "前文（接着这里往下写，输出紧接其后、不要重复前文）：\n<<<\n%s\n>>>\n" % tail
    if material:
        user += "\n" + material_block([material]) if isinstance(material, str) else \
            "\n" + material_block(material)
    if brief_hint:
        user += "\n作者此刻的意图：%s\n" % str(brief_hint).strip()[:200]
    user += "\n这次只续%s。" % label
    return [{"role": "system", "content": SYS_WRITE}, {"role": "user", "content": user}], \
        {"max_tokens": tokens, "stop": stop}


def spark_messages(text, material="", seen=(), n=3):
    tail = (text or "")[-2500:]
    user = "这篇稿子写到这儿：\n<<<\n%s\n>>>\n" % tail
    if material:
        user += "\n" + (material_block([material]) if isinstance(material, str)
                        else material_block(material))
    if seen:
        user += "\n已经给过这些角度，别再给同类的：%s\n" % "、".join(
            [str(x)[:40] for x in list(seen)[-12:]])
    user += "\n给我 %d 个往下写的方向，一行一个，格式严格照「- 方向」，不要编号、不要解释。" % n
    return [{"role": "system", "content": SYS_SPARK}, {"role": "user", "content": user}]


def parse_sparks(raw, n=3):
    """把模型给的那几行收成干净的方向列表：去掉行首符号、去重、限长、最多 n 条。

    模型不总按格式来（有时给「1. 」、有时给「**方向**：」），所以解析要宽进；
    但出口必须严 —— 界面上那三张卡是等高的，脏行会让卡片塌掉。
    """
    out = []
    for line in str(raw or "").splitlines():
        s = line.strip()
        if not s:
            continue
        # 只摘「真的像是列表标记」的那一小段：项目符号本身，或 1–2 位编号 + 分隔符。
        # 旧写法 `[-*+•\d]+[.)、:：]?\s*` 里分隔符与空白都是可选的，于是任何以数字
        # 开头的方向都会被啃掉一截 —— 「2024 年的数据说明了什么」变成「年的数据说明了
        # 什么」、「3 个能立刻用的方法」变成「个能立刻用的方法」。方向是给人看的，开头
        # 那个数字往往就是信息本身，不能当成编号吃掉。`{1,2}` 挡住年份；`(?!\d)` 挡住
        # 「3.14」「1:30」这类以数字开头的正文。
        s = re.sub(r"^(?:[-*+•]+\s*|\d{1,2}、\s*|\d{1,2}[).:：](?!\d)\s*)", "", s)
        s = re.sub(r"^[「《\"“'\[]+|[」》\"”'\]]+$", "", s).strip()
        s = s.replace("**", "").strip()
        if not s or s in out:
            continue
        out.append(s[:40])
        if len(out) >= n:
            break
    return out


def polish_messages(sel, tone="smooth", material=""):
    label = dict(POLISH_TONES).get(tone, "顺一顺")
    user = "把下面这段「%s」：\n<<<\n%s\n>>>\n" % (label, (sel or "")[:6000])
    if material:
        user += "\n" + (material_block([material]) if isinstance(material, str)
                        else material_block(material))
    user += "\n只输出改写后的这一段。"
    return [{"role": "system", "content": SYS_POLISH}, {"role": "user", "content": user}]


def diff_blocks(old, new):
    """把「AI 想改的」切成一块一块，交给界面逐块接受或拒绝。

    为什么后端来切而不是前端：回退与「一键接受」的正确性取决于切得对不对，
    而这块逻辑在 Python 里能直接单测（difflib 是标准库），放到页面上就变成
    一串没法单独验的 DOM 操作。原文一个字都不会被改动 —— 这里只给差异。

    每块带 `at` / `end`：这一处改动在**选中段落**里的字符区间。有了它，界面
    接受一块就是把那一段换掉，接受多块就按 `at` 从后往前换 —— 从前往后换会
    让后面那些坐标整体错位，改到不该改的字上，那是最伤的一种 bug。
    """
    import difflib
    old = old or ""
    new = new or ""
    a = [x for x in re.split(r"(?<=[。！？；\n])", old) if x != ""]
    b = [x for x in re.split(r"(?<=[。！？；\n])", new) if x != ""]
    # 每句在原文里的起点。按编号乘长度算不出来（切掉过空串），只能逐句累加；
    # 除了空串这里没丢过任何字符，所以累加值就等于原串下标。
    start, acc = [], 0
    for x in a:
        start.append(acc)
        acc += len(x)
    start.append(acc)
    blocks = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag == "equal":
            continue
        blocks.append({"op": {"replace": "改", "insert": "增", "delete": "删"}.get(tag, tag),
                       "at": start[i1], "end": start[i2],
                       "old": "".join(a[i1:i2]), "new": "".join(b[j1:j2])})
    return blocks


def fingerprint(text):
    """正文指纹，给「内容没变就别拍快照」那一类判断用。"""
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:12]
