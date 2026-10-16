#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「清除本地数据」得把账本写到一半的临时件一起带走（cleanup.py）。

为什么单开这一条
    「清除本地数据」是**换机、把机器送人之前最常点的那一下**，删的是归藏自己
    攒下来的东西。可 `run()` 历来按一张**精确路径**白名单删，而各本账的落盘都
    走「先写同目录临时件、再 `os.replace`」：临时件的名字不定 —— 现版是
    `<账本>.<pid>.<八位随机串>.tmp`（`ui_server._write_json` / `anna_state` /
    `book_notes` 那一族的尺子），旧版与 login / platform_compat / feed 那几处
    还写固定的 `<账本>.tmp`。进程正好在改名之前被强杀（断电、关窗口、活动监视器
    里杀进程），盘上就留下那么一份半截账：`config.json` 那份里是用户的接口 Key，
    `library.json` 那份是他整个书架。

    而这份残件谁都收不走：清理白名单按精确名认不到它，`ui_server.drop_stray_tmps`
    只扫书目录、够不到 `cache/` 与数据根。于是点完「清除」（界面回「已释放 …」），
    `cache/config.json.4242.1a2b3c4d.tmp` 还躺在盘上，Key 原样在里头。

这一条钉住
  · 前两种形状的残件（固定名 `.tmp` / pid + 随机串 `.tmp`）都进「数据」组清单，试算里
    `exist` 为真、试算本身不删任何东西；`run("data")` 之后确实没了；
  · **第三种形状：云同步取回留下的 `<账本>.part`**（`sync._write_atomic` 先写
    `<目标>.part` 再改名）。`_collect` 把 `cache/readstat.json` / `library.json` /
    `notes_index.json` 也列进同步对，取回途中被强杀就会在 `cache/` 留下这一份；
    书目录那一头早就是 `.tmp` / `.part` 同一把尺子，这一刀并齐；
  · 清完之后整个数据目录里再也找不到那串 Key（连残件一起 grep 一遍）；
  · 红线：不认识的 `.tmp` / `.part`、正经文件、**同名目录**、**符号链接**一概不碰；
  · 对照：只按旧那份精确名清单删一遍，同一份残件会原样留下 —— 这一刀不是空转。

全程临时沙盒，不碰真实 cache / 书库 / 导出目录。纯标准库，离线。
"""
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-clean-tmp-"))
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


# 一个「漏出去就糟了」的假 Key：残件里就是它。
KEY = "sk-" + "f" * 32
# 各本账的残件名，全部按真写口的形状拼（见 ui_server._write_json / login / platform_compat）。
R_FIXED = CACHE / "config.json.tmp"
R_RANDOM = CACHE / "config.json.4242.1a2b3c4d.tmp"
R_LIB = CACHE / "library.json.999.deadbeef.tmp"
# 第三种形状：云同步取回写到一半（`sync._write_atomic` 的 `path + ".part"`）。这条
# 账本在 `_collect` 的同步对里，取回途中被强杀就会在 cache/ 留下这一份。
R_PART = CACHE / "library.json.part"
R_ROOT_FIXED = DATA / "runtime.json.tmp"
R_ROOT_OPML = DATA / "订阅.opml.77.00ff00ff.tmp"
RESIDUES = (R_FIXED, R_RANDOM, R_LIB, R_PART, R_ROOT_FIXED, R_ROOT_OPML)
# 一概不许碰的几样。
DECOY_TMP = CACHE / "别人的东西.tmp"          # 不在白名单里的 .tmp
DECOY_PART = CACHE / "别人的东西.part"        # 不在白名单里的 .part（白名单仍然管着 .part）
DECOY_FILE = CACHE / "notes.txt"              # 正经文件
DECOY_DIR = CACHE / "feed.json.tmp"           # 残件形状被占成了一个目录
DECOY_PART_DIR = CACHE / "notes_index.json.part"  # 白名单里那本账，但 .part 被占成目录
DECOY_LINK = CACHE / "anna.json.5.12345678.tmp"   # 残件形状被占成了一个符号链接
LINK_TARGET = SB / "outside-target.txt"


def seed():
    CACHE.mkdir(parents=True, exist_ok=True)
    BOOKS.mkdir(parents=True, exist_ok=True)
    (CACHE / "config.json").write_text('{"agent_key": "%s"}' % KEY, encoding="utf-8")
    (CACHE / "library.json").write_text('{"books": {}}', encoding="utf-8")
    (DATA / "runtime.json").write_text('{"port": 8770}', encoding="utf-8")
    # 半截件：写到一半的账（故意留着不合法的 JSON，形状就像被强杀那一刻）。
    R_FIXED.write_text('{"agent_key": "%s"' % KEY, encoding="utf-8")
    R_RANDOM.write_text('{"agent_key": "%s"' % KEY, encoding="utf-8")
    R_LIB.write_text('{"books": []', encoding="utf-8")
    R_PART.write_text('{"books": []', encoding="utf-8")
    R_ROOT_FIXED.write_text('{"port": 8770', encoding="utf-8")
    R_ROOT_OPML.write_text("<opml", encoding="utf-8")
    # 红线这几样。
    DECOY_TMP.write_text("keep", encoding="utf-8")
    DECOY_PART.write_text("keep", encoding="utf-8")
    DECOY_FILE.write_text("keep", encoding="utf-8")
    DECOY_DIR.mkdir(exist_ok=True)
    (DECOY_DIR / "里面还有个文件.txt").write_text("keep", encoding="utf-8")
    DECOY_PART_DIR.mkdir(exist_ok=True)
    (DECOY_PART_DIR / "里面还有个文件.txt").write_text("keep", encoding="utf-8")
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

    # ── 0. 名字这把尺子本身：锚死、两种形状认、别的都不认 ──────────────
    re_ = cl._residue_re("config.json")
    chk("尺子：认固定名 config.json.tmp", bool(re_.match("config.json.tmp")))
    chk("尺子：认带 pid + 随机串的 config.json.4242.1a2b3c4d.tmp",
        bool(re_.match("config.json.4242.1a2b3c4d.tmp")))
    chk("尺子：前面多一个字不算（锚在开头）", not re_.match("xconfig.json.tmp"))
    chk("尺子：别的账本的残件不算", not re_.match("library.json.1.deadbeef.tmp"))
    chk("尺子：团成一团的随机段不算（要 pid + 八位小写十六进制）",
        not re_.match("config.json.abc.deadbeef.tmp")
        and not re_.match("config.json.1.DEADBEEF.tmp"))
    chk("尺子：认云同步取回留下的固定名 config.json.part",
        bool(re_.match("config.json.part")))
    chk("尺子：`.part` 也锚死前后（config.part / xconfig.json.part 都不算）",
        not re_.match("config.part") and not re_.match("xconfig.json.part"))
    chk("尺子：`.partx` / `.part.tmp` 这类近似名不算",
        not re_.match("config.json.partx") and not re_.match("config.json.part.tmp"))

    # ── 1. 对照：旧那份清单（只按精确名）根本认不到残件 ────────────────
    old_paths = [os.path.join(cl.cache_dir(), n) for n, _ in cl.CACHE_ITEMS]
    old_paths += [os.path.join(cl.cache_dir(), n) for n, _ in cl.CACHE_FILES]
    old_paths += [os.path.join(cl.data_dir(), n) for n, _ in cl.ROOT_FILES]
    chk("对照：旧清单里没有任何一个 .tmp 残件",
        not any(p.endswith(".tmp") for p in old_paths),
        [p for p in old_paths if p.endswith(".tmp")])
    chk("对照：旧清单里也没有任何一个 .part 残件",
        not any(p.endswith(".part") for p in old_paths),
        [p for p in old_paths if p.endswith(".part")])
    # 真按旧清单删一遍（就是 run() 干的事：存在的精确路径删掉），残件原样留下。
    for p in old_paths:
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass
    chk("对照：旧清单删完之后，半截 config.json 残件还在盘上",
        R_FIXED.exists() and R_RANDOM.exists() and KEY in R_FIXED.read_text(encoding="utf-8"),
        sorted(str(x) for x in RESIDUES))
    chk("对照：旧清单删完之后，云同步那份 .part 半截件也还在盘上",
        R_PART.exists(), str(R_PART))
    # 恢复到「刚装过、出过事故」的样子，接着走真路径。
    seed()

    # ── 2. 试算：残件在清单里、exist 为真，且一个字都不删 ───────────────
    before = snapshot()
    p = cl.plan("data")
    items = {i["key"]: i for i in p["groups"][0]["items"]}
    want = {"data-cache-config.json.tmp", "data-cache-config.json.4242.1a2b3c4d.tmp",
            "data-cache-library.json.999.deadbeef.tmp", "data-cache-library.json.part",
            "data-root-runtime.json.tmp", "data-root-订阅.opml.77.00ff00ff.tmp"}
    chk("试算：三种形状的残件（固定 .tmp / 随机 .tmp / 同步 .part）都列出来了",
        want <= set(items), sorted(set(items) - want))
    chk("试算：残件那几行 exist 为真、大小大于零",
        all(items[k]["exist"] and items[k]["size"] > 0 for k in want if k in items),
        {k: (items[k]["exist"], items[k]["size"]) for k in want if k in items})
    chk("试算：残件的标签点明它是「写到一半的临时件」",
        all("临时件" in items[k]["label"] for k in want if k in items),
        {k: items[k]["label"] for k in want if k in items})
    decoys = {"data-cache-别人的东西.tmp", "data-cache-别人的东西.part",
              "data-cache-notes.txt", "data-cache-feed.json.tmp",
              "data-cache-notes_index.json.part", "data-cache-anna.json.5.12345678.tmp"}
    chk("试算：不认识的 .tmp / .part、正经文件、同名目录、符号链接都不进清单",
        decoys & set(items) == set(), sorted(decoys & set(items)))
    chk("试算只看不删（整棵数据目录一个字节没动）", snapshot() == before,
        [k for k in before if k not in snapshot()])

    # ── 3. 真清：残件一起走，红线一概不碰 ─────────────────────────────
    r = cl.run("data")
    chk("清数据：没报错、确实删了东西", r["ok"] and r["removed"] and r["freed"] > 0,
        {k: r[k] for k in ("ok", "freed", "errors")})
    chk("清数据：六种形状的残件全没了（含同步那份 .part）",
        not any(x.exists() for x in RESIDUES),
        [str(x) for x in RESIDUES if x.exists()])
    chk("清数据：白名单里的正本也没了",
        not (CACHE / "config.json").exists() and not (CACHE / "library.json").exists()
        and not (DATA / "runtime.json").exists())
    chk("清数据：不认识的 .tmp / .part 与正经文件一个字没动",
        DECOY_TMP.read_text(encoding="utf-8") == "keep"
        and DECOY_PART.read_text(encoding="utf-8") == "keep"
        and DECOY_FILE.read_text(encoding="utf-8") == "keep")
    chk("清数据：同名目录还在、里面的文件也在（.tmp 与 .part 两种形状各一个）",
        DECOY_DIR.is_dir() and (DECOY_DIR / "里面还有个文件.txt").exists()
        and DECOY_PART_DIR.is_dir() and (DECOY_PART_DIR / "里面还有个文件.txt").exists())
    if have_link():
        chk("清数据：符号链接还在（哪怕名字长得像残件）",
            os.path.islink(str(DECOY_LINK)) and LINK_TARGET.read_text(encoding="utf-8") == "keep")
    else:
        chk("清数据：符号链接那一组跳过（这台机器建不了链接）", True)

    # ── 4. 这一刀真正要保的那件事：清完之后数据目录里再没有那串 Key ─────
    leftovers = []
    for root, _dirs, files in os.walk(DATA):
        for f in files:
            p = pathlib.Path(root) / f
            try:
                if KEY.encode() in p.read_bytes():
                    leftovers.append(str(p.relative_to(DATA)))
            except OSError:
                leftovers.append(str(p.relative_to(DATA)) + "（读不出来）")
    chk("清数据：整个数据目录里再也找不到那串 Key（含残件）", leftovers == [], leftovers)

    # ── 5. 接线：残件那一刀真挂进了数据组的清单 ─────────────────────────
    src = (ROOT / "cleanup.py").read_text(encoding="utf-8")
    chk("接线：_data_items 把残件并进数据组", "out.extend(_residue_items())" in src)
    chk("接线：残件只认白名单里那两张表",
        "for name, label in CACHE_FILES:" in src and "for name, label in ROOT_FILES:" in src)
    chk("接线：只收普通文件（符号链接与同名目录不碰）",
        "os.path.islink(path) or not os.path.isfile(path)" in src)
    chk("接线：尺子把 `.part` 与 `.tmp` 并进同一条（sync 那一档）",
        r"\.part" in src and r"\.tmp" in src and "re.escape(name)" in src)

    shutil.rmtree(SB, ignore_errors=True)
    bad = [c for c in checks if not c[0]]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} 通过")
    for _ok, name, extra in bad:
        print("  ✗ " + name + "  <- " + extra)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
