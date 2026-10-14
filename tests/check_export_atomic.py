# -*- coding: utf-8 -*-
"""取书账本落盘的原子性：目录 / 钉住记录 / meta 不再写一半。

为什么单独一份：取书是**会被中止**的长任务（界面那颗停止键就是杀进程），而
export_precise 里三份账本原先都是 `open(path, "w")` 直写 —— `"w"` 一进去就把旧
内容截断了，恰好在写完之前被杀，盘上就只剩半截 JSON：

  * `_catalog.json` → json.load 失败，读侧当「没有目录」，续传锚点丢掉位置；
  * `_stuck.json`   → 「这一项连着两轮钉不住」的记账归零，下一轮重新钉同一处；
  * `meta.json`     → `book_layout.read_meta()` 只能当没有 meta，书名 / 作者 /
                      done / 章节数一起变回默认，界面显示成「这本没取完」。
                      meta.json 恰恰是在整趟取书**收尾那一刻**才写的，用户等完
                      一场再点停止，最容易砸在这一步。

仓库里其它账本早就是「先写 .tmp 再 os.replace」（画板 / 导图 / 写作 / 登录态 /
订阅 / 安娜 / 云同步…），只有引擎这三份还是直写。这一条把原子写口钉住：正常写
一个字不改、写一半失败时旧档完整、失败或中断后不留 .tmp，并留一条「直写确实会
把旧档砸坏」的对照，免得门禁空转。纯逻辑 + 临时目录，不联网、不开浏览器。
"""
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import export_precise as E  # noqa: E402

FAIL = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if not cond:
        FAIL.append(name)


TMP = tempfile.mkdtemp(prefix="guizang-atomic-")


def path_in(name):
    return os.path.join(TMP, name)


def read_text(p):
    with io.open(p, encoding="utf-8") as f:
        return f.read()


def read_json(p):
    return json.loads(read_text(p))


# ── A. 写口本身 ────────────────────────────────────────────────────

p = path_in("a.json")
E._dump_json(p, {"t": "书名", "n": 1})
chk("基本：写出来就是那份 JSON", read_json(p) == {"t": "书名", "n": 1}, read_text(p))
chk("基本：中文按原字写，不是 \\u 转义", "书名" in read_text(p))
chk("基本：改名完成，不留 .tmp", not os.path.exists(p + ".tmp"))
E._dump_json(path_in("ind.json"), {"a": 1}, indent=2)
chk("基本：indent 传得进去（meta 那份要缩进）",
    read_text(path_in("ind.json")).startswith("{\n"))

# 覆盖：先有一份旧的，再写一份新的 —— 盘上只剩新的
E._dump_json(p, ["新"])
chk("覆盖：旧内容整份换成新的", read_json(p) == ["新"])
chk("覆盖：不留 .tmp", not os.path.exists(p + ".tmp"))

# ── B. 写一半崩：旧档必须完整，.tmp 必须收掉 ──────────────────────────

p2 = path_in("b.json")
E._dump_json(p2, {"v": "旧的完整账"})
good = read_text(p2)


class _BoomJSON:
    @staticmethod
    def dump(*a, **k):
        # 半截写出去再炸：这正是「写到一半断电 / 被杀」的最小复现
        a[1].write('{"v": "新')
        raise KeyboardInterrupt()


real_json = E.json
try:
    E.json = _BoomJSON
    try:
        E._dump_json(p2, {"v": "新的"})
        chk("中断（BaseException）：确实抛出来了", False, "没抛")
    except KeyboardInterrupt:
        chk("中断（BaseException）：抛的是它自己那种异常", True)
finally:
    E.json = real_json
chk("中断：旧档一个字没动", read_text(p2) == good, read_text(p2))
chk("中断：不留 .tmp（BaseException 也要收）", not os.path.exists(p2 + ".tmp"))


class _BoomOS:
    remove = staticmethod(os.remove)

    @staticmethod
    def replace(*a, **k):
        raise OSError("改名失败")


real_os = E.os
try:
    E.os = _BoomOS
    try:
        E._dump_json(p2, {"v": "新的"})
        chk("改名失败：确实抛出来了", False, "没抛")
    except OSError:
        chk("改名失败：抛的是它自己那种异常", True)
finally:
    E.os = real_os
chk("改名失败：旧档一个字没动", read_text(p2) == good, read_text(p2))
chk("改名失败：不留 .tmp", not os.path.exists(p2 + ".tmp"))

# 序列化这一步就炸（塞了写不进 JSON 的东西）同样不许毁旧档
try:
    E._dump_json(p2, {"v": object()})
    chk("写不进 JSON：确实抛出来了", False, "没抛")
except TypeError:
    chk("写不进 JSON：抛的是它自己那种异常", True)
chk("写不进 JSON：旧档一个字没动", read_text(p2) == good, read_text(p2))
chk("写不进 JSON：不留 .tmp", not os.path.exists(p2 + ".tmp"))

# ── C. 对照：老写法（open w + json.dump）确实会把旧档砸坏 ──────────────
# 没有这一条，上面的断言可能只是因为「写坏了也没人看」。这里按老写法走一遍，
# 要求旧档**真的**读不回来了 —— 证明原子写不是多余的。

p3 = path_in("c.json")
E._dump_json(p3, {"v": "旧的完整账"})
try:
    with io.open(p3, "w", encoding="utf-8") as f:
        f.write('{"v": "新')
        raise KeyboardInterrupt()
except KeyboardInterrupt:
    pass
try:
    json.loads(read_text(p3))
    broken = False
except ValueError:
    broken = True
chk("对照：老写法写一半后旧档已经读不回来（原子写不是多余的）", broken, read_text(p3))

# ── D. 真接线：进度 / 钉住记录走的是同一个写口 ────────────────────────

book = path_in("book")
os.makedirs(book, exist_ok=True)
E.write_progress(book, running=True, pages=7, chapters=2)
prog = read_json(os.path.join(book, "_progress.json"))
chk("_progress.json：照常写得出、字段没丢",
    prog.get("running") is True and prog.get("pages") == 7 and prog.get("chapters") == 2,
    prog)
chk("_progress.json：不留 .tmp", not os.path.exists(os.path.join(book, "_progress.json.tmp")))

stuck = path_in("_stuck.json")
E.stuck_bump(stuck, 3, 1)
chk("_stuck.json：照常写得出、还是合法 JSON", isinstance(read_json(stuck), dict))
chk("_stuck.json：不留 .tmp", not os.path.exists(stuck + ".tmp"))

# ── E. 接线：三份账本都过 _dump_json，不再有直写 ──────────────────────
src = io.open(os.path.join(ROOT, "export_precise.py"), encoding="utf-8").read()
chk("接线：_dump_json 是唯一的账本写口",
    "def _dump_json(path, obj, indent=None)" in src)
chk("接线：目录 dump 走 _dump_json", "_dump_json(catalog_path, titles)" in src)
chk("接线：钉住记录走 _dump_json", "_dump_json(stuck_path," in src)
chk("接线：meta 收尾走 _dump_json",
    '_dump_json(os.path.join(book_dir, "meta.json"), meta, indent=2)' in src)
chk("接线：进度也过同一个写口（不再抄一份 .tmp 逻辑）", "_dump_json(path, data)" in src)
_DIRECT = ('with open(catalog_path, "w"', 'with open(stuck_path, "w"',
           'with open(os.path.join(book_dir, "meta.json"), "w"')
chk("接线：没有哪份账本还在用直写（w 模式已被 _dump_json 取代）",
    not [s for s in _DIRECT if s in src],
    [s for s in _DIRECT if s in src])
# 原子写会留下 `<名>.tmp` 半截件（只有改名之前被强杀才会）。它不属于任何读口，
# 清残稿也必须连它一起收，否则永远留在书目录里没人管。
srv = io.open(os.path.join(ROOT, "ui_server.py"), encoding="utf-8").read()
chk("接线：清残稿把三份账本的 .tmp 半截件一起收",
    all(('"%s"' % n) in srv for n in
        ("_catalog.json.tmp", "_progress.json.tmp", "_stuck.json.tmp")),
    [n for n in ("_catalog.json.tmp", "_progress.json.tmp", "_stuck.json.tmp")
     if ('"%s"' % n) not in srv])
chk("接线：改书名 / 标完成那条 meta 也走 ui_server._write_json",
    "_write_json(mp, cur)" in srv)
chk("接线：ui_server 里 json.dump 只剩原子写口那一处（没有别的直写账本）",
    srv.count("json.dump(") == 1, srv.count("json.dump("))

shutil.rmtree(TMP, ignore_errors=True)
print()
print("取书账本原子落盘：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
