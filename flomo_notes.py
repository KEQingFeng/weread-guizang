# -*- coding: utf-8 -*-
"""flomo 笔记导入：把官方导出的那份 HTML 变成这个工具里的普通条目。

为什么走「导出文件」而不是 API：flomo 读笔记只有两条路 —— 网页版设置里的导出
（全局或按标签导一份 HTML zip），或者官方 MCP（要 MAX 会员 + 个人 Token）。
导出的那份谁都能拿到，程序也不用碰账号凭证；界面里再留一个「重新导一次」的按钮，
用户想更新就点一下，比挂一个要会员的长连接实在。

认的是 flomo 官方导出的形状（实测本机一份 555 条的导出）：

    <div class="memo">
      <div class="time">2026-08-31 22:26:59</div>
      <div class="content"><p>正文 #阅读/读书</p><p>第二段</p></div>
      <div class="files"><img src="file/2026-08-29/75134/x.jpg" alt="memo image" /></div>
    </div>

三件事必须保住：
  · **时间原样留着**（用户点名要「完整呈现时间、标签等字段」）—— 日期与时分秒分开存，
    列表按天分组要的是日期，卡片上要的是那一秒；
  · **标签既要能筛又不能改正文** —— `#一级/二级` 就地留在正文里（flomo 本来就是这么写的），
    另外抽一份放进 `tags` 供筛选与统计；
  · **重复导同一份不许翻倍** —— 每条的 id 是「日期时间 + 正文」的哈希，再导一次
    只会补上新的，已有的原样不动（哈希认不出「改过的」，改了内容在 flomo 里就是新的一条
    旧的一条，这里跟着它一起当两条，不去悄悄覆盖用户可能已经批注过的东西）。

本模块只碰磁盘、不碰网络、不起线程：喂它一段 HTML 它就给一批条目，所以能在没起服务的
情况下整套测一遍 —— 解析、抽标签、附件落盘、去重、导成书，全是纯逻辑。
"""

import hashlib
import io
import json
import os
import re
import time
import zipfile
from html.parser import HTMLParser

VERSION = 1

# 一条笔记里最多存多少字。flomo 单条上限本就不大（长文会被用户自己拆开），
# 这个闸只挡「导出的 HTML 坏掉导致把整页当一条」这种事故。
MAX_MD = 20000

# 单个附件落盘的上限：导出包里的图通常是手机拍的 1~3 MB，超过 12 MB 的多半是
# 原图或视频抽帧，收进来只会让本地目录虚胖，不如跳过并出声。
MAX_ATT_BYTES = 12 * 1024 * 1024

IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")

# 用户记忆画像落在存储目录下的这个文件夹里（cache/flomo/portrait/）：
# JSON 给机器比对，Markdown 给人读、也给 Agent 当上下文。
PORTRAIT_DIR = "portrait"
PORTABLE_JSON = "portrait.json"
PORTABLE_MD = "portrait.md"

# 标签：# 前面必须是行首或空白（否则「C#」这种词会被误认），# 后面不能紧跟空白
# （那是 markdown 标题 `# 标题`，flomo 编辑器里也不当标签）。到下一个空白为止，
# 中间允许 / 表示层级；行尾再用 strip_punct 把「。）」这类跟上去的标点摘掉。
TAG_RE = re.compile(r"(?<!\S)#([^\s#]+)")
PUNCT_TAIL = "。，、；：！？）】」』,.;:!?)>\u3000"
TIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})?")


# ─────────────────────────── 解析 ───────────────────────────

class _MemoParser(HTMLParser):
    """把导出的 HTML 拆成一条条笔记。

    为什么不正则切 `<div class="memo">…</div>`：正文里可以套 p / strong / mark / ul /
    ol / img / br，div 也会嵌套（外层还有个 class="memos" 的壳）。正则匹不到「配对的
    那个 </div>」，一旦正文里出现裸 < 就整块错位。用标准库的 HTMLParser 跟着深度走，
    多深的嵌套都只是一个计数器。
    """

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.memos = []
        self.cur = None            # 正在收的那一条
        self.where = ""            # time / content / files / ""
        self.depth = 0             # div 深度
        self.memo_depth = None     # 这条 memo 的 div 起始深度
        self.buf = []              # 正文的字符缓冲
        self.list_style = []       # ul / ol 栈，决定 li 前缀
        self.memos_text = ""
        self.last_item = False     # 上一块是不是列表项（决定粘不粘空行）

    # -- 小工具 ------------------------------------------------------------
    def _cls(self, attrs):
        d = dict(attrs)
        return str(d.get("class") or "")

    def _flush_para(self):
        """一段结束：把攒着的字并进去。

        段与段之间空一行（Markdown 才认段落），但**连着两条列表项之间只换行** ——
        `- a` 和 `- b` 中间插个空行就变成「两个只有一项的列表」，阅读器里行距会宽一倍，
        那条清单看着就不像一条清单了。所以粘不粘空行看这两块各自是不是列表项。
        """
        txt = "".join(self.buf)
        self.buf = []
        txt = re.sub(r"[ \t]*\n[ \t]*", "\n", txt).strip()
        if not txt:
            return
        item = bool(re.match(r"^(- |\d+\. )", txt))
        glue = "\n" if (item and self.last_item) else "\n\n"
        if self.memos_text:
            self.memos_text += glue
        self.memos_text += txt
        self.last_item = item

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        cls = str(d.get("class") or "")
        if tag == "div":
            self.depth += 1
            if "memo" in cls.split() and self.cur is None:
                self.cur = {"time": "", "text": "", "imgs": []}
                self.memo_depth = self.depth
                self.where = ""
                self.memos_text = ""
                self.last_item = False
                self.buf = []
                return
            if self.cur is not None:
                if "time" in cls.split():
                    self.where = "time"
                elif "content" in cls.split():
                    self.where = "content"
                    self.memos_text = ""
                    self.last_item = False
                    self.buf = []
                elif "files" in cls.split():
                    self.where = "files"
            return
        if self.cur is None:
            return
        if tag == "img":
            src = str(d.get("src") or "").strip()
            if src and self.where == "files":
                self.cur["imgs"].append(src)
            elif src and self.where == "content":
                # 正文里嵌的图也收（flomo 的图都在 files 那层，这条只是兜底）
                self.cur["imgs"].append(src)
                self.buf.append(" ![图](%s)" % src)
            return
        if self.where != "content":
            return
        if tag in ("p", "div"):
            self._flush_para()
        elif tag == "br":
            self.buf.append("\n")
        elif tag == "li":
            self.buf.append("1. " if self.list_style and self.list_style[-1] == "ol" else "- ")
        elif tag == "ul":
            self.list_style.append("ul")
        elif tag == "ol":
            self.list_style.append("ol")
        elif tag in ("strong", "b"):
            self.buf.append("**")
        elif tag in ("em", "i"):
            self.buf.append("*")
        elif tag in ("code",):
            self.buf.append("`")
        elif tag == "mark":
            # flomo 的高亮在 Markdown 里没有对应记号。折成加粗：留不住「荧光笔」这层
            # 语义，但强调还在，也不会像 ==文字== 那样在阅读器里露出两个怪符号。
            self.buf.append("**")
        elif tag in ("del", "s", "strike"):
            self.buf.append("~~")
        elif tag == "blockquote":
            self.buf.append("\n> ")

    def handle_endtag(self, tag):
        if tag == "div":
            if self.cur is not None and self.depth == self.memo_depth:
                self._flush_para()
                self.cur["text"] = self.memos_text
                self.memos.append(self.cur)
                self.cur = None
                self.where = ""
                self.list_style = []
            self.depth = max(0, self.depth - 1)
            return
        if self.cur is None or self.where != "content":
            return
        if tag in ("p", "li"):
            self._flush_para()
        elif tag in ("ul", "ol"):
            if self.list_style:
                self.list_style.pop()
        elif tag in ("strong", "b", "em", "i", "code", "mark", "del", "s", "strike"):
            self.buf.append("**" if tag in ("strong", "b", "mark")
                            else ("*" if tag in ("em", "i") else
                                  ("`" if tag == "code" else "~~")))

    def handle_data(self, data):
        if self.cur is None:
            return
        if self.where == "time":
            self.cur["time"] += data
        elif self.where == "content":
            self.buf.append(data)


def parse_html(text):
    """导出 HTML → [{date, clock, ts, md, tags, imgs, words}, …]，按文件里的先后原样给。

    界面要的是「按天倒序」，但排序交给调用方 —— 解析这一步只管忠实还原，
    方便出问题时对着原文一眼看出是哪一条被啃了。
    """
    p = _MemoParser()
    p.feed(text or "")
    p.close()
    out = []
    for m in p.memos:
        md = str(m.get("text") or "").strip()
        if len(md) > MAX_MD:
            md = md[:MAX_MD]
        out.append(memo_of(str(m.get("time") or ""), md, list(m.get("imgs") or [])))
    return out


def memo_of(time_str, md, imgs=()):
    """一条笔记的各字段凑齐。单独拎出来是为了让「手工攒一条」和「从 HTML 解一条」
    共用同一套 id / 标签 / 字数口径（否则重复导入会出现「同一条两个 id」）。"""
    raw = str(time_str or "").strip()
    mm = TIME_RE.match(raw)
    date = mm.group(1) if mm else raw[:10]
    clock = (mm.group(2) or "") if mm else ""
    md = str(md or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    tags = extract_tags(md)
    ts = 0
    if date:
        try:
            parts = [int(x) for x in date.split("-")]
            sec = [int(x) for x in (clock or "00:00:00").split(":")]
            ts = int(time.mktime(tuple(parts) + tuple(sec) + (0, 0, -1)))
        except Exception:
            ts = 0
    imgs = [str(s) for s in (imgs or []) if str(s).strip()]
    h = hashlib.sha1(("%s|%s" % (raw or date + " " + clock, md)).encode("utf-8"))
    return {"id": "fm_" + h.hexdigest()[:12], "date": date, "clock": clock, "ts": ts,
            "md": md, "tags": tags, "imgs": imgs, "words": words_of(md),
            "atts": []}


def words_of(md):
    """正文字数：去掉空白和 Markdown 记号，跟剪藏那边同一把尺。"""
    return len(re.sub(r"\s", "", re.sub(r"[#>*`_~\-\|\[\]()!]", "", str(md or ""))))


def extract_tags(md):
    """抽出这条里的标签，按「一级/二级」原样保留层级，去重后按出现顺序给。"""
    out = []
    for m in TAG_RE.finditer(str(md or "")):
        t = (m.group(1) or "").strip().strip(PUNCT_TAIL).strip("/").replace(" ", "")
        if not t or t.isdigit():
            continue                      # 「#3」这种纯数字是序号，不是标签
        if t not in out:
            out.append(t)
    return out


# ─────────────────────────── 读导出包 ───────────────────────────

def _fix_zip_name(info):
    """zip 里的文件名：没打 UTF-8 标记的条目，Python 会按 cp437 解，中文名就成了乱码。
    导出的 HTML 那一页带的是 UTF-8 标记，附件目录是 ASCII —— 两种都要能翻开，
    所以按「能不能凑出 .html」再认一次，不靠名字里的中文。"""
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        return name.encode("cp437").decode("utf-8")
    except Exception:
        return name


def read_export(blob, filename=""):
    """导入包 → (导出的 HTML 文本, {包内相对路径: 字节})。

    收三种：官方那个 zip（`.zip`，里面一页 HTML + file/ 附件）、单一份 HTML、
    以及把 HTML 直接改名成 .txt 的那种（有人导完顺手改了后缀）。
    认不出来的回一句人话，别让前端只显示「失败」。
    """
    if not blob:
        raise ValueError("没有收到文件内容")
    low = str(filename or "").lower()
    if blob[:2] == b"PK" or low.endswith(".zip"):
        try:
            z = zipfile.ZipFile(io.BytesIO(blob))
        except Exception as e:
            raise ValueError("这个 zip 打不开：%s" % str(e)[:80])
        html_name, html_text, files = None, None, {}
        for info in z.infolist():
            if info.is_dir():
                continue
            name = _fix_zip_name(info)
            base = os.path.basename(name).lower()
            if name.lower().endswith(".html") or base == "index.html":
                if html_text is None or name.count("/") < html_name.count("/"):
                    # 多层同名时取浅的那一份（根目录那页才是全部笔记）
                    html_name, html_text = name, z.read(info.filename).decode("utf-8", "replace")
                continue
            if name.lower().endswith(IMG_EXT) and info.file_size <= MAX_ATT_BYTES:
                files[name] = z.read(info.filename)
        if html_text is None:
            raise ValueError("这个包里没找到笔记那一页 HTML（确认是 flomo 网页版「设置 → 导出」的那份）")
        return html_text, files
    text = blob.decode("utf-8", "replace") if isinstance(blob, (bytes, bytearray)) else str(blob)
    if 'class="memo"' in text or "<div class=" in text:
        return text, {}
    raise ValueError("认不出这份文件：要 flomo 导出的那个 zip，或它里面的那页 HTML")


# ─────────────────────────── 存盘 ───────────────────────────

def path_in(flomo_dir):
    return os.path.join(flomo_dir, "notes.json")


def load(flomo_dir):
    """读那份账；坏了、没有都给空的（导入这件事必须能从零开始，不许被旧脏数据挡死）。"""
    try:
        with open(path_in(flomo_dir), encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    memos = d.get("memos")
    if not isinstance(memos, list):
        memos = []
    d["memos"] = [m for m in memos if isinstance(m, dict) and m.get("id")]
    d.setdefault("at", 0)
    d.setdefault("src", "")
    d.setdefault("version", VERSION)
    return d


def save(flomo_dir, data):
    os.makedirs(flomo_dir, exist_ok=True)
    tmp = "%s.%d.tmp" % (path_in(flomo_dir), os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path_in(flomo_dir))


def att_dir(flomo_dir):
    d = os.path.join(flomo_dir, "att")
    os.makedirs(d, exist_ok=True)
    return d


def att_name(src, blob):
    """附件的本机名：内容哈希 + 原后缀。

    用内容而不是路径命名，是因为同一张图会被多条笔记引用（转发的图、重复导出的包），
    哈希名让它在本地只有一份；路径里那个 `file/2026-08-29/75134/` 是 flomo 的临时目录，
    下一次导出就可能变，拿它当键会把同一张图存成两三份。
    """
    ext = os.path.splitext(str(src))[1].lower()
    if ext not in IMG_EXT:
        ext = ".jpg"
    return hashlib.sha1(blob).hexdigest()[:20] + ext


def att_keys(src):
    """一个包内路径 → 它在 HTML 里可能出现的所有写法。

    flomo 导出包里是 `flomo@xxx-20260901/file/2026-08-29/75134/x.jpg`（顶层带包名），
    HTML 的 img src 却写 `file/2026-08-29/75134/x.jpg`（相对那一页）。两者差一层目录，
    拿整串当键去取图就一定取不到 —— 于是同一张图会「存下来了但笔记里看不见」。
    这里把三种写法都登记上：原样、去掉顶层那一段、只剩文件名（外加 ./ 前缀的变体）。
    """
    s = str(src or "").replace("\\", "/").lstrip("./")
    keys = {s, "./" + s, os.path.basename(s)}
    parts = [p for p in s.split("/") if p]
    if len(parts) > 1:
        tail = "/".join(parts[1:])
        keys.add(tail)
        keys.add("./" + tail)
    return [k for k in keys if k]


def store_atts(flomo_dir, files):
    """把导出包里的图片落到本地，返回 {包内相对路径: 本机名}。"""
    out = {}
    if not files:
        return out
    d = att_dir(flomo_dir)
    for src, blob in files.items():
        if not blob:
            continue
        nm = att_name(src, blob)
        try:
            if not os.path.exists(os.path.join(d, nm)):
                with open(os.path.join(d, nm), "wb") as f:
                    f.write(blob)
        except OSError:
            continue
        # 文件名撞了（不同内容的同名图）时先到先得，后来那条引用宁可没图也不指错图。
        for k in att_keys(src):
            if not out.get(k):
                out[k] = nm
    return out


def import_notes(flomo_dir, blob, filename="", log=None):
    """导入一份 flomo 导出 → {added, existed, total, skipped, src, at}。

    只增不改：已经在这儿的条目一个字都不动（用户可能给它贴过标签、收进过书架）。
    """
    say = log or (lambda s: None)
    text, files = read_export(blob, filename)
    parsed = parse_html(text)
    if not parsed:
        raise ValueError("这份导出里一条笔记都没读出来")
    data = load(flomo_dir)
    by_id = {m["id"]: m for m in data["memos"]}
    names = store_atts(flomo_dir, files)
    added, patched = 0, 0
    for m in parsed:
        old = by_id.get(m["id"])
        if old is not None:
            # 已经在账上的：正文一个字不动（用户可能给它归过类、收进过书架），
            # 但这一趟包里带来的图要接上 —— 按标签导的那份常常不含附件，
            # 下次导全量时同一条笔记的哈希认得出是它，图就该补进去。
            for src in m["imgs"]:
                nm = names.get(src)
                if nm and nm not in (old.get("atts") or []):
                    old.setdefault("atts", []).append(nm)
                    patched += 1
            continue
        m["atts"] = [names[s] for s in m["imgs"] if names.get(s)]
        data["memos"].append(m)
        by_id[m["id"]] = m
        added += 1
    data["at"] = int(time.time())
    data["src"] = str(filename or "")[-80:]
    data["version"] = VERSION
    save(flomo_dir, data)
    if patched:
        say("补上 %d 张图" % patched)
    # names 里一个文件占好几个键（包内全路径 / 去掉顶层 / 只剩文件名），
    # 报数只按「真落了几张图」算，界面上「导入 555 条 · 1 张图」才对得上。
    return {"added": added, "existed": len(parsed) - added, "total": len(data["memos"]),
            "atts": len({names[k] for k in names}), "patched": patched,
            "src": data["src"], "at": data["at"]}


def tag_match(mtags, tg):
    """标签筛的那把尺：只往下算，不往上算。

    选「读书」要看得见「读书/神经科学」（flomo 的标签是一棵树，点枝干就该看见枝叶）；
    反过来不成立 —— 选「读书/神经科学」却捞出只打了「读书」的那几条，药丸上的数字就成了
    谎话（看着像「这一支有两条」，点进去一条根本不属于它）。上一版两个方向都算，界面上
    并排出现「读书 2」「读书/神经科学 2」，一眼就像坏了。筛选与药丸计数共用这一把尺。"""
    tg = str(tg or "").strip()
    return any(t == tg or t.startswith(tg + "/") for t in mtags)


def tag_paths(tags):
    """一条笔记打了 #读书/神经科学，那它也属于 #读书：把每一级的路径都摊出来。"""
    out = set()
    for t in tags or []:
        parts = str(t).split("/")
        for i in range(1, len(parts) + 1):
            out.add("/".join(parts[:i]))
    return out


def tag_counts(data):
    """标签 → 条数，界面那一排药丸就照这个顺序摆。

    两级讲究，都是「数字必须对得上结果」这一条引出来的：
      · 上级路径也占一颗（打了 #读书/神经科学 的人想看「读书底下都记了什么」，
        flomo 自己的标签树就是这么摆的；不带这颗，纯层级的标签就永远筛不到）；
      · 每一颗的条数用 tag_match 同一把尺 —— 之前只数「原样打上去的那级」，于是
        药丸写着 1、点下去出 2，看着就像筛选坏了。
    一条笔记只走它自己那几个标签的祖先路径，几百个标签 × 几千条笔记也不会变成逐对比较。
    """
    c = {}
    for m in data.get("memos") or []:
        for p in tag_paths(m.get("tags") or []):
            c[p] = c.get(p, 0) + 1
    return [{"tag": p, "n": c[p]} for p, _ in
            sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))]


def recent_tags(data, n=12):
    """按天看：出现得最晚的那几个标签，给「最近都在记什么」用。"""
    seen = []
    for m in sorted(data.get("memos") or [], key=lambda x: -int(x.get("ts") or 0)):
        for t in m.get("tags") or []:
            if t not in seen:
                seen.append(t)
            if len(seen) >= n:
                return seen
    return seen


def pick(data, tag="", q="", limit=0, offset=0, order="desc"):
    """筛出一条条给界面：标签（前缀匹配，选「SOP」也要能看见「SOP/flomo」）、
    关键字（搜正文与标签），按时间倒序 —— 新笔记在上面，这是笔记应用的基本礼貌。"""
    rows = data.get("memos") or []
    tg = str(tag or "").strip()
    if tg:
        rows = [m for m in rows if tag_match(m.get("tags") or [], tg)]
    kw = str(q or "").strip().lower()
    if kw:
        rows = [m for m in rows
                if kw in str(m.get("md") or "").lower() or kw in ",".join(m.get("tags") or [])]
    rows = list(rows)
    rows.sort(key=lambda m: (-int(m.get("ts") or 0), str(m.get("clock") or "")),
              reverse=(str(order) == "asc"))
    total = len(rows)
    off = max(0, int(offset or 0))
    lim = int(limit or 0)
    part = rows[off:] if not lim else rows[off:off + lim]
    return {"total": total, "offset": off, "count": len(part),
            "memos": [public(m) for m in part]}


def public(m):
    """交给界面/MCP 的那一份：只带用得上的字段，内部键（比如原始图片路径）不外漏。

    多带一个 `plain`：正文里那几个「已经在 tags 里单列出来的标签」摘掉之后的那份。
    界面上这件事由前端自己摘（它拿不到 Python），MCP 与任何别的外部读者拿的是这份账，
    没有各自的摘法 —— 所以真源必须在这儿出一份，否则 Agent 读到的正文会比用户在
    屏幕上看到的多一排 #标签（同一个标签在同一条里出现两次，正是上一轮修掉的毛病）。
    """
    return {"id": m.get("id"), "date": m.get("date") or "", "clock": m.get("clock") or "",
            "ts": int(m.get("ts") or 0), "md": m.get("md") or "",
            "plain": strip_inline_tags(m.get("md"), m.get("tags")),
            "tags": list(m.get("tags") or []), "words": int(m.get("words") or 0),
            "atts": list(m.get("atts") or []), "book": m.get("book") or ""}


def one(data, memo_id):
    for m in data.get("memos") or []:
        if m.get("id") == str(memo_id or ""):
            return m
    return None


def set_book(flomo_dir, memo_id, book_id):
    """记住这条已经收成书了 —— 界面上的「收进书架」要变成「去书库」，
    不然同一个用户点两下就得到两本一样的书。"""
    data = load(flomo_dir)
    m = one(data, memo_id)
    if not m:
        return False
    m["book"] = str(book_id or "")
    save(flomo_dir, data)
    return True


def forget(flomo_dir, memo_id):
    """从本机的账里去掉一条（不动文件、不动 flomo）。"""
    data = load(flomo_dir)
    before = len(data.get("memos") or [])
    data["memos"] = [m for m in (data.get("memos") or []) if m.get("id") != str(memo_id)]
    if len(data["memos"]) == before:
        return False
    save(flomo_dir, data)
    return True


def clear(flomo_dir):
    """清掉导入的笔记，连带把它们带出来的附件与画像一起抹掉。

    界面上「清空」那颗写的是「笔记、附件里的图片和记忆画像」，那就得三样都没 ——
    账都清了还把用户的私人图片摊在磁盘上，属于说到没做到。
    只删这两个文件夹里的文件，目录与 notes.json 本身留着（写成空账）：数据目录是
    「清除本地数据」的四条红线之一，别人家的东西也在同一层里放着。
    """
    data = load(flomo_dir)
    n = len(data.get("memos") or [])
    data["memos"] = []
    data["at"] = int(time.time())
    save(flomo_dir, data)
    for d in (att_dir(flomo_dir), portrait_dir(flomo_dir)):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for nm in names:
            p = os.path.join(d, nm)
            if os.path.isfile(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
    return n


# ─────────────────────────── 收成书 ───────────────────────────

def strip_inline_tags(md, tags):
    """把正文里那几个「已经另外列出来的标签」摘掉。

    flomo 的导出把标签就写在正文末尾（#读书 就是句末那几个字），而卡下面那排药丸、
    书开头那行时间戳都会再列一遍 —— 同一条笔记里同一个标签出现两次，看着就像没清干净。
    只摘标签账上真有的那几个：句末那个「#3」是序号，不在账上，一个字不动。
    账里存的那份 md 照旧不改（重复导入靠原始 md 算 id，动了会把同一条认成两条）。
    """
    s = str(md or "")
    for t in sorted({str(x) for x in (tags or []) if x}, key=len, reverse=True):
        s = re.sub(r"(^|\s)#" + re.escape(t) + r"(?=\s|$|[，。、；：！？,.;:!?）)])",
                   r"\1", s)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]{2,}", " ", s)).strip()


def memo_title(m):
    """一条笔记当书的时候叫什么。

    flomo 的笔记没有标题，界面里就借「第一行的前几个字」——这跟用户自己翻笔记时的
    找法一致（靠开头认），而不是靠一个日期串。纯图没字的给一句兜底。
    末尾那个标签先摘掉再取：书名是「带图的一条，图在 files 那层」，不是「…#读书」，
    标签在书的开头那行和时间线那一排药丸里都会露，不必挤进书名。
    """
    first = ""
    for line in strip_inline_tags(m.get("md"), m.get("tags")).split("\n"):
        s = line.strip().lstrip("#>- ").strip()
        if s:
            first = s
            break
    if not first:
        return "%s 的一张图" % (m.get("date") or "flomo")
    first = re.sub(r"\s+", " ", first)
    return first[:24] + ("…" if len(first) > 24 else "")


def memo_markdown(m):
    """一条笔记 → 一本书的那一篇 Markdown。

    开头把时间与标签摆明（进了阅读器也得看得见「这是哪天记的」），
    图片引用换成落盘后的 images/<本机名>，阅读器与导出 EPUB 都认这个相对路径。
    """
    lines = ["# %s" % memo_title(m), ""]
    meta = []
    stamp = " ".join(x for x in (m.get("date"), m.get("clock")) if x)
    if stamp:
        meta.append(stamp)
    if m.get("tags"):
        meta.append(" ".join("#" + t for t in m["tags"]))
    if meta:
        lines.append("> " + "　·　".join(meta))
        lines.append("")
    md = strip_inline_tags(m.get("md"), m.get("tags"))
    if md:
        lines.append(md)
    for nm in (m.get("atts") or []):
        lines.append("")
        lines.append("![图](images/%s)" % nm)
    return "\n".join(lines).strip() + "\n"


def to_shelf(flomo_dir, memo_id, out_root, import_fn=None):
    """一条笔记 → 书库便签那一格里的一本书。

    import_fn 是可注入的（默认书 book_import.import_book）：测试里换成一个假函数就能
    只看「这篇 Markdown 长什么样、meta 补了哪些字段」，不必真去建目录。
    """
    data = load(flomo_dir)
    m = one(data, memo_id)
    if not m:
        raise ValueError("没有这条笔记")
    if m.get("book"):
        d = os.path.join(out_root, m["book"])
        if os.path.isdir(d):
            return {"id": m["book"], "dir": d, "title": memo_title(m),
                    "existed": True}
    if import_fn is None:
        import book_import
        import_fn = book_import.import_book
    body = memo_markdown(m)
    info = import_fn(out_root, "flomo.md", body.encode("utf-8"),
                     title=memo_title(m), author="flomo",
                     book_id_prefix="flomo", source="flomo")
    # 图片：账里存的是本机哈希名，书目录要的是 images/<同名>，阅读器才找得着
    srcdir = att_dir(flomo_dir)
    saved = 0
    for nm in (m.get("atts") or []):
        sp = os.path.join(srcdir, os.path.basename(nm))
        if not os.path.isfile(sp):
            continue
        dst = os.path.join(info["dir"], "images")
        os.makedirs(dst, exist_ok=True)
        try:
            with open(sp, "rb") as f:
                blob = f.read(MAX_ATT_BYTES + 1)
            if len(blob) > MAX_ATT_BYTES:
                continue
            with open(os.path.join(dst, os.path.basename(nm)), "wb") as f:
                f.write(blob)
            saved += 1
        except OSError:
            continue
    mp = os.path.join(info["dir"], "meta.json")
    # 书目录正常由 import_book 建好；这里再保一次，是为了「注入假 import_fn」的测试
    # 路径也能跑通，更重要的是：目录不存在时宁可新建，也不要抛 FileNotFoundError
    # 让用户看到「收进书架失败」却不知道为什么。
    os.makedirs(info["dir"], exist_ok=True)
    meta = {}
    try:
        with open(mp, encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        meta = {}
    meta.update({"source": "flomo", "format": "clip", "memo_id": m["id"],
                 "date": m.get("date") or "", "clock": m.get("clock") or "",
                 "tags": list(m.get("tags") or []), "words": int(m.get("words") or 0),
                 "chars": int(m.get("words") or 0), "images": saved,
                 "imported_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    tmp = mp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.replace(tmp, mp)
    info["meta"] = meta
    info["title"] = info.get("title") or memo_title(m)
    set_book(flomo_dir, m["id"], info.get("id"))
    return info


# ─────────────────────────── 记忆画像 ───────────────────────────

def summary_stats(data):
    """这批笔记的骨架数字，给「用户记忆画像」和界面计数用（不碰原文，只给数）。"""
    memos = data.get("memos") or []
    days = {}
    words = 0
    tag_n = {}
    img_n = 0
    for m in memos:
        d = str(m.get("date") or "")
        if d:
            days[d] = days.get(d, 0) + 1
        words += int(m.get("words") or 0)
        for t in m.get("tags") or []:
            tag_n[t] = tag_n.get(t, 0) + 1
        img_n += len(m.get("atts") or [])
    ds = sorted(days)
    return {"memos": len(memos), "days": len(days), "first": ds[0] if ds else "",
            "last": ds[-1] if ds else "", "words": words, "tags": len(tag_n),
            "images": img_n,
            "top_tags": sorted(tag_n.items(), key=lambda kv: (-kv[1], kv[0]))[:20],
            "busiest": sorted(days.items(), key=lambda kv: (-kv[1], kv[0]))[:5]}


def build_portrait(data):
    """用户记忆画像：从这批笔记里算出「这人是怎么记的」，一条原文都不带。

    为什么要这么窄：Agent 每次执行任务前要先读这份画像（这是用户要的流程），而它是
    唯一会被反复喂给模型的东西 —— 喂原文等于把用户全部私人记录交给每一次对话，
    喂数字与标签则只暴露「结构」。标签是用户自己打的分类，本来就是给人看的。

    字段设计对着「Agent 拿到它能做什么决定」来：
      · 体量与节奏 → 该一次给多少条、要不要分页；
      · 标签层级（一级 → 二级）→ 用户的心智分类，检索时按它排优先级；
      · 长度分布 → 这人是写一句话灵感还是写长段思考，回复该多长；
      · 已收进书架的条数 → 哪些内容他已经在深加工，值得优先提。
    """
    memos = data.get("memos") or []
    st = summary_stats(data)
    per_day = st["days"] or 1
    long_n = mid_n = short_n = 0
    tops = {}
    hours = {}
    for m in memos:
        w = int(m.get("words") or 0)
        if w < 30:
            short_n += 1
        elif w <= 200:
            mid_n += 1
        else:
            long_n += 1
        clock = str(m.get("clock") or "")
        if clock[:2].isdigit():
            hours[int(clock[:2])] = hours.get(int(clock[:2]), 0) + 1
        for t in m.get("tags") or []:
            head = str(t).split("/")[0]
            tops[head] = tops.get(head, 0) + 1
    recent = 0
    if memos:
        ds = sorted({str(m.get("date") or "") for m in memos if m.get("date")})
        tail = ds[-7:]
        recent = sum(1 for m in memos if str(m.get("date") or "") in tail)
    return {
        "memos": st["memos"], "days": st["days"],
        "first": st["first"], "last": st["last"],
        "words": st["words"], "images": st["images"],
        "per_day": round(st["memos"] / per_day, 1),
        "avg_words": round(st["words"] / (st["memos"] or 1), 1),
        "short": short_n, "mid": mid_n, "long": long_n,
        "shelved": sum(1 for m in memos if m.get("book")),
        "last7": recent,
        "tags": st["top_tags"], "groups": sorted(tops.items(), key=lambda kv: (-kv[1], kv[0]))[:10],
        "busiest": st["busiest"],
        # 记笔记的高峰时段（小时 → 条数）：Agent 用它判断「这人什么时候在想事情」，
        # 比只知道「每天记几条」更接近本人的节奏。
        "hours": sorted(hours.items(), key=lambda kv: (-kv[1], kv[0]))[:6],
        "src": data.get("src") or "", "at": int(data.get("at") or 0),
    }


def portrait_markdown(p):
    """画像 → 给人和 Agent 一起读的那页 Markdown。

    写成自然句子而不是 JSON 堆在这：Agent 读的是文本上下文，一句话能说明白的
    就别让它解析十层括号；界面里也能直接当「我的记忆画像」预览渲染。
    """
    lines = ["# 我的记忆画像", ""]
    lines.append("到 %s 为止，本机存着 %d 条 flomo 笔记，记在 %d 个不同的日子里"
                 "（%s ~ %s），共 %s 字、%d 张图。"
                 % (p.get("last") or "现在", p["memos"], p["days"],
                    p.get("first") or "?", p.get("last") or "?",
                    p["words"], p["images"]))
    lines.append("")
    lines.append("平均每天 %.1f 条、单条 %.1f 字 —— 这是「%s」的记法。"
                 % (p["per_day"], p["avg_words"],
                    ("碎片灵感为主" if p["short"] >= p["memos"] * 0.7 else
                     ("成段思考为主" if p["long"] >= p["memos"] * 0.2 else
                      "长短混合"))))
    lines.append("")
    lines.append("## 分类习惯")
    lines.append("")
    for t, n in (p.get("groups") or [])[:10]:
        lines.append("- %s：%d 条" % (t, n))
    if not p.get("groups"):
        lines.append("- 这批笔记基本没打标签")
    lines.append("")
    lines.append("## 常打的标签")
    lines.append("")
    lines.append("、".join("%s(%d)" % (t, n) for t, n in (p.get("tags") or [])[:20]) or "无")
    lines.append("")
    lines.append("## 节奏")
    lines.append("")
    if p.get("busiest"):
        lines.append("记最多的几天：%s"
                     % "、".join("%s（%d 条）" % (d, n) for d, n in p["busiest"]))
    if p.get("hours"):
        lines.append("高峰时段：%s"
                     % "、".join("%02d 点（%d 条）" % (h, n) for h, n in p["hours"]))
    lines.append("最近有记录的那几天里有 %d 条。" % p.get("last7", 0))
    lines.append("")
    lines.append("## 深加工")
    lines.append("")
    lines.append("已经收进书架、被当成一本书来读的有 %d 条。" % p.get("shelved", 0))
    lines.append("")
    lines.append("> 这份画像只由条数、字数、日期与标签算出，不含任何一条笔记原文。")
    return "\n".join(lines).strip() + "\n"


def portrait_dir(flomo_dir):
    """画像落在存储目录下的指定文件夹：`cache/flomo/portrait/`。

    跟着 flomo_dir 走而不是另起一处 —— 「清除本地数据」清 cache/flomo 时画像也该没，
    否则用户删了笔记，Agent 却还拿旧画像做事。
    """
    d = os.path.join(flomo_dir, PORTRAIT_DIR)
    os.makedirs(d, exist_ok=True)
    return d


def write_portrait(flomo_dir, data=None):
    """算一次、写一份（JSON 给机器、Markdown 给人和 Agent）。返回两个路径。"""
    data = data if data is not None else load(flomo_dir)
    p = build_portrait(data)
    d = portrait_dir(flomo_dir)
    for name, text in ((PORTABLE_JSON, json.dumps(p, ensure_ascii=False, indent=1)),
                        (PORTABLE_MD, portrait_markdown(p))):
        tmp = os.path.join(d, name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, os.path.join(d, name))
    return {"json": os.path.join(d, PORTABLE_JSON),
            "md": os.path.join(d, PORTABLE_MD), "portrait": p}


def read_portrait(flomo_dir):
    """读回画像；没有就现算一份并落盘（首次让 Agent 读时不该拿到空的）。"""
    jp = os.path.join(flomo_dir, PORTRAIT_DIR, PORTABLE_JSON)
    try:
        with open(jp, encoding="utf-8") as f:
            p = json.load(f)
        if isinstance(p, dict):
            return p
    except Exception:
        pass
    return write_portrait(flomo_dir)["portrait"]


def portrait_markdown_path(flomo_dir):
    return os.path.join(flomo_dir, PORTRAIT_DIR, PORTABLE_MD)
