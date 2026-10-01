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


def _split_at(text, pattern):
    """按标题正则切章，返回 [(标题, 正文), ...]。标题前若还有散段落，单列一章。"""
    lines = (text or "").split("\n")
    heads = []
    for i, ln in enumerate(lines):
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
    levels = {}
    for ln in text.split("\n"):
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


def _decode_text(data):
    """按常见中文编码依次试，全不行就按 utf-8 强解（宁可个别字坏，不整本丢）。"""
    for enc in ("utf-8", "utf-16", "gb18030", "big5"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


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
            out[-1] = out[-1] + s
        else:
            out.append(s)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


# ─────────────────────────── XML / HTML 杂活 ───────────────────────────

def _tag(el):
    t = el.tag
    return t.rsplit("}", 1)[-1] if isinstance(t, str) else ""


def _find_all(root, name):
    return [e for e in root.iter() if _tag(e) == name]


def _parse_xml(raw):
    """解析不可信的 XML（EPUB 的 OPF）。

    OPF 是外面带进来的东西，一律按不可信处理：先挡掉 DTD / 实体声明（防实体
    膨胀与外部实体读取），再交给标准解析器。解析失败返回 None，让调用方走兜底
    路径（按 manifest / 自然序排章节），不因为一个坏 OPF 就整本导入失败。
    """
    if isinstance(raw, str):
        head = raw[:4096]
    else:
        head = raw[:4096].decode("utf-8", "replace")
    if "<!DOCTYPE" in head or "<!ENTITY" in head:
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

def read_epub(data):
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


def _chapters_from_docs(docs):
    """把一批 xhtml 文档转成章节：[标题, markdown]。"""
    out = []
    for _full, txt in docs:
        md = _md_from_xhtml(txt)
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


def _rewrite_imgs(md):
    """把章内图片引用归到 images/<落盘名>；网络图与 data: 原样保留。"""
    def rep(m):
        alt, src = m.group(1), m.group(2)
        if src.startswith(("http://", "https://", "data:")):
            return m.group(0)
        if src.startswith("images/"):
            return m.group(0)
        name = _safe_name(urllib.parse.unquote(src))
        return "![%s](images/%s)" % (alt, name)
    return _IMG_REF.sub(rep, md or "")


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


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


def import_book(dest_root, filename, data, title="", author="", cover_dir=None,
                book_id_prefix="imp", source="local"):
    """把一份文件收进 dest_root，返回「像取回来的书」的元信息。

    产出的目录结构跟引擎落盘完全一致（chapters/*.md + images/ + meta.json +
    _catalog.json + _progress.json），所以阅读器、导出 EPUB/PDF、在文件管理器里
    定位这些现成能力一行都不用改；只有 meta.source（"local" 或 "clip"）用来和
    微信读书来的书区分，前端据此分开显示。
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
    if fmt == "epub":
        docs, images, cover_orig, opf_meta = read_epub(data)
        chapters = _chapters_from_docs(docs)
    elif fmt == "pdf":
        text = _normalize_lines(_pdf_text(data))
        chapters = split_markdown(text) if re.search(r"(?m)^#\s", text) else split_plain(text)
    else:
        text = _decode_text(data).replace("\r\n", "\n").replace("\r", "\n")
        chapters = split_markdown(text) if fmt == "md" else split_plain(text)

    # 标题/作者：调用方给了就用调用方的，没给就先认 EPUB 里的 dc:title / dc:creator，
    # 最后才退回文件名。导入时前端不传这两个字段，所以 OPF 元信息才是主要来源。
    title = ((title or "").strip() or opf_meta.get("title")
             or os.path.splitext(name)[0].strip() or "未命名")
    author = (author or "").strip() or opf_meta.get("creator") or ""

    chapters = [(t or "第 %d 章" % (i + 1), _rewrite_imgs(b).strip())
                for i, (t, b) in enumerate(chapters) if b and b.strip()]
    if not chapters:
        raise ValueError("这份文件里没读出正文，换一份再试")

    d, bid = _make_book_dir(dest_root, title, book_id_prefix)
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
        namemap = _unique_names([n for n, _ in images])
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
                    shutil.copyfile(os.path.join(img_dir, flat),
                                    os.path.join(cover_dir, bid + ".jpg"))
                except Exception:
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


if __name__ == "__main__":
    # 手动冒烟：python3 book_import.py <文件> [输出根目录]
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else ""
    root = sys.argv[2] if len(sys.argv) > 2 else os.path.join(REPO if "REPO" in dir() else ".",
                                                              "output")
    with open(src, "rb") as fh:
        info = import_book(root, os.path.basename(src), fh.read())
    print(json.dumps(info, ensure_ascii=False, indent=2))