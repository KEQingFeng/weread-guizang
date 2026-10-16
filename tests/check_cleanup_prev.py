#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「清除本地数据」得把重建笔记索引留下的 `notes_index.json.prev` 一起带走（cleanup.py）。

为什么单开这一条
    `/api/notes_index` 的 rebuild 分支在整本重建之前先
    `shutil.copy2(NOTES_IDX, NOTES_IDX + ".prev")`（ui_server.py）—— 那是**上一版
    正本**，不是写到一半的临时件：名字以 `.prev` 收尾，既不在 `<账本>.tmp` 那一族
    的尺子里（`_residue_items` 只认固定名与 `<账本>.<pid>.<随机>.tmp`），也不在
    `run()` 按精确名删的白名单里。于是用户在换机 / 把机器送人之前点的那一下
    「清除本地数据」会把 `notes_index.json` 收走，却把旁边那份**内容一模一样**的
    `.prev` 留在盘上 —— 里面是整份划线与笔记索引。

这一条钉住
  · 生产端与清理端对得上：`ui_server.NOTES_IDX + ".prev"` 的基名就在白名单里，
    且 rebuild 分支真的会写出这一份；
  · 对照：只按「没有这一条」的旧白名单删一遍，`.prev` 原样留下（这一刀不是空转）；
  · 试算：`.prev` 在清单里、`exist` 为真、大小大于零，试算本身一个字节都不删；
  · 真清：`.prev` 没了，且整棵数据目录里再也找不到那份笔记索引的内容；
  · 红线：不在白名单里的兄弟（`.bak` / 同名目录 / 符号链接）一概不碰。

全程临时沙盒，不碰真实 cache / 书库；纯标准库，离线。
"""
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-clean-prev-"))
DATA = SB / "data"
BOOKS = SB / "books"
os.environ["GUIZANG_DATA"] = str(DATA)
os.environ["GUIZANG_BOOKS"] = str(BOOKS)

import cleanup as cl      # noqa: E402
import ui_server as us    # noqa: E402  —— 生产端：NOTES_IDX + ".prev" 就是这里写的

CACHE = DATA / "cache"
IDX = CACHE / "notes_index.json"
PREV = CACHE / "notes_index.json.prev"
# 一份笔记索引里「漏出去就糟了」的内容：正本与 .prev 都写它。
MARK = "划线的私事-" + "x" * 24
# 一概不许碰的几样（都不在白名单里）。
BAK = CACHE / "notes_index.json.bak"          # 正经的兄弟文件（不是残件）
DECOY_DIR = CACHE / "notes_index.json.old"    # 同名形态被占成了一个目录
DECOY_LINK = CACHE / "notes_index.json.orig"  # …以及一个符号链接
LINK_TARGET = SB / "outside-target.txt"

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


def snapshot():
    """整棵数据目录里每个文件的内容与相对路径（比对「一个字节都没动」）。"""
    out = {}
    for root, _dirs, files in os.walk(DATA):
        for f in files:
            p = pathlib.Path(root) / f
            try:
                out[str(p.relative_to(DATA))] = p.read_bytes()
            except OSError:
                out[str(p.relative_to(DATA))] = b"<unreadable>"
    return out


def have_link():
    return os.path.islink(str(DECOY_LINK))


def seed():
    """铺成「刚重建过一次笔记索引、还没清数据」的样子。"""
    CACHE.mkdir(parents=True, exist_ok=True)
    BOOKS.mkdir(parents=True, exist_ok=True)
    doc = '{"items": [{"book": "某本", "text": "%s"}]}' % MARK
    IDX.write_text(doc, encoding="utf-8")
    PREV.write_text(doc, encoding="utf-8")
    BAK.write_text("keep", encoding="utf-8")
    DECOY_DIR.mkdir(exist_ok=True)
    (DECOY_DIR / "里面还有个文件.txt").write_text("keep", encoding="utf-8")
    LINK_TARGET.write_text("keep", encoding="utf-8")
    try:
        os.symlink(str(LINK_TARGET), str(DECOY_LINK))
    except (OSError, NotImplementedError, AttributeError):
        pass          # 建不了链接就算了，那一组断言跟着跳过


def main():
    # ── 0. 接线：生产端写的那一份，正是清理端点名的那一份 ──────────────
    want = os.path.basename(us.NOTES_IDX + ".prev")
    declared = {n for n, _ in cl.CACHE_ITEMS} | {n for n, _ in cl.CACHE_FILES}
    chk("接线：ui_server 重建时 copy 的基名就是 notes_index.json.prev",
        want == "notes_index.json.prev", want)
    chk("接线：这一份在清理白名单里（以前漏的就是它）", want in declared,
        sorted(declared))
    src_srv = (ROOT / "ui_server.py").read_text(encoding="utf-8")
    chk("接线：rebuild 分支确实会写下这一份",
        'NOTES_IDX + ".prev"' in src_srv)

    seed()

    # ── 1. 对照：旧白名单（没有这一条）删一遍，.prev 原样留下 ───────────
    old_paths = [os.path.join(cl.cache_dir(), n) for n, _ in cl.CACHE_ITEMS]
    old_paths += [os.path.join(cl.cache_dir(), n) for n, _ in cl.CACHE_FILES
                  if n != "notes_index.json.prev"]
    old_paths += [os.path.join(cl.data_dir(), n) for n, _ in cl.ROOT_FILES]
    chk("对照：旧清单里没有 .prev 这一条", not any(p.endswith(".prev") for p in old_paths))
    for p in old_paths:
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass
    chk("对照：旧清单删完，正本没了、.prev 还在盘上（它一直漏在这儿）",
        not IDX.exists() and PREV.exists() and MARK in PREV.read_text(encoding="utf-8"),
        sorted(x.name for x in CACHE.iterdir()))
    seed()          # 恢复到「刚重建过」的样子，接着走真路径

    # ── 2. 试算：.prev 在清单里、exist 为真，且一个字都不删 ─────────────
    before = snapshot()
    p = cl.plan("data")
    items = {i["key"]: i for i in p["groups"][0]["items"]}
    key = "data-cache-notes_index.json.prev"
    chk("试算：上一版正本被列出来了", key in items, sorted(items))
    chk("试算：exist 为真、大小大于零",
        key in items and items[key]["exist"] and items[key]["size"] > 0,
        items.get(key))
    chk("试算：标签点明它是「上一版」而不是临时件",
        key in items and "上一版" in items[key]["label"], items.get(key, {}).get("label"))
    chk("试算只看不删（整棵数据目录一个字节没动）", snapshot() == before,
        [k for k in before if k not in snapshot()])

    # ── 3. 真清：.prev 一起走；红线一概不碰 ────────────────────────────
    r = cl.run("data")
    chk("清数据：没报错、确实删了东西", r["ok"] and r["removed"] and r["freed"] > 0,
        {k: r[k] for k in ("ok", "freed", "errors")})
    chk("清数据：正本与上一版都没了", not IDX.exists() and not PREV.exists())
    chk("清数据：不认识的兄弟文件一个字没动",
        BAK.read_text(encoding="utf-8") == "keep")
    chk("清数据：同名目录还在、里面的文件也在",
        DECOY_DIR.is_dir() and (DECOY_DIR / "里面还有个文件.txt").exists())
    if have_link():
        chk("清数据：符号链接还在（哪怕名字长得像备份）",
            os.path.islink(str(DECOY_LINK))
            and LINK_TARGET.read_text(encoding="utf-8") == "keep")
    else:
        chk("清数据：符号链接那一组跳过（这台机器建不了链接）", True)

    # ── 4. 这一刀真正要保的那件事：清完之后再也没有那份笔记索引 ──────────
    leftovers = []
    for root, _dirs, files in os.walk(DATA):
        for f in files:
            fp = pathlib.Path(root) / f
            try:
                if MARK.encode() in fp.read_bytes():
                    leftovers.append(str(fp.relative_to(DATA)))
            except OSError:
                leftovers.append(str(fp.relative_to(DATA)) + "（读不出来）")
    chk("清数据：整个数据目录里再也找不到那份划线 / 笔记索引", leftovers == [], leftovers)

    shutil.rmtree(SB, ignore_errors=True)
    bad = [c for c in checks if not c[0]]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} 通过")
    for _ok, name, extra in bad:
        print("  ✗ " + name + "  <- " + extra)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
