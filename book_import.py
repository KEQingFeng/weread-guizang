# -*- coding: utf-8 -*-
"""把自己手里的书（Markdown / TXT / EPUB / PDF）收进书库，交给内置阅读器读。

收进来之后，她就是一个普通的书目录 —— chapters/*.md + images/ + meta.json，
跟「取回来的书」长得一模一样。所以阅读器、导出 EPUB/PDF、在文件管理器里定位
这些能力一行都不用改，直接就对导入的书生效；区别只在 meta.json 里多一个
`source: "local"` 标记，前端据此把它和微信读书来的书分开显示。

格式处理的分工：

- Markdown / TXT：纯标准库。先按标题切章（`# 章名` 或 `第X章`），切不动就
  按字数与空行切块 —— 保证每一章都在「能一口气读完」的量级。
- EPUB：标准库 zipfile + ElementTree + HTMLParser。按 spine 顺序取每个 xhtml，
  转成阅读器认得的 Markdown 子集；图片抽出来放进 images/。
- PDF：借 pypdf 抽文字（纯 Python，跟着 requirements 一起装）。抽不出文字的
  扫描件会明确报错，不含糊过去 —— 让用户知道「这本得先 OCR」而不是拿到空书。

正文是不可信的外部内容：这里只做「拆成 markdown 文本」这一件事，不执行、
不解释其中的任何东西；链接只保留 http/https/mailto/锚点，其余一律丢掉。
"""

import html
import html.parser
import io
import json
import os
import posixpath
import re
import shutil
import time
import urllib.parse
import uuid
import zipfile
from xml.etree import ElementTree as ET

import book_layout
import md_fence

# 单份导入的上限。base64 会胖三分之一，前端也按这个数先拦一道。
MAX_BYTES = 200 * 1024 * 1024
# EPUB 解压后的总量上限：防压缩炸弹把内存吃干
MAX_UNZIP = 400 * 1024 * 1024
# 切块目标：一段落一段落攒，攒到这个字数就开新章
CHUNK_CHARS = 6000


# ─────────────────────────── 格式识别 ───────────────────────────

_EXT_FORMAT = {
    ".md": "md", ".markdown": "md", ".mdown": "md", ".mkd": "md",
    ".txt": "txt", ".text": "txt", ".log": "txt",
    ".epub": "epub",
    ".pdf": "pdf",
}

FORMAT_LABEL = {"md": "Markdown", "txt": "TXT", "epub": "EPUB", "pdf": "PDF"}


def guess_format(filename, data=b""):
    """先看扩展名，看不出再看文件头。认不出返回空串。"""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in _EXT_FORMAT:
        return _EXT_FORMAT[ext]
    head = bytes(data[:8])
    if head[:4] == b"%PDF":
        return "pdf"
    if head[:2] == b"PK":
        # 压缩包，再看有没有 EPUB 的特征文件
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if "mimetype" in z.namelist():
                    return "epub"
        except Exception:
            pass
    return ""


# ─────────────────────────── 切章 ───────────────────────────

_H_MD = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
# 纯文本里的章标题：「第X章 / 第X回」或「Chapter N」。整行就是标题，故不取分组。
_H_PLAIN = re.compile(
    r"^\s*(?:第\s*[0-9一二三四五六七八九十百零两]{1,6}\s*[章回节篇]"
    r"|Chapter\s+[0-9IVXLCivxlc]{1,6}"
    r"|CHAPTER\s+[0-9IVXLCivxlc]{1,6})\s*[^\n]{0,40}$")

# 围栏代码块的识别统一交给 md_fence（全仓唯一那把尺子）。这里只留一层薄壳：
# 切章要把 mask 和 `text.split("\n")` 一行行对齐，所以行由调用方自己切、再交出去标。
def _fenced_mask(text):
    """逐行标出「这一行属于围栏代码块」。True 的行不许再拿去认标题。

    代码块里的 `# 安装依赖` 是注释，不是章节 —— 不标出来的话，导入一本带代码示例的
    Markdown 书会照着注释行切章，目录里多出一批并不存在的章节、正文也被剪成碎块。
    口径（开栏三个以上同种字符、反引号 info string 里不许再有反引号、收栏同种且不短于
    开栏并只剩空白、没有收栏的块一直吃到文末）只有一份实现，在 `md_fence`；这一层
    只负责「按 \\n 切行」，好让返回的 mask 与 `text.split("\\n")` 逐行对得上。

    切行本身也走 `md_fence.split_lines`，不再自己写 `.split("\\n")`：md_fence 的
    `prose_lines`（大纲 / 导图 / 笔记）与这里必须是同一条行尺子，各写一份迟早分叉
    —— `str.splitlines()` 认的那串 Unicode 换行就是分叉点，一分开切章与大纲就会
    对同一段稿子给出两种结构。
    """
    return md_fence.fence_mask(md_fence.split_lines(text))


def _split_at(text, pattern):
    """按标题正则切章，返回 [(标题, 正文), ...]。标题前若还有散段落，单列一章。

    代码块里的行一律不认标题，理由见 `_fenced_mask`。
    """
    lines = (text or "").split("\n")
    mask = _fenced_mask(text)
    heads = []
    for i, ln in enumerate(lines):
        if mask[i]:
            continue
        m = pattern.match(ln)
        if not m:
            continue
        if m.groups() and m.lastindex and m.lastindex >= 2:
            title = (m.group(2) or "").strip()
        else:
            title = ln.strip()
        heads.append((i, re.sub(r"^#+\s*", "", title).strip()))
    if not heads:
        return []
    out = []
    pre = "\n".join(lines[:heads[0][0]]).strip()
    if pre:
        out.append(("", pre))
    for idx, (start, title) in enumerate(heads):
        end = heads[idx + 1][0] if idx + 1 < len(heads) else len(lines)
        body = "\n".join(lines[start + 1:end]).strip()
        out.append((title, body))
    return out


def _chunk(text, size=CHUNK_CHARS):
    """按空行分段再攒块：段太碎就并，攒到 size 字开一块。单段就超长的硬切。"""
    blocks, buf, n = [], [], 0
    for p in re.split(r"\n\s*\n", text or ""):
        p = p.strip()
        if not p:
            continue
        buf.append(p)
        n += len(p)
        if n >= size:
            blocks.append("\n\n".join(buf))
            buf, n = [], 0
    if buf:
        blocks.append("\n\n".join(buf))
    out = []
    for b in blocks:
        if len(b) <= size * 2:
            out.append(b)
        else:
            out.extend(b[i:i + size] for i in range(0, len(b), size))
    return out


def _chunk_titles(text):
    chunks = _chunk(text)
    if not chunks:
        return []
    if len(chunks) == 1:
        return [("", chunks[0])]
    return [("第 %d 节" % (i + 1), c) for i, c in enumerate(chunks)]


def split_markdown(text):
    """md 优先按标题切：从最浅的一级标题往下找，选中「这一级至少有两段」的那级；
    `#` 是章、`##` 是小节时就不会把小节拆成章。切不出来再按字数分块。"""
    text = text or ""
    mask = _fenced_mask(text)
    levels = {}
    for ln, in_code in zip(text.split("\n"), mask):
        if in_code:
            continue
        m = _H_MD.match(ln)
        if m:
            n = len(m.group(1))
            levels[n] = levels.get(n, 0) + 1
    if levels:
        for lv in sorted(levels):
            if levels[lv] < 2:
                continue
            pat = re.compile(r"^#{%d}\s+(.+?)\s*$" % lv)
            parts = [(t, b) for t, b in _split_at(text, pat) if b]
            if len(parts) >= 2:
                return parts
        lv = min(levels)
        pat = re.compile(r"^#{%d}\s+(.+?)\s*$" % lv)
        parts = [(t, b) for t, b in _split_at(text, pat) if b]
        if parts:
            return parts
    return _chunk_titles(text)


def split_plain(text):
    """纯文本优先按「第X章 / Chapter N」切；切不出就按字数切块。"""
    parts = [(t, b) for t, b in _split_at(text, _H_PLAIN) if b]
    if len(parts) >= 2:
        return parts
    return _chunk_titles(text)


# 带 BOM 的按 BOM 认。长 BOM 必须排在短 BOM 前面（UTF-32 的小端 BOM 也以 FF FE 开头，
# 先撞上 utf-16 就会把一半字节解成别的字）。utf-8-sig 顺手把 BOM 本身摘掉。
_BOMS = (
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xfe\xff", "utf-16"),
    (b"\xff\xfe", "utf-16"),
)


def _looks_utf16(data):
    """无 BOM 时唯一能拿来认 utf-16 的旁证：整篇高位字节都 ≤ 0x9F。

    汉字在 UTF-16LE 里是两个字节一个字，常用汉字的第二个字节（高位）落在 0x4E–0x9F；
    而 GBK / GB18030 常用汉字的第二个字节（尾字节）最小也是 0xA1。所以「数据是偶数字节，
    且所有奇数位字节都 ≤ 0x9F」既认得整篇中文的 utf-16，又撞不上 GBK。单靠这一条不够
    （ASCII 的 utf-16 满篇 0x00，见 _decode_text 里先走的那道闸），但两者合起来，
    无 BOM 的 utf-16 中文就不必再被 gb18030 抢走。
    """
    if len(data) < 2 or len(data) % 2:
        return False
    return max(data[1::2]) <= 0x9F


def _decode_text(data):
    """按常见中文编码依次试，全不行就按 utf-8 强解（宁可个别字坏，不整本丢）。

    为什么不能像以前那样把 `utf-16` 直接排进候选列表：**无 BOM 的 utf-16 解码几乎
    不会报错**，它会拿两个字节当一个字硬解，而 GBK / Big5 的中文正文刚好都是偶数长度，
    于是整篇被抢先解成别的字。这不是「个别字坏」，是满屏乱码 —— 例如「中文测试」
    (8 字节 gb18030) 会被解成「탖쓎」，用户导入一本 GBK 的 TXT 看到的就是这个。

    所以顺序是：BOM → 含 0x00 的 utf-16 → utf-8 → 像 utf-16 的 → gb18030 → big5。
    两道 utf-16 的闸都必要：含 0x00 那条管带 ASCII / 换行的（无 BOM 的 utf-16 只要有
    一个换行就带 0x00），_looks_utf16 那条管整篇纯汉字、一个 0x00 都没有的。
    """
    if not data:
        return ""
    for bom, enc in _BOMS:
        if data.startswith(bom):
            try:
                return data.decode(enc)
            except (UnicodeDecodeError, LookupError):
                break
    if b"\x00" in data:
        try:
            return data.decode("utf-16")
        except (UnicodeDecodeError, LookupError):
            pass
    try:
        return data.decode("utf-8")
    except (UnicodeDecodeError, LookupError):
        pass
    if _looks_utf16(data):
        try:
            return data.decode("utf-16")
        except (UnicodeDecodeError, LookupError):
            pass
    for enc in ("gb18030", "big5"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


# 中日韩文字、假名、全角标点：这些字之间本来就不写空格，拼回去时也不能补。
_CJK_CH = re.compile(r"[\u2e80-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff"
                     r"\uf900-\ufaff\uff00-\uffef]")
# 上一行以这些字符收尾时，下一行不该空一格接上去（左括号、上引号、连字符）。
_OPEN_TAIL = set("([{<\u2018\u201c\u3008\u300a\u300c\u300e\u3010\u3014\u3016"
                 "-\u2010\u2011\u2012\u2013\u2014")
# 下一行以这些字符开头时，前面不能多出一个空格（右括号、收尾标点、下引号）。
_CLOSE_HEAD = set(")]}>.,;:!?\u2019\u201d\u3009\u300b\u300d\u300f\u3011\u3015\u3017"
                  "\uff0c\u3002\uff01\uff1f\uff1b\uff1a\u2014\u2026")


def _join_wrapped(prev, nxt):
    """把被 PDF 硬换行劈开的两行拼回一行：拉丁词之间补一个空格，中文之间不补。

    为什么不能一律直接相加：英文 PDF 每一行都抽成独立一行，`quick brown` /
    `fox jumps` 直接相加会拼出 `brownfox`，整本书的词都粘在一起。中文反过来，
    字与字之间没有词距，补空格反而是错的。所以只看接缝两侧那一个字符。
    """
    if not prev or not nxt:
        return prev + nxt
    a, b = prev[-1], nxt[0]
    if _CJK_CH.match(a) or _CJK_CH.match(b):
        return prev + nxt
    if a in _OPEN_TAIL or b in _CLOSE_HEAD:
        return prev + nxt
    return prev + " " + nxt


def _normalize_lines(text):
    """PDF 抽出来的文字一行一断：把不成段的单换行并回同一段。"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    out = []
    enders = ("。", "！", "？", "；", "：", ".", "!", "?", ";", ":", "”", "」", "』")
    for ln in text.split("\n"):
        s = ln.strip()
        if not s:
            out.append("")
        elif out and out[-1] and not out[-1].endswith(enders):
            out[-1] = _join_wrapped(out[-1], s)
        else:
            out.append(s)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


# ─────────────────────────── XML / HTML 杂活 ───────────────────────────

def _tag(el):
    t = el.tag
    return t.rsplit("}", 1)[-1] if isinstance(t, str) else ""


def _find_all(root, name):
    return [e for e in root.iter() if _tag(e) == name]


# 实体闸门必须扫**全文**，不能只看开头 4KB：EPUB 是从外面带进来的东西（安娜的档案
# 领回来的、用户自己找的包），在 OPF 前面垫一段超长注释就能把 DOCTYPE / ENTITY 顶出
# 窗口，闸门等于不存在。而 expat 对**内部**实体是照展开的：几百字节的「十亿笑」能解出
# 几十万字符，再深几层就是几个 G（tests/check_import_entity.py 里留了这条对照，免得
# 收口哪天被拿掉还在空转）。DOCTYPE / ENTITY 是标记而不是内容（正文里的尖括号要写成
# `&lt;`），正常 OPF 里一个都没有，所以扫全篇不会误伤。与 feed.import_opml 同一把尺子
# （那边也是全篇扫，见 tests/check_opml_safety.py）。
_DTD_DECL = re.compile(rb"<!(?:DOCTYPE|ENTITY)", re.I)
_DTD_DECL_STR = re.compile(r"<!(?:DOCTYPE|ENTITY)", re.I)


def _has_dtd_decl(raw):
    """整份文档里有没有 DTD / 实体声明。bytes 与 str 两条路都不先解码整份再扫。"""
    if isinstance(raw, str):
        return bool(_DTD_DECL_STR.search(raw))
    try:
        return bool(_DTD_DECL.search(raw))
    except TypeError:            # memoryview 这类不吃 bytes 正则的
        return bool(_DTD_DECL.search(bytes(raw)))


def _parse_xml(raw):
    """解析不可信的 XML（EPUB 的 OPF）。

    OPF 是外面带进来的东西，一律按不可信处理：先挡掉 DTD / 实体声明（防实体
    膨胀与外部实体读取），再交给标准解析器。解析失败返回 None，让调用方走兜底
    路径（按 manifest / 自然序排章节），不因为一个坏 OPF 就整本导入失败。
    """
    if _has_dtd_decl(raw):
        return None
    try:
        parser = ET.XMLParser()
        # 老版本 ElementTree 允许注入实体表；清空它，杜绝自定义实体。
        try:
            parser.entity.clear()  # type: ignore[attr-defined]
        except Exception:
            pass
        return ET.fromstring(raw, parser=parser)
    except Exception:
        return None


_IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp"}


def _safe_name(name):
    """扁平化落盘名：只留文件名，非法字符换成下划线。"""
    base = os.path.basename(name or "")
    base = re.sub(r"[^0-9A-Za-z._\-]", "_", base)
    if not base or base.startswith("."):
        base = "img_" + base.lstrip(".")
    return base[:80] or "img"


def _unique_names(orig_names):
    """把一批原始资源名映射成互不冲突的落盘名。"""
    used, out = set(), {}
    for orig in orig_names:
        base = _safe_name(orig)
        if base in used:
            root, ext = os.path.splitext(base)
            i = 2
            while "%s_%d%s" % (root, i, ext) in used:
                i += 1
            base = "%s_%d%s" % (root, i, ext)
        used.add(base)
        out[orig] = base
    return out


class _ToMarkdown(html.parser.HTMLParser):
    """xhtml 片段 → 阅读器认得的那点 Markdown 子集。

    只留标题/段落/粗斜体/行内码/引用/列表/图片/链接/分隔线；认不出的标签把文字留下、
    标签吞掉。链接只保留 http/https/mailto/锚点，其余（站内相对路径、脚本式 URL）
    一律丢掉 —— 正文是不可信的外部内容。
    """

    _VOID = {"meta", "link", "base", "img", "br", "hr", "col", "source"}
    _SKIP = {"script", "style", "title", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.pre = 0
        self.skip = 0
        self.href = None
        self.lists = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self._SKIP:
            self.skip += 1
            return
        if tag in ("meta", "link", "base"):
            return
        if self.skip:
            return
        if tag == "pre":
            self.pre += 1
            self.out.append("\n\n```\n")
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag == "img":
            src = a.get("src") or ""
            if src:
                self.out.append("![%s](%s)" % (a.get("alt") or "", src))
        elif tag == "a":
            self.href = a.get("href") or ""
            self.out.append("[")
        elif tag in ("ul", "ol"):
            self.lists.append(tag)
            self.out.append("\n")
        elif tag == "li":
            self.out.append("\n" + ("1. " if self.lists and self.lists[-1] == "ol" else "- "))
        elif tag == "blockquote":
            self.out.append("\n\n> ")
        elif tag == "br":
            self.out.append("\n")
        elif tag == "hr":
            self.out.append("\n\n---\n\n")
        elif tag in ("p", "div", "section", "article", "table", "tr", "figure"):
            self.out.append("\n\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            if self.skip:
                self.skip -= 1
            return
        if self.skip:
            return
        if tag == "pre":
            self.pre = max(0, self.pre - 1)
            self.out.append("\n```\n\n")
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("\n\n")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag == "a":
            self._close_a()
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            self.out.append("\n")
        elif tag in ("p", "div", "section", "article", "table", "tr", "blockquote", "figure"):
            self.out.append("\n\n")

    def _close_a(self):
        if self.href is None:
            return
        href = self.href.strip()
        self.href = None
        low = href.lower()
        if low.startswith(("http://", "https://", "mailto:")) or low.startswith("#"):
            self.out.append("](%s)" % href)
        else:
            self.out.append("]")     # 站内相对链接之类：文字留着，链接丢掉

    def handle_data(self, data):
        if self.skip:
            return
        self.out.append(data if self.pre else re.sub(r"\s+", " ", data))

    def text(self):
        s = "".join(self.out)
        s = re.sub(r"[ \t]+\n", "\n", s)
        s = re.sub(r"[ \t]{2,}", " ", s)
        s = re.sub(r"\n{3,}", "\n\n", s)
        return s.strip()


def _md_from_xhtml(txt):
    p = _ToMarkdown()
    try:
        p.feed(txt)
        p.close()
    except Exception:
        pass
    return p.text()


# ─────────────────────────── EPUB 拆解 ───────────────────────────

def epub_zip_error(exc):
    """压缩包这一类坏法 → 一句给用户的话。

    不把 zipfile 的英文原文（`File is not a zip file` 之类）递给用户：那不是内容，
    是解释器的措辞；加密那种还会带上压缩包里的文件名。
    """
    if isinstance(exc, zipfile.BadZipFile):
        return "这个 EPUB 打不开（文件可能没下全，或者根本不是一个 EPUB 压缩包）"
    if isinstance(exc, RuntimeError):
        return "这个 EPUB 加了密，归藏打不开（先去掉 DRM 再导）"
    return "这个 EPUB 用的压缩方式归藏解不开，换一份再试"


def read_epub(data):
    """拆一份 EPUB；压缩包坏了 / 加了密一律翻成人话的 ValueError。

    扩展名一旦是 `.epub`，`guess_format` 就不再翻回头去看内容，所以「这份文件其实
    不是个压缩包」只有这一层挡得住。不挡的话 `zipfile` 的 BadZipFile 会穿过
    `import_book` 一路到路由的兜底 except：用户拿到的是 500 加一句英文
    `File is not a zip file`（安娜那条路更糟，账本上记的是 `BadZipFile` 这个名字）。
    """
    try:
        return _read_epub(data)
    except ValueError:
        raise                        # 「解压后太大」这些本来就已经是给用户的话
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError) as e:
        raise ValueError(epub_zip_error(e))


def _read_epub(data):
    """EPUB → (章节文档, 图片, 封面, 元信息)。

    章节文档 = [(包内路径, xhtml 文本)]（按 spine 顺序）；图片 = [(原始名, bytes)]；
    封面 = 原始名或 None；元信息 = {"title": …, "creator": …}（OPF 的 dc:title /
    dc:creator，可能为空串）。整包只读进内存，不按名字解压到磁盘 —— 压缩包里的路径
    不可信，落盘名前一律走 _safe_name。
    """
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        total = sum(z.getinfo(n).file_size for n in names)
        if total > MAX_UNZIP:
            raise ValueError("这本 EPUB 解压后太大了（超过 %d MB），先确认文件没问题"
                             % (MAX_UNZIP // 1024 // 1024))
        opf = next((n for n in names if n.lower().endswith(".opf")), None)
        base = posixpath.dirname(opf) if opf else ""
        manifest, spine, cover_id = {}, [], None
        meta_info = {"title": "", "creator": ""}
        if opf:
            root = _parse_xml(z.read(opf))
            if root is not None:
                # dc:title / dc:creator：OPF 里带命名空间的 Dublin Core 元素，取第一个
                # 非空文本。压缩包可能塞多个（多语言副标题等），取第一个够用。
                for el in _find_all(root, "title"):
                    t = "".join(el.itertext()).strip()
                    if t:
                        meta_info["title"] = t
                        break
                for el in _find_all(root, "creator"):
                    t = "".join(el.itertext()).strip()
                    if t:
                        meta_info["creator"] = t
                        break
                for item in _find_all(root, "item"):
                    iid, href = item.get("id"), item.get("href")
                    if iid and href:
                        props = item.get("properties") or ""
                        manifest[iid] = (href, (item.get("media-type") or "").lower(), props)
                        if "cover-image" in props:
                            cover_id = iid
                for meta in _find_all(root, "meta"):
                    if (meta.get("name") or "").lower() == "cover" and meta.get("content"):
                        cover_id = meta.get("content")
                for ref in _find_all(root, "itemref"):
                    spine.append(ref.get("idref"))

        def _resolve(href):
            full = posixpath.normpath(posixpath.join(base, href)).lstrip("/")
            if full in names:
                return full
            for n in names:
                if n == href or n.endswith("/" + href):
                    return n
            return None

        htmls = lambda h: h.lower().endswith((".xhtml", ".html", ".htm", ".xhtm"))
        order = [manifest[i][0] for i in spine if i in manifest and htmls(manifest[i][0])]
        if not order:
            order = [m[0] for m in manifest.values() if htmls(m[0])]
        if not order:
            order = sorted(n for n in names if htmls(n))

        docs = []
        for href in order:
            full = _resolve(href)
            if not full:
                continue
            try:
                raw = z.read(full)
            except Exception:
                continue
            try:
                txt = raw.decode("utf-8")
            except UnicodeDecodeError:
                txt = raw.decode("utf-8", "replace")
            docs.append((full, txt))

        images = []
        for n in names:
            if os.path.splitext(n)[1].lower() in _IMG_EXT:
                try:
                    images.append((n, z.read(n)))
                except Exception:
                    pass

        cover = None
        if cover_id and cover_id in manifest:
            cf = _resolve(manifest[cover_id][0])
            if cf:
                cover = cf
        return docs, images, cover, meta_info


def _chapters_from_docs(docs, namemap=None):
    """把一批 xhtml 文档转成章节：[标题, markdown]。

    namemap 是「包内路径 → 落盘名」。图片要在这一层就按本文档所在目录解好 ——
    一旦几篇文档合并成一份 md，同名图来自哪个目录就分不出来了。
    """
    out = []
    for full, txt in docs:
        md = _rewrite_imgs(_md_from_xhtml(txt), namemap,
                           posixpath.dirname(full or ""))
        if not md.strip():
            continue
        for t, b in split_markdown(md):
            if not b.strip():
                continue
            out.append((t or "第 %d 章" % (len(out) + 1), b))
    return out


# ─────────────────────────── PDF 拆解 ───────────────────────────

def _pdf_text(data):
    """pypdf 抽文字。抽不出（扫描件）明确报错，不把空书塞给用户。"""
    try:
        from pypdf import PdfReader
    except Exception as e:
        raise ValueError("读 PDF 要用到 pypdf，先跑一次安装（或 pip install pypdf）") from e
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                raise ValueError("这本 PDF 有密码，导不进来")
        pages = []
        for pg in reader.pages:
            try:
                pages.append(pg.extract_text() or "")
            except Exception:
                pages.append("")
    except ValueError:
        raise
    except Exception as e:
        raise ValueError("读 PDF 出错：%s" % (str(e)[:120])) from e
    text = "\n\n".join(p for p in pages if p.strip())
    if len(text.strip()) < 50:
        raise ValueError("这本 PDF 里抽不出文字，多半是扫描件 —— 先用 OCR 转一遍再导")
    return text


# ─────────────────────────── 落盘 ───────────────────────────

_IMG_REF = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def _img_flat(raw, namemap=None, doc_dir=""):
    """章内图片引用 → 落盘名。

    有 namemap（EPUB）时，先按「本文档所在目录 + 相对路径」解析成包内路径再查表；
    解析不出来就退回按文件名找，最后才退回 `_safe_name`（与落盘那一步同一把尺子）。
    查询串 / 锚点（`pic.png?v=2`、`pic.png#f`）不参与文件名。
    """
    if namemap:
        key = urllib.parse.unquote(raw.split("#", 1)[0].split("?", 1)[0])
        cand = posixpath.normpath(posixpath.join(doc_dir or "", key)).lstrip("/")
        flat = namemap.get(cand) or namemap.get(key)
        if not flat:
            base = posixpath.basename(key)
            flat = next((v for k, v in namemap.items()
                         if posixpath.basename(k) == base), None)
        if flat:
            return flat
    return _safe_name(urllib.parse.unquote(raw))


def _rewrite_imgs(md, namemap=None, doc_dir=""):
    """把章内图片引用归到 images/<落盘名>；网络图与 data: 原样保留。

    带 namemap 时不能只看文件名：两篇文档各自引一张 `pic.png`（分别落在包内不同
    目录）时，只看 basename 会双双指向先登记的那张 —— 用户看到的是「这一章的配图
    其实是上一章的」，而且不报任何错。按文档目录解析出包内路径才分得清。
    """
    def rep(m):
        alt, src = m.group(1), m.group(2)
        if src.startswith(("http://", "https://", "data:")):
            return m.group(0)
        if src.startswith("images/"):
            return m.group(0)
        return "![%s](images/%s)" % (alt, _img_flat(src, namemap, doc_dir))
    return _IMG_REF.sub(rep, md or "")


# 落单的半个代理码点（U+D800–U+DFFF 单独出现、不成对）：浏览器的 JS 字符串被拦腰截断时
# 会留下它，而 POST 来的 JSON 里 `"\ud800"` 这种转义照样能被 json.load 读成这个码点 ——
# 于是它混进标题 / 作者 / 正文，写 utf-8 那一下抛 UnicodeEncodeError：/api/import 回 500 加
# 一句 codec 原文，/api/clip 回「剪不动：'utf-8' codec …」，而且书目录这时候已经建出来了，
# 书架上留一本只有半截章节的幽灵书。收口不在这里另写一把尺子：book_layout 那份
# drop_lone_surrogates 就是「外部文本要落进 utf-8」的共用尺（读 meta 也走它），直接复用。
_clean = book_layout.drop_lone_surrogates


def _tmp_name(path):
    """这条写口自己的临时件名：pid + 一小串随机，不再用固定的 `path + ".tmp"`。

    收进来的每一本书都有一份 `meta.json`，而这份账有两个写侧：`import_book` 先写
    一份，紧接着调用方（剪藏 / 订阅收成书 / 视频重建）读回来补自己那几个字段再写
    一遍。今天这两个写侧在同一个请求线程里一前一后，固定名还看不出问题；但名字
    一次一个之后，两条写哪怕真撞上（将来挪进线程 / 子进程）也不会互相搬走临时件
    ——「先收尾的那个把件挪走、后来者 `os.replace` 当场 FileNotFoundError」那一种，
    在这一族别的口上都实测过。与 `ui_server._write_json` / `book_notes._tmp_name`
    / `flomo_notes._tmp_for` / `writer._write_text_atomic` 同一把尺子：名字格式
    只有这一处真相。

    真正的现成毛病是**失败路径**：这条写口原先没有随机名，也没有一句清理 ——
    `json.dump` 写到一半断掉（盘满、编码不进去）就在书目录里留一个 `meta.json.tmp`，
    它不进任何读口，界面上也看不见。
    """
    return "%s.%d.%s.tmp" % (path, os.getpid(), uuid.uuid4().hex[:8])


def _write_json(path, obj):
    tmp = _tmp_name(path)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        # 写这一步没走完就退（编码不进去、盘满）或撞上中止信号（SystemExit /
        # KeyboardInterrupt 不继承 Exception）时，把临时件收干净再抛：收不动也
        # 不算错，但别在书目录里留一个只有磁盘看得见的垃圾。
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _make_book_dir(dest_root, title, prefix="imp"):
    """在书库里开一个安全命名的目录：<prefix>_<slug>_<hex>。

    prefix 是可传的：导入自己的文件用 imp_，剪藏一篇文章用 clip_。书架据此就能
    一眼分清「这本书是从哪来的」，而行号、章节、图片那些结构仍然完全一样。
    """
    os.makedirs(dest_root, exist_ok=True)
    slug = re.sub(r"[^0-9A-Za-z_\-]", "", title or "")[:18]
    bid = "%s_%s_%s" % (prefix or "imp", slug or "book", uuid.uuid4().hex[:8])
    d = os.path.join(dest_root, bid)
    os.makedirs(d, exist_ok=True)
    return d, bid


def _discard_import(book_dir, cover_file=""):
    """一次没收成的导入：把刚开出来的那个半截书目录收干净。

    为什么非收不可：`import_book` 是「先把章节 / 图片铺进目录、最后才写 meta 与目录账」，
    中途断在盘满 / 编码不过 / 中止信号（Ctrl-C、关窗口，`KeyboardInterrupt` /
    `SystemExit` 不继承 `Exception`）时，书目录里已经有几章正文却没有 `meta.json`；
    而 `list_books` 按目录列书、缺 meta 只是「允许没 meta」那一档 —— 书架上于是多出
    一本点开没内容、也没有正式痕迹的幽灵书。整本书的产物都在这个刚建的目录里，收它
    一个就够了，不必叫醒上面那把清理刀（那里按书号走，这本还没进册账）。

    只在**新建**的那个目录上动手（`_make_book_dir` 的名字带 `uuid4().hex[:8]`，不会
    撞上别人的书）；符号链接不碰（`shutil.rmtree` 对链接会抛，且链接另一头不该由这里
    删）。收残件自己再失败也绝不能盖住真正的错 —— 调用方固定是
    `except BaseException: _discard_import(...); raise`。
    """
    try:
        if cover_file:
            try:
                os.remove(cover_file)
            except OSError:
                pass
        if os.path.isdir(book_dir) and not os.path.islink(book_dir):
            shutil.rmtree(book_dir, ignore_errors=True)
    except Exception:
        pass


def import_book(dest_root, filename, data, title="", author="", cover_dir=None,
                book_id_prefix="imp", source="local"):
    """把一份文件收进 dest_root，返回「像取回来的书」的元信息。

    产出的目录结构跟引擎落盘完全一致（chapters/*.md + images/ + meta.json +
    _catalog.json + _progress.json），所以阅读器、导出 EPUB/PDF、在文件管理器里
    定位这些现成能力一行都不用改；只有 meta.source（"local" 或 "clip"）用来和
    微信读书来的书区分，前端据此分开显示。

    失败路径只留一个结果：目录开了之后任何一步没走完（盘满 / 编码不进去 / Ctrl-C），
    刚开的那个半截书目录连同一张顺手写下的封面一起收掉再原样抛出（见 `_discard_import`），
    书架上不会多出一本点开没内容的幽灵书。目录开之前的失败（空文件 / 太大 / 认不出格式 /
    坏 EPUB）本来就什么都没落下。
    """
    if not data:
        raise ValueError("这份文件是空的")
    if len(data) > MAX_BYTES:
        raise ValueError("文件太大了（超过 %d MB），先拆分再导"
                         % (MAX_BYTES // 1024 // 1024))
    fmt = guess_format(filename, data)
    if not fmt:
        raise ValueError("认不出格式，现在能收的是 Markdown / TXT / EPUB / PDF")

    name = os.path.basename(filename or "").strip() or "未命名"
    opf_meta = {}
    images, cover_orig = [], None
    namemap = {}
    if fmt == "epub":
        docs, images, cover_orig, opf_meta = read_epub(data)
        # 落盘名只算一次：正文引用与真正写文件必须查同一张表，否则同名图对不上。
        namemap = _unique_names([n for n, _ in images])
        chapters = _chapters_from_docs(docs, namemap)
    elif fmt == "pdf":
        text = _normalize_lines(_pdf_text(data))
        chapters = split_markdown(text) if re.search(r"(?m)^#\s", text) else split_plain(text)
    else:
        text = _decode_text(data).replace("\r\n", "\n").replace("\r", "\n")
        chapters = split_markdown(text) if fmt == "md" else split_plain(text)

    # 标题/作者：调用方给了就用调用方的，没给就先认 EPUB 里的 dc:title / dc:creator，
    # 最后才退回文件名。导入时前端不传这两个字段，所以 OPF 元信息才是主要来源。
    # 拼出来之后立刻过共用的清洗尺：这几项都是要落进 meta.json（ensure_ascii=False）的。
    title = _clean((title or "").strip() or opf_meta.get("title")
                   or os.path.splitext(name)[0].strip() or "未命名")
    author = _clean((author or "").strip() or opf_meta.get("creator") or "")

    # 正文与章标题同理：先摘掉落单的半个代理码点再落盘（`f.write` 抛的是同一个错）。
    # 顺序与旧写法一致 —— 空正文照旧丢，章序号照旧按原始下标算，正常文本一个字不动。
    clean_chapters = []
    for i, (t, b) in enumerate(chapters):
        if not b or not b.strip():
            continue
        body = _clean(_rewrite_imgs(b)).strip()
        if not body:
            continue
        clean_chapters.append((_clean(t or "") or "第 %d 章" % (i + 1), body))
    chapters = clean_chapters
    if not chapters:
        raise ValueError("这份文件里没读出正文，换一份再试")

    d, bid = _make_book_dir(dest_root, title, book_id_prefix)
    cover_file = ""
    # 从这里到 return 之间，任何一步失败（盘满、编码不进去、Ctrl-C / 关窗口）都已经
    # 在书库里开出了目录，所以整段兜住：收掉这个半截书目录再原样抛出。范围只圈在
    # 「刚建的这个目录 + 它可能顺手写下的那张封面」上，不动别的书、也不吞错。
    try:
        ch_dir = os.path.join(d, "chapters")
        os.makedirs(ch_dir, exist_ok=True)
        catalog, total_chars = [], 0
        for i, (ctitle, body) in enumerate(chapters):
            with open(os.path.join(ch_dir, "%04d.md" % i), "w", encoding="utf-8") as f:
                f.write("# %s\n\n%s\n" % (ctitle, body))
            catalog.append(ctitle)
            total_chars += len(body)

        saved = 0
        if images:
            img_dir = os.path.join(d, "images")
            os.makedirs(img_dir, exist_ok=True)
            namemap = namemap or _unique_names([n for n, _ in images])
            for orig, blob in images:
                flat = namemap.get(orig)
                if not flat:
                    continue
                with open(os.path.join(img_dir, flat), "wb") as f:
                    f.write(blob)
                saved += 1
            # 封面：是 JPEG 就另存一份到封面缓存，书架缩略图直接用（PNG 不强转）
            if cover_orig and cover_dir and namemap.get(cover_orig):
                flat = namemap[cover_orig]
                if os.path.splitext(flat)[1].lower() in (".jpg", ".jpeg"):
                    try:
                        os.makedirs(cover_dir, exist_ok=True)
                        cover_file = os.path.join(cover_dir, bid + ".jpg")
                        shutil.copyfile(os.path.join(img_dir, flat), cover_file)
                    except Exception:
                        # 复制到一半失败也不清空 cover_file：盘上可能已经留下半张，
                        # 收尾那把刀按同一个路径再删一次（删不到就是 OSError，已兜住）。
                        pass

        meta = {
            "title": title,
            "author": author,
            "done": True,
            "source": source or "local",
            "format": fmt,
            "chars": total_chars,
            "imported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": int(time.time()),
        }
        _write_json(os.path.join(d, "meta.json"), meta)
        _write_json(os.path.join(d, "_catalog.json"), catalog)
        _write_json(os.path.join(d, "_progress.json"),
                    {"at": len(chapters), "max": len(chapters)})
        return {"id": bid, "dir": d, "title": title, "author": meta["author"],
                "format": fmt, "label": FORMAT_LABEL.get(fmt, fmt.upper()),
                "chapters": len(chapters), "chars": total_chars, "images": saved}
    except BaseException:
        _discard_import(d, cover_file)
        raise


if __name__ == "__main__":
    # 手动冒烟：python3 book_import.py <文件> [输出根目录]
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else ""
    root = sys.argv[2] if len(sys.argv) > 2 else os.path.join(REPO if "REPO" in dir() else ".",
                                                              "output")
    with open(src, "rb") as fh:
        info = import_book(root, os.path.basename(src), fh.read())
    print(json.dumps(info, ensure_ascii=False, indent=2))