# -*- coding: utf-8 -*-
"""EPUB 的 OPF 实体闸门：垫一段注释把 DOCTYPE 顶出 4KB 窗口，也照样不许解析。

`book_import._parse_xml` 收的是「从外面带进来的 XML」——安娜的档案领回来的 epub、
用户自己找的包，成员全都不由我们定形状。它原先的实体闸门只看**开头 4096 字节**：

    head = raw[:4096].decode("utf-8", "replace")
    if "<!DOCTYPE" in head or "<!ENTITY" in head:
        return None

可在 OPF 前面垫一段超长注释就能把 `<!DOCTYPE ... [<!ENTITY ...>]>` 顶到窗口外，闸门
等于不存在。而 expat 对**内部**实体是照展开的：下面这份几百字节的「十亿笑」能解出
几十万字符，再深几层就是几个 G —— 内存被撑爆，导入那一下卡死，用户看到的是
「导入没反应」。`feed.import_opml` 早就吃过这一口（那边已经改成扫全篇，见
tests/check_opml_safety.py 的「垫到窗口外仍要拒」），EPUB 这一路一直没跟上。

这一条钉住：
  A 前提：实体真的会展开、4KB 窗口真的挡不住（这两条是「为什么要改」的对照，
    收口哪天被拿掉，这里会先红）；
  B 闸门：垫到窗口外的 / 没垫的 / 只写小写标签名的 / str 与 memoryview 两条路，
    一律不解析；干净 OPF 一个字不改、照常解析出标题（不误伤）；
  C 真产品路径：这样一份坏 OPF 的 epub 走 `read_epub`，不抛、不把展开出来的
    30 万字符当成书名，章节还能按自然序兜底导进来。

纯内存 zip，不联网、不起服务、不碰真实书库，所以归 --fast 静态段。

跑法：.venv/bin/python tests/check_import_entity.py
"""
import atexit
import io
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SANDBOX = tempfile.mkdtemp(prefix="gz-import-entity-")
atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))
os.environ["GUIZANG_DATA"] = os.path.join(SANDBOX, "data")

import book_import as B  # noqa: E402

PASSED = 0
FAIL = []


def chk(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + "   " + repr(extra))


# ── 夹具：一份「十亿笑」OPF。lol5 展开后是 300000 个字符，全部落在 <dc:title> 里。
_ENTITIES = [
    '<!ENTITY lol "lol">',
    '<!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">',
    '<!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">',
    '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">',
    '<!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">',
    '<!ENTITY lol5 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">',
]
EXPANDED = 300000


def _bomb(prefix="", lowercase=False):
    """造一份带内部实体声明的 OPF；`prefix` 垫在 DOCTYPE 前面。"""
    dt = "doctype" if lowercase else "DOCTYPE"
    ent = "entity" if lowercase else "ENTITY"
    body = "\n".join(e.replace("ENTITY", ent) for e in _ENTITIES)
    return ('<?xml version="1.0"?>' + prefix + "<!" + dt + " package [\n" + body + "\n]>\n"
            '<package xmlns="http://www.idpf.org/2007/opf"><metadata '
            'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>&lol5;</dc:title>'
            "</metadata><manifest/><spine/></package>")


PAD = "<!-- " + "x" * 5000 + " -->"      # 5000 字节，足够把 DOCTYPE 顶出 4096 窗口
PADDED = _bomb(PAD)
SHORT = _bomb()


CLEAN = ('<?xml version="1.0"?><!-- 一份正常的 OPF，没有 DTD -->'
         '<package xmlns="http://www.idpf.org/2007/opf"><metadata '
         'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>一本书</dc:title>'
         '<dc:creator>某人</dc:creator></metadata>'
         '<manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/></manifest>'
         '<spine><itemref idref="c1"/></spine></package>')


def old_windowed_gate(raw):
    """收口前的那把尺子（逐字复刻）：只看开头 4096 字节，且区分大小写的字面串。"""
    head = raw[:4096] if isinstance(raw, str) else raw[:4096].decode("utf-8", "replace")
    return "<!DOCTYPE" in head or "<!ENTITY" in head


def build_epub(opf, chapter='<html><body><h1>第一章</h1><p>正文</p></body></html>'):
    """内存里手搓一份 epub；chapter 是要放进 OEBPS 的那篇正文。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container><rootfiles>'
                   '<rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/ch1.xhtml", chapter)
    return buf.getvalue()


# ── A. 前提对照：实体真的会展开，4KB 窗口真的挡不住 ────────────────
try:
    root = ET.fromstring(PADDED.encode("utf-8"))
    title = "".join(root.itertext())
    chk("A1 对照：expat 对内部实体照展开（这几百字节解出 %d 个字符）" % EXPANDED,
        len(title) == EXPANDED, len(title))
    chk("A2 垫一段注释就能把 DOCTYPE 顶出 4096 窗口（旧闸门看不到它）",
        b"<!DOCTYPE" not in PADDED.encode("utf-8")[:4096])
    chk("A3 对照：旧闸门对这份坏 OPF 放行（这就是它挡不住的原因）",
        old_windowed_gate(PADDED) is False)
    chk("A4 对照：没垫的那份旧闸门挡得住（收的不是「一律不认」，是窗口太短）",
        old_windowed_gate(SHORT) is True)

    # ── B. 闸门：扫全篇，坏的一律不解析，好的一字不改 ───────────────
    chk("B1 垫到窗口外的坏 OPF 不再解析（bytes）", B._parse_xml(PADDED.encode("utf-8")) is None)
    chk("B2 没垫的坏 OPF 照样不解析（bytes）", B._parse_xml(SHORT.encode("utf-8")) is None)
    chk("B3 只写小写标签名（<!doctype / <!entity）也不解析",
        B._parse_xml(_bomb(lowercase=True).encode("utf-8")) is None)
    chk("B4 str 那条路同样挡住（不先解码整份再扫）",
        B._parse_xml(PADDED) is None)
    chk("B5 memoryview 这类不吃 bytes 正则的形状也不放过去",
        B._parse_xml(memoryview(PADDED.encode("utf-8"))) is None)
    chk("B6 只有 DOCTYPE、没有 ENTITY 的老口径不放宽",
        B._parse_xml(b'<?xml version="1.0"?><!DOCTYPE package><package><r/></package>') is None)

    good = B._parse_xml(CLEAN.encode("utf-8"))
    chk("B7 干净 OPF 照常解析（不误伤）",
        good is not None and "".join(good.itertext()).find("一本书") >= 0)
    chk("B8 干净 OPF 的 str 路径也一样",
        B._parse_xml(CLEAN) is not None)
    chk("B9 普通注释不是闸门（只有 DOCTYPE / ENTITY 才算标记）",
        B._parse_xml(('<?xml version="1.0"?><!-- 注释 --><package><r/></package>')
                     .encode("utf-8")) is not None)

    # ── C. 真产品路径：坏 OPF 的 epub 不抛、不当书名、章节兜底导得进来 ──
    docs, images, cover, meta = B.read_epub(build_epub(PADDED))
    chk("C1 坏 OPF 的 epub 拆得开（read_epub 不抛）",
        isinstance(docs, list) and isinstance(meta, dict))
    chk("C2 展开出来的 30 万字符没被当成书名（meta 是兜底的空值）",
        "lol" not in (meta.get("title") or "") and len(meta.get("title") or "") < 200,
        meta)
    chk("C3 没有 OPF 的清单也能按自然序兜底，章节没丢", len(docs) >= 1, docs)

    docs2, _images2, _cover2, meta2 = B.read_epub(build_epub(CLEAN))
    chk("C4 好 epub 的元信息一个字不改：标题、作者照读",
        meta2.get("title") == "一本书" and meta2.get("creator") == "某人", meta2)
    chk("C5 好 epub 的 spine 顺序照走，正文仍导进来",
        len(docs2) == 1 and "第一章" in docs2[0][1], docs2)
finally:
    shutil.rmtree(SANDBOX, ignore_errors=True)

print("\nEPUB 实体闸门（全篇扫 / 垫出窗口也拒 / 好包不改）：%d 项通过，失败 %d 项"
      % (PASSED, len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
