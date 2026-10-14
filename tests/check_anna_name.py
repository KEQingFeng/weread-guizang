# -*- coding: utf-8 -*-
"""安娜这一路「远端给的文件名」的落盘长度体测：截断必须按字节，不能按字符。

为什么单开一条门禁：`anna_state.safe_name` 原来的上限是「120 个字符」，而目录项
的上限是 **255 字节**（ext4 / APFS 都一样）。一个中文字 UTF-8 占三个字节，所以
120 个中文看着只有 120 个字符、其实是 360 字节 —— 名字「没超」，`open()` 却当场
抛 ENAMETOOLONG，窗口里那句话是「这份没接住：OSError」，用户看到的就是「点了下载
却什么也没有」。这一条不联网、不开浏览器：坏名字现编，落盘写在系统临时目录里，
跑完整棵删掉。

钉住五条：
  · 字节与字符两条上限同时管住（结果一定写得进磁盘）；
  · 真扩展名（≤ MAX_EXT_CHARS）留得住，长点号尾巴不冒充扩展名；
  · 截断不切出半个字（结果里不许出现替换符）；
  · 落单的半个代理码点先摘掉再量长度（`name.encode("utf-8")` 碰到它会抛，
    那一下发生在接住下载的回调里，用户看到的是「点了没反应」）；
  · 洗过的名字再洗一遍不变（幂等），且给 anna_browser 追加 `-<时间戳>-<序号>`
    之后仍在 255 字节以内。
"""
import os
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import anna_state as anna  # noqa: E402

FAIL = []
PASSED = [0]


def chk(name, ok, extra=""):
    if ok:
        PASSED[0] += 1
        print("  ok  " + name)
    else:
        FAIL.append(name)
        print(" FAIL " + name + (("  <- " + str(extra)[:220]) if extra else ""))


def esc(s):
    """bad-name 的 extra 里可能带着落单的半个代理码点：直接 print 会自己抛
    UnicodeEncodeError，把一条断言失败伪装成整个套件崩。转义成 \\ud83d 再打。"""
    return str(s).encode("unicode_escape").decode("ascii")


def has_surrogate(s):
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in str(s))


TMP = tempfile.mkdtemp(prefix="guizang-anna-name-")

# 远端可能给回来的各种坏名字：中文长标题、ASCII 长名、点号后面拖一大截、
# 没有点号、尾巴像扩展名但其实不是。
NASTY = [
    "长" * 300 + ".pdf",
    "长" * 300,
    "书" * 60 + ".epub",
    "a" * 200 + "." + "b" * 200,
    "a" * 400,
    "长" * 100 + "." + "扩展名真的是这一截吗" * 20,
    "这" * 79 + ".epub",                     # 79 字 × 3 = 237 字节：名字看着短，字节已经贴着上限
    "x" * 119 + ".epub",                     # 字符没超，加扩展名后字节数才超
    "." * 300 + ".pdf",
    "长" * 300 + "." + "p" * 300,
]


def creatable(name):
    """这个名字能不能在磁盘上真建出来 —— 这才是这一层要保证的事。"""
    path = os.path.join(TMP, name)
    try:
        with open(path, "wb") as f:
            f.write(b"x")
        os.unlink(path)
        return True, ""
    except OSError as e:
        return False, e


try:
    # ── 1 短名字一个字不动（这层只该动「不安全的」名字） ────────────────
    for good in ("正常的一本书.epub", "The_Book_(2026).pdf", "notes", "a.b.c.md"):
        chk("短名字原样留着：" + good, anna.safe_name(good) == good, anna.safe_name(good))

    # ── 2 防穿越与清洗的老口径仍然成立 ─────────────────────────────────
    chk("`../../etc/passwd` 只剩最后一截", anna.safe_name("../../etc/passwd") == "passwd")
    chk("反斜杠也算分隔符", anna.safe_name(r"..\..\windows\x.epub") == "x.epub")
    chk("纯点号名字退回「未命名」",
        anna.safe_name("..") == "未命名" and anna.safe_name(".") == "未命名",
        [anna.safe_name(".."), anna.safe_name(".")])
    chk("控制字符被摘掉", "\x00" not in anna.safe_name("一本\x00怪名字.epub"))
    chk("空格与斜杠换成下划线",
        " " not in anna.safe_name("这 本 / 那 本.epub")
        and "/" not in anna.safe_name("这 本 / 那 本.epub"))

    # ── 3 两条上限同时管住，而且真写得进磁盘 ──────────────────────────
    for raw in NASTY:
        got = anna.safe_name(raw)
        nchars, nbytes = len(got), len(got.encode("utf-8"))
        ok_disk, err = creatable(got)
        chk("字符数 ≤ %d：%r…" % (anna.MAX_NAME_CHARS, raw[:12]), nchars <= anna.MAX_NAME_CHARS,
            (nchars, got[:30]))
        chk("UTF-8 字节 ≤ %d：%r…" % (anna.MAX_NAME_BYTES, raw[:12]), nbytes <= anna.MAX_NAME_BYTES,
            (nbytes, got[:30]))
        chk("这份名字真建得出来：%r…" % raw[:12], ok_disk, err)

    # ── 4 截断不切字、不吐替换符，洗两遍还是一样的 ─────────────────────
    for raw in NASTY:
        got = anna.safe_name(raw)
        chk("没有半个字（无 U+FFFD）：%r…" % raw[:12], "\ufffd" not in got, repr(got[-16:]))
        chk("幂等：洗过的再洗一遍不变：%r…" % raw[:12],
            anna.safe_name(got) == got, (got, anna.safe_name(got)))

    # ── 5 真扩展名留得住；长点号尾巴不许冒充扩展名 ─────────────────────
    chk("长中文名截完仍带 .pdf", anna.safe_name("长" * 300 + ".pdf").endswith(".pdf"),
        anna.safe_name("长" * 300 + ".pdf"))
    chk("长英文名截完仍带 .epub", anna.safe_name("a" * 200 + ".epub").endswith(".epub"),
        anna.safe_name("a" * 200 + ".epub"))
    long_tail = anna.safe_name("a" * 200 + "." + "b" * 200)
    chk("点号后两百个字的尾巴不当扩展名（不为它把正文截没）",
        not long_tail.endswith("b" * 20) and len(long_tail) <= anna.MAX_NAME_CHARS, long_tail[:40])

    # ── 6 接住下载时还要追加 `-<时间戳>-<序号>`，拼完仍得在 255 字节内 ──
    for raw in NASTY:
        got = anna.safe_name(raw)
        worst = got + "-1750000000-99"
        chk("追加时间戳与序号后仍 < 255 字节：%r…" % raw[:12],
            len(worst.encode("utf-8")) < 255, len(worst.encode("utf-8")))

    # ── 7 上限本身是「按字节」这一件事，别被后人改回字符数 ──────────────
    chk("`长` × 100（300 字节）的名字被收下了，不是原样放行",
        len(anna.safe_name("长" * 100).encode("utf-8")) <= anna.MAX_NAME_BYTES,
        len(anna.safe_name("长" * 100).encode("utf-8")))

    # ── 8 落单的半个代理码点：先摘掉再量长度，别把这一份下载打回去 ─────
    # 浏览器的 JS 字符串被拦腰截断时会留下 U+D800–U+DFFF 里落单的一个，它会从
    # `download.suggested_filename` 一路走到 `name.encode("utf-8")` 上抛
    # UnicodeEncodeError —— 接住下载的回调里没人兜，表现是「点了下载什么也没有」。
    HALF = "\ud83d"
    SHORT = [
        ("短名中间夹一个", "a%sb.epub" % HALF, "ab.epub"),
        ("只剩名字开头一个", "%s.epub" % HALF, ".epub"),
        ("中文名中间夹一个", "书%s名.pdf" % HALF, "书名.pdf"),
        ("整串都是坏字符", HALF * 300, "未命名"),
    ]
    for label, raw, want in SHORT:
        try:
            got = anna.safe_name(raw)
        except Exception as e:                       # 修好之前这里就是 UnicodeEncodeError
            chk("坏字符不当场抛（%s）" % label, False, "%s: %s" % (type(e).__name__, e))
            continue
        chk("坏字符不抛、并原样摘掉（%s）" % label, got == want, esc(got))
        chk("坏字符不留代理码点（%s）" % label, not has_surrogate(got), esc(got))

    # 长名会走「按字节截断」那一段：坏字符必须先摘，否则 encode 在量长度时就抛。
    for label, raw in (("长中文名", "长" * 300 + HALF + ".pdf"),
                       ("长英文名开头", HALF + "a" * 400),
                       ("长英文名结尾", "x" * 200 + HALF)):
        try:
            got = anna.safe_name(raw)
        except Exception as e:
            chk("坏字符长名量得动（%s）" % label, False, "%s: %s" % (type(e).__name__, e))
            continue
        ok_disk, err = creatable(got)
        chk("坏字符长名仍在字节上限内（%s）" % label,
            len(got.encode("utf-8")) <= anna.MAX_NAME_BYTES, len(got.encode("utf-8")))
        chk("坏字符长名真建得出来（%s）" % label, ok_disk, err)
        chk("坏字符长名不留代理码点（%s）" % label, not has_surrogate(got), esc(got))
        chk("坏字符长名幂等（%s）" % label, anna.safe_name(got) == got, esc(anna.safe_name(got)))

    # 成对的码点（真字符）不是坏字符，不能顺手删掉：emoji 是 4 字节、仍是合法文本。
    chk("真的 emoji 原样留着（只摘落单的那半）",
        anna.safe_name("书\U0001F600.epub") == "书\U0001F600.epub",
        esc(anna.safe_name("书\U0001F600.epub")))
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("\n安娜落盘文件名：通过 %d 项，失败 %d 项" % (PASSED[0], len(FAIL)))
for f in FAIL:
    print("  × " + f)
sys.exit(1 if FAIL else 0)
