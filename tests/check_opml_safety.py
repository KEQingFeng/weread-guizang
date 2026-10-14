# -*- coding: utf-8 -*-
"""OPML 导入的两条安全边：这是「从别处整包拿来的订阅清单」进门的那道闸。

为什么单开一条门禁：OPML 是唯一一种「用户从别的软件 / 网站整包拿来」的文件，按
不可信外部文件办。原来的实现有两处漏：

  · 实体闸门只看开头 8192 字节 —— 在 DOCTYPE 前面垫一段超长注释，就能把 `<!DOCTYPE`
    顶出扫描窗口，闸门形同虚设；在 expat 没有内置放大保护的老 Python 上，这就是一次
    「十亿笑」。
  · 嵌套 outline 的递归没有层数上限 —— 几千层嵌套会把 walk() 的栈撞穿，抛出来的
    RecursionError 不是这个模块对用户说话的方式（那边一律 ValueError + 人话）。

这条不联网：import_opml 走 dry_run，只验地址形状、不真的去订阅；数据目录在 import
之前指到系统临时目录的沙盒，跑完整棵删掉。

钉住这些：
  1. 短的 DOCTYPE + ENTITY 照旧拒（原来那条能力的回归）；
  2. 8192 字节注释垫在前面，仍要拒，且理由还是「实体」；
  3. DOCTYPE 在正中间、前后都垫着大段注释时也要拒；
  4. 上限以内（32 层）的正常嵌套照常导入；
  5. 超过上限（33 层）给的是带「嵌套」字样的 ValueError，不是 RecursionError；
  6. 正常清单里的 `&amp;` 这类 XML 实体照常解，闸门收紧不误伤真实文件。
"""
import os
import pathlib
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

# GUIZANG_DATA 必须在 import feed 之前设好：feed.py 在导入时就把 DATA_DIR / STORE
# 定下来了，设晚了它还是往真实数据目录写。
TMP = tempfile.mkdtemp(prefix="gz-opml-check-")
os.environ["GUIZANG_DATA"] = os.path.join(TMP, "data")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)

import feed  # noqa: E402

FAIL = []
PASSED = [0]


def chk(name, ok, extra=""):
    if ok:
        PASSED[0] += 1
        print("  ok  " + name)
    else:
        FAIL.append(name)
        print(" FAIL " + name + (("  <- " + str(extra)[:220]) if extra else ""))


def err_of(fn, *a, **kw):
    """跑一次，拿回「会被人看见的那句话」。

    正常应当抛 ValueError；抛了别的类型就在结果里标出来 —— RecursionError 光是
    「没抛 ValueError」还不够，这条门禁要能直接说出它抛错了类型。
    """
    try:
        fn(*a, **kw)
    except ValueError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        return "__WRONG_TYPE__%s: %s" % (type(e).__name__, str(e)[:80])
    return ""


def opml(body, head="<title>t</title>"):
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<opml version="2.0"><head>%s</head><body>%s</body></opml>' % (head, body))


# 安全那几条用这一份：不带 XML 声明，于是前面垫注释 / 后面跟注释都还是合式 XML，
# 被拒的唯一理由只能是实体闸门，不会是「声明不在开头」这种解析错误。
def opml_raw(body):
    return '<opml version="2.0"><head/><body>%s</body></opml>' % body


FEED_LINE = '<outline type="rss" text="f" xmlUrl="http://example.com/f.xml"/>'


def nest(n, inner):
    """把 inner 套进 n 层组 outline：最里面那条的深度正好是 n。"""
    return "".join('<outline text="g">' for _ in range(n)) + inner + "</outline>" * n


def _wellformed(text):
    """这段 XML 本身解得开吗（用来证明「被拒」不是因为语法坏了）。"""
    try:
        ET.fromstring(text)
        return True
    except Exception:  # noqa: BLE001
        return False


try:
    # ── 1 短 DOCTYPE + ENTITY：原有能力不许回退 ───────────────────────
    short = '<!DOCTYPE opml [<!ENTITY a "x">]>' + opml(FEED_LINE)
    msg = err_of(feed.import_opml, short, dry_run=True)
    chk("短的 DOCTYPE + ENTITY 照旧拒掉", "实体" in msg, msg)

    # ── 2 超长注释把 DOCTYPE 顶过 8192：闸门必须还是看得见 ─────────────
    pad9000 = "<!--" + "x" * 9000 + "-->"
    chk("垫的注释确实超过 8192（否则下一条测不到点子上）", len(pad9000) > 8192, len(pad9000))
    padded = pad9000 + '<!DOCTYPE opml [<!ENTITY a "aaaaaaaaaa">]>' + opml_raw(FEED_LINE)
    chk("垫长注释的这份本身是合式的（拒它只能是因为实体，不是因为语法）",
        _wellformed(padded), padded[:60])
    msg = err_of(feed.import_opml, padded, dry_run=True)
    chk("注释垫到 8192 之外，实体声明仍被拒（看的是全文，不是开头）", "实体" in msg, msg)

    # ── 3 DOCTYPE 夹在前后两段长注释中间 ─────────────────────────────
    mid = ("<!--" + "y" * 12000 + "-->" + '<!DOCTYPE opml [<!ENTITY a "x">]>'
           + "<!--" + "z" * 12000 + "-->" + opml_raw(FEED_LINE))
    chk("夹在中间的这份也是合式的", _wellformed(mid), mid[:60])
    msg = err_of(feed.import_opml, mid, dry_run=True)
    chk("DOCTYPE 夹在两段长注释中间时也拒", "实体" in msg, msg)

    # ── 4 上限以内照常导入 ───────────────────────────────────────────
    ok32 = feed.import_opml(opml(nest(32, FEED_LINE)), dry_run=True)
    chk("32 层嵌套在闸门以内，照常收下",
        ok32.get("total") == 1 and ok32["results"][0]["status"] == "pending"
        and ok32["results"][0]["url"].endswith("/f.xml"), ok32)

    # ── 5 越过上限：人话，不是 RecursionError ────────────────────────
    msg = err_of(feed.import_opml, opml(nest(33, FEED_LINE)), dry_run=True)
    chk("33 层越界，抛的是带「嵌套」的 ValueError", "嵌套" in msg, msg)
    chk("越界时没有漏出 RecursionError 这种类型", "__WRONG_TYPE__" not in msg, msg)

    very_deep = opml(nest(3000, FEED_LINE))
    msg = err_of(feed.import_opml, very_deep, dry_run=True)
    chk("3000 层的恶意清单也给同一句人话（不再撞穿调用栈）", "嵌套" in msg, msg)

    # ── 6 真实文件里的 XML 实体照常解，不误伤 ────────────────────────
    amp = opml('<outline type="rss" text="A &amp; B" '
               'xmlUrl="http://example.com/a?x=1&amp;y=2"/>')
    got = feed.import_opml(amp, dry_run=True)
    row = (got.get("results") or [{}])[0]
    chk("正常清单里的 &amp; 照常解成 &", row.get("url") == "http://example.com/a?x=1&y=2",
        row)
    chk("带 &amp; 的标题也跟过来了", row.get("title") == "A & B", row)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("\nOPML 导入安全边：通过 %d 项，失败 %d 项" % (PASSED[0], len(FAIL)))
for f in FAIL:
    print("  × " + f)
sys.exit(1 if FAIL else 0)
