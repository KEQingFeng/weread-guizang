#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「清除本地数据」也得收走 `.part` 半截件：同步断在改名之前的账本不再留在 cache/。

为什么单开这一条
    上一轮（`#174`）给清残稿（`ui_server.drop_stray_tmps`）把 `.part` 并进了同一把
    「按尾巴收」的尺子 —— 但它只扫**书目录**。而 `.part` 半截件还有一层：同步取回
    远端文件时先写 `<目标>.part` 再改名（`sync._write_atomic`），`sync._collect`
    会把 `cache/` 里的 `readstat.json` / `library.json` / `notes_index.json` 也列进
    同步对。同步中途被强杀（断网断电、关窗口、活动监视器里杀进程），`cache/` 里就
    留下一份半截账。`cleanup._residue_re` 原先只认 `.tmp` 两族、清残稿又够不到
    `cache/`，于是这一份两份刀都收不走，点完「清除本地数据」（换机、把机器送人
    之前最常点的那一下）它还在盘上。

这一条钉四面：
  A. 名字这把尺子本身：`<账本>.part` 认，旧的 `.tmp` 两族不退化，锚死在账本名上、
     不误伤别本账的 `.part`；
  B. 真实生产者形状（行为级，不靠源码字面量）：拦下 `sync._write_atomic` 的
     `os.replace` 造一次中断，看它**实际**留在盘上的临时件名过不过这把尺子；正常
     写完不留 `.part`；`_collect` 点名的 cache 账本确实都在白名单里；
  C. 试算与真清：`cache/*.part` 残件进「数据」组清单、真删得掉，试算只看不删；
  D. 红线与对照：不认识的 `.part`、符号链接（哪怕名字就是残件形状）、同名目录一概
     不碰；再留一面「天只认 `.tmp` 的旧尺子收不到 `.part`」的镜子，防门禁空转。

全程临时沙盒，不碰真实 cache / 书库，不联网。纯标准库，归 --fast。
"""
import os
import pathlib
import re
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-clean-part-"))
DATA = SB / "data"
BOOKS = SB / "books"
os.environ["GUIZANG_DATA"] = str(DATA)
os.environ["GUIZANG_BOOKS"] = str(BOOKS)

import cleanup as cl   # noqa: E402
import sync            # noqa: E402

CACHE = DATA / "cache"
FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


# ── A. 名字这把尺子本身 ────────────────────────────────────────────
re_ = cl._residue_re("readstat.json")
chk("尺子：认同步那一处的 readstat.json.part", bool(re_.match("readstat.json.part")))
chk("尺子：旧的固定名 readstat.json.tmp 不退化（回归）",
    bool(re_.match("readstat.json.tmp")))
chk("尺子：旧的 pid + 随机串 readstat.json.4242.1a2b3c4d.tmp 不退化（回归）",
    bool(re_.match("readstat.json.4242.1a2b3c4d.tmp")))
chk("尺子：前面多一个字不算（锚在开头）", not re_.match("xreadstat.json.part"))
chk("尺子：别本账的 .part 不算", not re_.match("library.json.part"))
chk("尺子：`.part.tmp` 这种叠罗汉不算",
    not re_.match("readstat.json.part.tmp"))
# 对照：上一轮那把尺子（只认 .tmp 两族）量不到 .part —— 这一刀不是空转。
OLD_RE = re.compile(r"^readstat\.json(?:\.tmp|\.\d+\.[0-9a-f]{8}\.tmp)$")
chk("对照：只认 .tmp 的旧尺子量不到 readstat.json.part",
    not OLD_RE.match("readstat.json.part"))


# ── B. 真实生产者形状（行为级，不靠源码字面量） ────────────────────
CACHE.mkdir(parents=True, exist_ok=True)
target = str(CACHE / "readstat.json")
seen = []
_real_replace = sync.os.replace


def _capture_then_abort(src, dst):
    seen.append(src)
    raise KeyboardInterrupt("同步中途被强杀（模拟）")


sync.os.replace = _capture_then_abort
try:
    raised = False
    try:
        sync._write_atomic(target, b'{"read": 1')
    except BaseException:
        raised = True
finally:
    sync.os.replace = _real_replace
chk("sync._write_atomic 中断时往外抛（BaseException 不吞）", raised)
chk("sync._write_atomic 真落过一个临时件（不是没写就改名）", len(seen) == 1, seen)
produced = os.path.basename(seen[0]) if seen else ""
chk("它实际留在盘上的那个名字过得了这把尺子",
    bool(produced) and bool(cl._residue_re("readstat.json").match(produced)), produced)
chk("中断的那一次自己把半截件收干净了（sync 的 except BaseException）",
    not os.path.exists(target + ".part"))

sync._write_atomic(target, b'{"read": 2}')
chk("正常写完内容落地、不留 .part",
    pathlib.Path(target).read_bytes() == b'{"read": 2}'
    and not os.path.exists(target + ".part"))

# `_collect` 点名的 cache 账本必须都在这张白名单里，否则尺子再宽也够不到它们。
_collected = {os.path.basename(p) for _rel, p in
              sync._collect(str(CACHE), str(BOOKS), {"time": True, "records": True})}
_whitelisted = {n for n, _ in cl.CACHE_FILES}
chk("接线：sync 会同步的 cache 账本都在清理白名单里（readstat / library / notes_index）",
    {"readstat.json", "library.json", "notes_index.json"} <= _collected
    and _collected <= _whitelisted, sorted(_collected - _whitelisted))


# ── C. 试算与真清 ──────────────────────────────────────────────────
# 三种形状各来一份：白名单账本的 .part 半截（会被收）、不认识的 .part、名字像残件
# 却是链接 / 目录的（一概不许碰）。
R_READSTAT = CACHE / "readstat.json.part"
R_CONFIG = CACHE / "config.json.part"
DECOY_UNKNOWN = CACHE / "别人的东西.part"
DECOY_LINK = CACHE / "library.json.part"          # 残件形状被占成了一个符号链接
DECOY_DIR = CACHE / "notes_index.json.part"       # 残件形状被占成了一个目录
LINK_TARGET = SB / "outside-target.txt"


def seed():
    CACHE.mkdir(parents=True, exist_ok=True)
    BOOKS.mkdir(parents=True, exist_ok=True)
    R_READSTAT.write_text('{"books": [', encoding="utf-8")
    R_CONFIG.write_text('{"agent_key": "sk-半截', encoding="utf-8")
    DECOY_UNKNOWN.write_text("keep", encoding="utf-8")
    DECOY_DIR.mkdir(exist_ok=True)
    (DECOY_DIR / "里面还有个文件.txt").write_text("keep", encoding="utf-8")
    LINK_TARGET.write_text("keep", encoding="utf-8")
    try:
        os.symlink(str(LINK_TARGET), str(DECOY_LINK))
    except (OSError, NotImplementedError, AttributeError):
        pass          # 建不了链接的机器跳过这一组


def have_link():
    return os.path.islink(str(DECOY_LINK))


def snapshot():
    out = {}
    for root, _dirs, files in os.walk(DATA):
        for f in files:
            p = pathlib.Path(root) / f
            try:
                out[str(p.relative_to(DATA))] = p.read_bytes()
            except OSError:
                out[str(p.relative_to(DATA))] = b"<unreadable>"
    return out


def main():
    # 先把上一步 sync 落下的正本清掉，重铺成「刚出过事故」的样子。
    pathlib.Path(target).unlink(missing_ok=True)
    seed()

    # ── 对照：旧那份精确名清单根本认不到 .part 残件 ─────────────────
    old_paths = [os.path.join(cl.cache_dir(), n) for n, _ in cl.CACHE_ITEMS]
    old_paths += [os.path.join(cl.cache_dir(), n) for n, _ in cl.CACHE_FILES]
    old_paths += [os.path.join(cl.data_dir(), n) for n, _ in cl.ROOT_FILES]
    chk("对照：旧清单里没有任何一个 .part 残件",
        not any(p.endswith(".part") for p in old_paths),
        [p for p in old_paths if p.endswith(".part")])
    for p in old_paths:
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass
    chk("对照：按旧清单删一遍，两份 .part 半截账还在盘上",
        R_READSTAT.exists() and R_CONFIG.exists()
        and "半截" in R_CONFIG.read_text(encoding="utf-8"))
    seed()          # 恢复

    # ── 试算：进清单、exist 为真，且一个字都不删 ────────────────────
    before = snapshot()
    p = cl.plan("data")
    items = {i["key"]: i for i in p["groups"][0]["items"]}
    want = {"data-cache-readstat.json.part", "data-cache-config.json.part"}
    chk("试算：两份 .part 残件都列出来了", want <= set(items), sorted(set(items) - want))
    chk("试算：残件那两行 exist 为真、大小大于零",
        all(items[k]["exist"] and items[k]["size"] > 0 for k in want if k in items),
        {k: (items[k]["exist"], items[k]["size"]) for k in want if k in items})
    chk("试算：标签点明它是「写到一半的临时件」",
        all("临时件" in items[k]["label"] for k in want if k in items),
        {k: items[k]["label"] for k in want if k in items})
    chk("试算：只看不删（整棵数据目录一个字节没动）", snapshot() == before,
        [k for k in before if k not in snapshot()])

    # ── 真清：.part 残件一起走，红线一概不碰 ─────────────────────────
    r = cl.run("data")
    chk("清数据：没报错、确实删了东西", r["ok"] and r["removed"] and r["freed"] > 0,
        {k: r[k] for k in ("ok", "freed", "errors")})
    chk("清数据：两份 .part 残件都没了", not R_READSTAT.exists() and not R_CONFIG.exists(),
        [str(x) for x in (R_READSTAT, R_CONFIG) if x.exists()])
    chk("清数据：不认识的 .part 一个字没动",
        DECOY_UNKNOWN.read_text(encoding="utf-8") == "keep")
    chk("清数据：同名目录还在、里面的文件也在",
        DECOY_DIR.is_dir() and (DECOY_DIR / "里面还有个文件.txt").exists())
    if have_link():
        chk("清数据：符号链接还在（哪怕名字就是残件形状）",
            os.path.islink(str(DECOY_LINK))
            and LINK_TARGET.read_text(encoding="utf-8") == "keep")
    else:
        chk("清数据：符号链接那一组跳过（这台机器建不了链接）", True)

    shutil.rmtree(SB, ignore_errors=True)
    print()
    print("清除本地数据收 .part 残件：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  ✗ " + f)
        return 1
    print("ok  尺子认 .part、真实生产者过尺、试算与真清都收得掉、红线不碰")
    return 0


if __name__ == "__main__":
    sys.exit(main())
