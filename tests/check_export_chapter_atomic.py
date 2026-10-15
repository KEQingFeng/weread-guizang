#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""取书章节与合并稿的原子落盘：被杀的半截章不再冒充「已取完」。

上一对门禁（check_export_atomic）管的是三份**账本**（_catalog / _stuck / meta）
别写一半；这一条管的是**正文**：章节 `.md` / `raw/*.json` 与整本合并稿。

为什么正文也得原子写 —— 它不是「重下一张」那种小事：

  · 章节 `.md`：`open(path, "w")` 一进去就把旧内容截断，恰好在写完前被杀，盘上
    剩半截正文。可它的第一行 `# 章名` 是最先写出去的、完好无损，续传时
    `load_used_titles` 只认第一行 —— 于是这一章被算成「已写出来」，正文缺的那
    半截再也没人补。不报错、不丢文件，只是少了一半内容，最不容易被发现。
  · 整本合并稿：用户等了整场取书，最后一步被杀，`<书名>.md` 是个半本，在下次
    导出前它一直躺在 output/ 里像个成品。

改成「先写同目录 .tmp、写完再 os.replace」之后：被杀的章节文件压根不出现
（`.tmp` 不以 `.md` 结尾，`load_used_titles` 不认它），旧档全程完整，失败不留
.tmp，重跑自然补上这一章。

门禁钉四件事：
  A 正常写：章节 .md / raw .json / 合并稿照常落地，正文一个字不改，不留 .tmp；
  B 写到一半被杀（BaseException）：目标位置不出现半截文件 / 旧档一个字没动；
  C 改名失败（OSError）：旧档完整、不留 .tmp、异常照抛；
  D 对照 + 接线：老直写留下的半截章**确实**会被 load_used_titles 认成已写出来
    （证明这条收口不是多余的），且 save_chapter / merge_chapters 都真的走了
    原子写口，不是只写了个函数没人用。

playwright 没装时引擎 import 不起来，这条明确标 SKIP，绝不把「没测」装成「通过」。
跑法：.venv/bin/python tests/check_export_chapter_atomic.py
不联网、不起浏览器、不碰真实书库（全在系统临时目录里）。
"""
import builtins
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent

FAIL = []
SKIPS = []


def chk(name, ok, extra=""):
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   " + repr(extra)[:220]))
    if not ok:
        FAIL.append(name)


def skip(name, why):
    SKIPS.append(name)
    print(("SKIP  " + name + "   " + why)[:260])


def read_text(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8")


def read_json(path):
    return json.loads(read_text(path))


# ---------- 对照：老直写留下的半截章，续传确实会把它当成品 ----------
# 不 import 引擎也能跑：只证明「第一行章名 + 半截正文」这个形状会被
# load_used_titles 那一把尺子（取第一行、剥掉 `# `）认成已写出来。
def control_half_chapter_looks_done():
    tmp = tempfile.mkdtemp(prefix="guizang-chapter-atomic-ctl-")
    try:
        p = os.path.join(tmp, "0000.md")
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write("# 第一章 被杀的章\n\n正文只写了一半")
                raise KeyboardInterrupt()       # 「写到一半被杀」
        except KeyboardInterrupt:
            pass
        first = read_text(p).split("\n", 1)[0].strip()
        chk("对照：老直写被杀后盘上留下半截 .md（不是文件消失）", os.path.exists(p))
        chk("对照：半截 .md 的第一行 `# 章名` 完好 —— 续传会把它当已写出来",
            first == "# 第一章 被杀的章", first)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 真跑：引擎的三处正文写口 ----------
def run_functional():
    try:
        sys.path.insert(0, str(ROOT))
        import export_precise as E          # noqa: E402
    except Exception as e:                  # 多半是没装 playwright
        skip("真跑：章节 / 合并稿的原子落盘",
             "import export_precise 失败（%s: %s），这条起不来" % (type(e).__name__, e))
        return

    tmp = tempfile.mkdtemp(prefix="guizang-chapter-atomic-")
    md_dir = os.path.join(tmp, "chapters")
    raw_dir = os.path.join(tmp, "raw")
    out_dir = os.path.join(tmp, "out")
    for d in (md_dir, raw_dir, out_dir):
        os.makedirs(d, exist_ok=True)

    BODY = "第一章的正文，中文与 🧭 都要在。\n第二行。"
    try:
        # ---- A：正常写 ----
        n, imgs = E.save_chapter("第一章", [{"type": "text", "text": BODY}], 0, md_dir, raw_dir)
        md0 = os.path.join(md_dir, "0000.md")
        raw0 = os.path.join(raw_dir, "0000.json")
        chk("A1 正常：报的字符数是正文长度", n == len(BODY), n)
        chk("A2 正常：章节 .md 落地、章名与正文都在",
            os.path.exists(md0) and "# 第一章" in read_text(md0) and BODY in read_text(md0))
        chk("A3 正常：raw json 落地、是合法 JSON、章名没变",
            read_json(raw0).get("title") == "第一章", read_text(raw0))
        chk("A4 正常：两份都不留 .tmp",
            not os.path.exists(md0 + ".tmp") and not os.path.exists(raw0 + ".tmp"))
        chk("A5 正常：正文里没有半截代理码点",
            not any(0xD800 <= ord(c) <= 0xDFFF for c in read_text(md0)))

        # ---- B：写到一半被杀（BaseException 走 f.write） ----
        # 往模块里塞一个假 open（模块全局优先于内建）：写半截再抛 KeyboardInterrupt，
        # 这正是「进程被停止键强杀在 f.write 那一句」的最小复现。
        real_open = builtins.open

        class _HalfWriteFile:
            def __init__(self, f):
                self._f = f

            def write(self, s):
                self._f.write(s[: max(1, len(s) // 2)])   # 半截
                self._f.flush()
                raise KeyboardInterrupt()                 # 被杀

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self._f.close()
                return False

        def _boom_open(path, mode="r", **kw):
            return _HalfWriteFile(real_open(path, mode, **kw))

        # B1：全新的一章（目标位置本来不存在）—— 被杀后目标不许出现
        E.open = _boom_open
        try:
            try:
                E.save_chapter("第二章 半截", [{"type": "text", "text": BODY}], 1, md_dir, raw_dir)
                raised = False
            except KeyboardInterrupt:
                raised = True
        finally:
            del E.open
        md1 = os.path.join(md_dir, "0001.md")
        chk("B1 被杀：异常照抛", raised)
        chk("B1 被杀：半截章节文件压根不出现（不会冒充已写出来）", not os.path.exists(md1))
        chk("B1 被杀：不留 .tmp", not os.path.exists(md1 + ".tmp"))
        chk("B1 被杀：raw json 也没落地", not os.path.exists(os.path.join(raw_dir, "0001.json")))

        # B2：覆盖已有的一章 —— 被杀后旧内容必须一字未动
        old_md = os.path.join(md_dir, "0002.md")
        with real_open(old_md, "w", encoding="utf-8") as f:
            f.write("# 旧章\n\n旧的完整正文")
        old = read_text(old_md)
        E.open = _boom_open
        try:
            try:
                E.save_chapter("第三章 新", [{"type": "text", "text": BODY}], 2, md_dir, raw_dir)
            except KeyboardInterrupt:
                pass
        finally:
            del E.open
        chk("B2 被杀：旧章一个字没动", read_text(old_md) == old, read_text(old_md))
        chk("B2 被杀：不留 .tmp", not os.path.exists(old_md + ".tmp"))

        # ---- C：改名失败（OSError） ----
        class _BoomOS:
            """除 replace 外一律转发给真 os（save_chapter / merge_chapters 还要 path、
            listdir、remove 这些）。"""

            @staticmethod
            def replace(*a, **k):
                raise OSError("改名失败")

            def __getattr__(self, name):
                return getattr(os, name)

        boom_os = _BoomOS()

        real_os = E.os
        E.os = boom_os
        try:
            try:
                E.save_chapter("第四章 改名失败", [{"type": "text", "text": BODY}], 2,
                               md_dir, raw_dir)
                raised_os = False
            except OSError:
                raised_os = True
            finally:
                E.os = real_os
        finally:
            E.os = real_os
        chk("C1 改名失败：异常照抛", raised_os)
        chk("C2 改名失败：旧章仍是一个字没动", read_text(old_md) == old, read_text(old_md))
        chk("C3 改名失败：不留 .tmp", not os.path.exists(old_md + ".tmp"))

        # ---- D：续传不认半截（被杀留下的 .tmp） ----
        with real_open(os.path.join(md_dir, "0009.md.tmp"), "w", encoding="utf-8") as f:
            f.write("# 被杀的那一章\n\n正文一半")
        used = E.load_used_titles(md_dir)
        chk("D1 续传：被杀留下的 .tmp 不以 .md 结尾，不算「已写出来」",
            E.norm_title("被杀的那一章") not in used, used)
        chk("D2 续传：真正写完的章还在 used 里（口径没被改窄）",
            E.norm_title("第一章") in used and E.norm_title("旧章") in used, used)

        # ---- E：合并稿也原子 ----
        merged = E.merge_chapters(md_dir, out_dir, "样张_书名", "样张", "作者")
        mtext = read_text(merged)
        chk("E1 合并：正常落盘、书名标题行在、正文在",
            "# 样张" in mtext and "旧的完整正文" in mtext and BODY in mtext)
        chk("E2 合并：正常不留 .tmp", not os.path.exists(merged + ".tmp"))

        with real_open(merged, "w", encoding="utf-8") as f:
            f.write("上一版整本")
        prev = read_text(merged)
        E.os = boom_os
        try:
            try:
                E.merge_chapters(md_dir, out_dir, "样张_书名", "样张", "作者")
                raised_m = False
            except OSError:
                raised_m = True
            finally:
                E.os = real_os
        finally:
            E.os = real_os
        chk("E3 合并被杀/改名失败：异常照抛", raised_m)
        chk("E4 合并被杀/改名失败：上一版整本一字未动", read_text(merged) == prev, read_text(merged))
        chk("E5 合并被杀/改名失败：不留 .tmp", not os.path.exists(merged + ".tmp"))

        # ---- F：接线：两处正文写口真的走原子写 ----
        src = io.open(os.path.join(ROOT, "export_precise.py"), encoding="utf-8").read()
        chk("F1 接线：存在 _atomic_write_text 这一个正文原子写口",
            "def _atomic_write_text(path, text)" in src)
        chk("F2 接线：save_chapter 的 .md 过原子写口",
            "_atomic_write_text(os.path.join(md_dir" in src)
        chk("F3 接线：save_chapter 的 raw json 过 _dump_json（不再 json.dump 直写）",
            '_dump_json(os.path.join(raw_dir' in src)
        chk("F4 接线：merge_chapters 过原子写口",
            '_atomic_write_text(merged, "".join(parts))' in src)
        chk("F5 接线：源码里不再有对 md_dir 的直写 open",
            'open(os.path.join(md_dir, f"{ch_idx:04d}.md"), "w"' not in src)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


control_half_chapter_looks_done()
run_functional()

print()
if SKIPS:
    print("取书章节原子落盘取证：跳过 %d 项（%s）" % (len(SKIPS), "、".join(SKIPS)))
print("取书章节原子落盘取证：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
