#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「清除本地数据」得把同步取回留下的 `.part` 半截账一起带走（cleanup.py）。

为什么单开这一条
    `check_cleanup_tmp.py` 收的是 `.tmp` 那一族：各模块「先写同目录临时件再
    `os.replace`」，强杀就留下 `<账本>.tmp` / `<账本>.<pid>.<随机>.tmp`。可 **同步
    取回**走的是另一把尺子 —— `sync._write_atomic`（`sync.py:487`）先写的是
    `<目标>.part`，而 `_collect` 把 `cache/readstat.json` / `cache/library.json` /
    `cache/notes_index.json` 也列进了同步对。于是同步中途被强杀（断电、关窗口、
    活动监视器里杀进程），`cache/` 里就留一份半截账：`library.json.part` 是他整个
    书架，`notes_index.json.part` 是他的划线与笔记索引。

    而这一份 `cleanup._residue_re` 原先只认 `.tmp` 两族形状，一个都认不到；
    `ui_server.drop_stray_tmps` 只扫书目录、够不到 `cache/` 与数据根。于是点完
    「清除本地数据」（换机、把机器送人之前最常点的那一下，界面回「已释放 …」），
    `cache/library.json.part` 还躺在盘上。

这一条钉住
  · 尺子：认 `<账本>.part`，也照旧认两种 `.tmp` 形状；不是白名单账本、或名字被
    改动过的 `.part` 一律不认；
  · 对照：只认 `.tmp` 的旧尺子量不到 `<账本>.part` —— 这一刀不是空转；
  · 试算：`.part` 残件进「数据」组清单、`exist` 为真、试算本身一个字节都不删；
  · 真清：`cache/` 里三本账的 `.part` 与数据根上 `订阅.opml.part` 一并没了，
    白名单里的正本也照旧没了；
  · 红线：不在白名单里的 `<别人>.part`、`.part` 收尾的同名目录、`.part` 收尾的
    符号链接一概不碰；
  · 接线：`_residue_re` 里确有 `.part`；真实生产者 `sync._write_atomic` 写的确实是
    `<目标>.part`，且 `_collect` 把 cache 那几本账列进了同步对。

全程临时沙盒，不碰真实 cache / 书库 / 导出目录。纯标准库，离线。
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

import cleanup as cl  # noqa: E402

CACHE = DATA / "cache"

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


# 各本账的 `.part` 残件名，全部按真写口的形状拼（`sync._write_atomic` = `<目标>.part`）。
R_LIB = CACHE / "library.json.part"              # 同步取回被强杀：他整个书架
R_READSTAT = CACHE / "readstat.json.part"        # 阅读时长账
R_INDEX = CACHE / "notes_index.json.part"        # 划线与笔记索引
R_ROOT_OPML = DATA / "订阅.opml.part"            # 数据根上那一份
RESIDUES = (R_LIB, R_READSTAT, R_INDEX, R_ROOT_OPML)
# 一概不许碰的几样。
DECOY_PART = CACHE / "别人的东西.part"           # 不在白名单里的 .part
DECOY_TMP = CACHE / "别人的东西.tmp"             # 不在白名单里的 .tmp（回归）
DECOY_FILE = CACHE / "notes.txt"                 # 正经文件
DECOY_DIR = CACHE / "feed.json.part"             # 残件形状被占成了一个目录
DECOY_LINK = CACHE / "anna.json.part"            # 残件形状被占成了一个符号链接
LINK_TARGET = SB / "outside-target.txt"


def seed():
    CACHE.mkdir(parents=True, exist_ok=True)
    BOOKS.mkdir(parents=True, exist_ok=True)
    (CACHE / "library.json").write_text('{"books": {}}', encoding="utf-8")
    (CACHE / "readstat.json").write_text('{"total": 0}', encoding="utf-8")
    (DATA / "订阅.opml").write_text("<opml/>", encoding="utf-8")
    # 半截件：写到一半的账（故意留着不合法的 JSON，形状就像被强杀那一刻）。
    R_LIB.write_text('{"books": [', encoding="utf-8")
    R_READSTAT.write_text('{"total": 12', encoding="utf-8")
    R_INDEX.write_text('{"notes": [', encoding="utf-8")
    R_ROOT_OPML.write_text("<opml", encoding="utf-8")
    # 红线这几样。
    DECOY_PART.write_text("keep", encoding="utf-8")
    DECOY_TMP.write_text("keep", encoding="utf-8")
    DECOY_FILE.write_text("keep", encoding="utf-8")
    DECOY_DIR.mkdir(exist_ok=True)
    (DECOY_DIR / "里面还有个文件.txt").write_text("keep", encoding="utf-8")
    LINK_TARGET.write_text("keep", encoding="utf-8")
    try:
        os.symlink(str(LINK_TARGET), str(DECOY_LINK))
    except (OSError, NotImplementedError, AttributeError):
        pass          # 没有建符号链接的权限就算了，下面那一组断言跟着跳过


def have_link():
    return os.path.islink(str(DECOY_LINK))


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


def main():
    seed()

    # ── 0. 名字这把尺子本身：`.part` 认、`.tmp` 那两族照旧认、别的都不认 ──────
    re_ = cl._residue_re("library.json")
    chk("尺子：认同步取回留下的 library.json.part", bool(re_.match("library.json.part")))
    chk("尺子：照旧认固定名 library.json.tmp", bool(re_.match("library.json.tmp")))
    chk("尺子：照旧认带 pid + 随机串的 library.json.4242.1a2b3c4d.tmp",
        bool(re_.match("library.json.4242.1a2b3c4d.tmp")))
    chk("尺子：前面多一个字不算（锚在开头）", not re_.match("xlibrary.json.part"))
    chk("尺子：别的账本的 .part 不算", not re_.match("readstat.json.part"))
    chk("尺子：`.part` 后面再多一段不算（不是任意尾巴）",
        not re_.match("library.json.part.1")
        and not re_.match("library.json.1.part")
        and not re_.match("library.json.PART"))

    # ── 1. 对照：只认 `.tmp` 的旧尺子量不到 `.part` ──────────────────────
    old_re = re.compile(r"^%s(?:\.tmp|\.\d+\.[0-9a-f]{8}\.tmp)$" % re.escape("library.json"))
    chk("对照：旧尺子认 library.json.tmp（不是它本来就瞎）",
        bool(old_re.match("library.json.tmp")))
    chk("对照：旧尺子量不到 library.json.part", not old_re.match("library.json.part"))
    chk("对照：新尺子才认得它", bool(cl._residue_re("library.json").match("library.json.part")))

    # ── 2. 试算：`.part` 残件在清单里、exist 为真，且一个字都不删 ───────────
    before = snapshot()
    p = cl.plan("data")
    items = {i["key"]: i for i in p["groups"][0]["items"]}
    want = {"data-cache-library.json.part", "data-cache-readstat.json.part",
            "data-cache-notes_index.json.part", "data-root-订阅.opml.part"}
    chk("试算：四份 `.part` 残件都列出来了", want <= set(items), sorted(set(items) - want))
    chk("试算：残件那几行 exist 为真、大小大于零",
        all(items[k]["exist"] and items[k]["size"] > 0 for k in want if k in items),
        {k: (items[k]["exist"], items[k]["size"]) for k in want if k in items})
    chk("试算：残件的标签点明它是「写到一半的临时件」",
        all("临时件" in items[k]["label"] for k in want if k in items),
        {k: items[k]["label"] for k in want if k in items})
    chk("试算：不认识的 .part / .tmp / 正经文件 / 同名目录 / 符号链接都不进清单",
        {"data-cache-别人的东西.part", "data-cache-别人的东西.tmp",
         "data-cache-notes.txt", "data-cache-feed.json.part",
         "data-cache-anna.json.part"} & set(items) == set(),
        sorted({"data-cache-别人的东西.part", "data-cache-别人的东西.tmp",
                "data-cache-notes.txt", "data-cache-feed.json.part",
                "data-cache-anna.json.part"} & set(items)))
    chk("试算只看不删（整棵数据目录一个字节没动）", snapshot() == before,
        [k for k in before if k not in snapshot()])

    # ── 3. 真清：`.part` 残件一起走，红线一概不碰 ─────────────────────────
    r = cl.run("data")
    chk("清数据：没报错、确实删了东西", r["ok"] and r["removed"] and r["freed"] > 0,
        {k: r[k] for k in ("ok", "freed", "errors")})
    chk("清数据：四份 `.part` 残件全没了", not any(x.exists() for x in RESIDUES),
        [str(x) for x in RESIDUES if x.exists()])
    chk("清数据：白名单里的正本也没了",
        not (CACHE / "library.json").exists() and not (CACHE / "readstat.json").exists()
        and not (DATA / "订阅.opml").exists())
    chk("清数据：不认识的 .part / .tmp 与正经文件一个字没动",
        DECOY_PART.read_text(encoding="utf-8") == "keep"
        and DECOY_TMP.read_text(encoding="utf-8") == "keep"
        and DECOY_FILE.read_text(encoding="utf-8") == "keep")
    chk("清数据：同名目录还在、里面的文件也在",
        DECOY_DIR.is_dir() and (DECOY_DIR / "里面还有个文件.txt").exists())
    if have_link():
        chk("清数据：符号链接还在（哪怕名字长得像 .part 残件）",
            os.path.islink(str(DECOY_LINK)) and LINK_TARGET.read_text(encoding="utf-8") == "keep")
    else:
        chk("清数据：符号链接那一组跳过（这台机器建不了链接）", True)

    # ── 4. 接线：这一刀真挂进了数据组，且 `.part` 真的是被生产出来的 ────────
    src = (ROOT / "cleanup.py").read_text(encoding="utf-8")
    chk("接线：_residue_re 的尺子里有 .part",
        r"\.part)$" in src and r"\.tmp|\.\d+\.[0-9a-f]{8}\.tmp" in src)
    chk("接线：_data_items 把残件并进数据组", "out.extend(_residue_items())" in src)
    chk("接线：只收普通文件（符号链接与同名目录不碰）",
        "os.path.islink(path) or not os.path.isfile(path)" in src)

    sync_src = (ROOT / "sync.py").read_text(encoding="utf-8")
    chk("生产者：sync._write_atomic 写的确实是 `<目标>.part`",
        'tmp = path + ".part"' in sync_src)
    chk("生产者：_collect 把 cache 里那几本账列进了同步对",
        '"readstat.json"' in sync_src and '"library.json"' in sync_src
        and '"notes_index.json"' in sync_src)

    shutil.rmtree(SB, ignore_errors=True)
    bad = [c for c in checks if not c[0]]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} 通过")
    for _ok, name, extra in bad:
        print("  ✗ " + name + "  <- " + extra)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
