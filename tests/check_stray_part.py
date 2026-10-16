# -*- coding: utf-8 -*-
"""清残稿顺手收走 `.part` 半截件：强杀留下的同步 / 插图临时件不再永远占盘。

为什么还要这一条
    第十四轮（`#163`）给清残稿补了 `ui_server.drop_stray_tmps(d)`：按 `.tmp` 尾巴把
    书目录里「写到一半被强杀」留下的半截件一并收走。可临时件不止 `.tmp` 一族 ——
    同步取回远端文件时先写 `<目标>.part` 再改名（`sync._write_atomic`），插图下载同样
    先写 `<图>.part`（`download_images.download_one`）。正常路径都被它们的
    `finally` / `except BaseException` 收干净，但断电 / SIGKILL（关窗口强杀、活动监视器）
    那一档只有磁盘看得见：`.part` 半截件不进任何读口，清残稿又只认 `.tmp`，于是它
    永远删不掉，只能等整本书被删。

    最要命的是同步取回留下的那一份**落在书目录根上**（`notes.json.part` 这类，
    不在 `RESIDUE_DIRS` 那三本子目录里），既不跟 `chapters` / `raw` / `images` 一起被
    清掉，也点不到。所以把 `.part` 并进同一把「按尾巴收」的尺子。

这一条钉四面：
  A. 本体：书目录根与各子目录里的 `.part` 都收得掉，`.tmp` 那族一个不漏（回归），
     正经产物（meta / notes / 插图 / 画板 / 合并稿）一个不碰，返回值就是删掉的个数；
  B. 红线：符号链接（哪怕以 `.part` 收尾）与同名目录都不碰，`os.walk` 也不跟符号
     链接目录走出这本书；
  C. 端到端：`reset_book_output` 之后书目录根上的 `.part` 真没了，书身份（meta）与
     用户笔记（notes）留着，`done` 打回假；
  D. 接线与对照：源码里两个后缀只有一处真相、两个真实生产者写的确实是 `.part`，
     再留一面「只认 `.tmp` 的旧尺子量不到 `.part`」的镜子，防门禁空转。

跑法：纯函数 + 系统临时目录沙盒，不联网、不起服务、不开浏览器，归 --fast。
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = tempfile.mkdtemp(prefix="guizang-stray-part-")
os.environ["GUIZANG_DATA"] = os.path.join(ROOT, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(ROOT, "books")

import ui_server                                          # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def new_book(bid, title="残稿书", done=True):
    """在模块文件夹里铺一本「像书」的目录（meta.json 是 resolve 认它的标记）。"""
    d = os.path.join(ui_server.module_dir("weread"), bid)
    shutil.rmtree(d, ignore_errors=True)
    write(os.path.join(d, "meta.json"),
          json.dumps({"title": title, "author": "某人", "done": done,
                      "source": "weread"}, ensure_ascii=False))
    # 用户自己的划线：清残稿不许碰它（它是这本书唯一的用户数据凭据）
    write(os.path.join(d, "notes.json"),
          json.dumps({"marks": [{"text": "这句要留"}], "entries": []},
                     ensure_ascii=False))
    return d


def left(d, suffix):
    out = []
    for root, _dirs, files in os.walk(d):
        out += [os.path.join(root, n) for n in files if n.endswith(suffix)]
    return sorted(out)


# ── A. 本体：书目录根 + 各子目录的 .part / .tmp 都收，正经产物不动 ─────────
print("── A. drop_stray_tmps 本体（.part 与 .tmp 一起收） ─────")
a = new_book("SP_A")
good = [
    os.path.join(a, "meta.json"),
    os.path.join(a, "notes.json"),
    os.path.join(a, "notes.md"),
    os.path.join(a, "images", "ch0001_img01.png"),        # 下完整的那张图
    os.path.join(a, "boards", "board.json"),
]
for p in good:
    write(p, "正经产物")

strays = [
    os.path.join(a, "notes.json.part"),                   # 同步取回落在书目录根上
    os.path.join(a, "boards", "board.json.part"),         # 不在 RESIDUE_DIRS 里
    os.path.join(a, "images", "ch0001_img02.png.part"),   # 插图下到一半被杀
    os.path.join(a, "meta.json.4242.1a2b3c4d.tmp"),       # 老的 .tmp 一族（回归）
]
for p in strays:
    write(p, "半截")

gone = ui_server.drop_stray_tmps(a)
chk("A1 书目录根与 boards / images 子目录里的 .part 都收掉了",
    left(a, ".part") == [], left(a, ".part"))
chk("A2 .tmp 那一族照旧一个不漏（本次改动没把老规矩弄丢）",
    left(a, ".tmp") == [], left(a, ".tmp"))
chk("A3 返回值就是删掉的个数（四件半截件）", gone == 4, gone)
chk("A4 正经产物（meta / notes / 插图 / 画板）一个不碰",
    all(os.path.isfile(p) for p in good),
    [p for p in good if not os.path.isfile(p)])


# ── B. 红线：链接与同名目录都不碰，也不跟链接走出这本书 ─────────────────
print("── B. 红线：链接 / 同名目录 / 库外 ────────────────────")
b = new_book("SP_B")
outside = os.path.join(ROOT, "outside_dir")
write(os.path.join(outside, "他人目标.part"), "别人的半截件")
outside_target = os.path.join(ROOT, "link_target.part")
write(outside_target, "链接指向的真身")

made_link = True
try:
    os.symlink(outside_target, os.path.join(b, "evil.part"))
except (OSError, NotImplementedError) as e:               # 没有符号链接权限就跳过这一条
    made_link = False
    print("       符号链接不可用，跳过 B1：%r" % (e,))
if made_link:
    ui_server.drop_stray_tmps(b)
    chk("B1 以 .part 收尾的符号链接不删、指向的目标逐字不动",
        os.path.islink(os.path.join(b, "evil.part"))
        and open(outside_target, encoding="utf-8").read() == "链接指向的真身")
else:
    chk("B1 以 .part 收尾的符号链接不删、指向的目标逐字不动（符号链接不可用，按不适用记过）",
        True)

# 以 .part 收尾的目录：可能装着用户自己的东西，只认普通文件
os.makedirs(os.path.join(b, "keep.part"), exist_ok=True)
write(os.path.join(b, "keep.part", "inner.txt"), "用户的东西")
# 链接目录那头也有 .part：os.walk 不跟过去，库外那份一个不少
made_dir_link = True
try:
    os.symlink(outside, os.path.join(b, "linked"))
except (OSError, NotImplementedError):
    made_dir_link = False
b_gone = ui_server.drop_stray_tmps(b)
chk("B2 同名目录 `keep.part` 不碰，里面那份还在",
    os.path.isdir(os.path.join(b, "keep.part"))
    and os.path.isfile(os.path.join(b, "keep.part", "inner.txt")))
chk("B3 os.walk 不跟符号链接目录，库外的 .part 一个不少",
    (not made_dir_link) or os.path.isfile(os.path.join(outside, "他人目标.part")))
chk("B4 这一趟没有可收的普通文件时回 0（不越权删目录）", b_gone == 0, b_gone)


# ── C. 端到端：reset_book_output 收走书目录根上的 .part ─────────────────
print("── C. reset_book_output 端到端 ───────────────────────")
d = new_book("SP_C", title="同步残件书", done=True)
write(os.path.join(d, "notes.json.part"), "半截同步")          # 书目录根，旧尺子漏的就是它
write(os.path.join(d, "boards", "board.json.part"), "半截板")
write(os.path.join(d, "meta.json.7.cafe0123.tmp"), "半截账")
write(os.path.join(d, "chapters", "001.md"), "# 第1章")
write(os.path.join(d, "_catalog.json"), "[]")

r = ui_server.reset_book_output("SP_C")
chk("C1 清稿有应答", isinstance(r, dict) and r.get("ok") is True, r)
chk("C2 书目录根与 boards 里的 .part 收干净了（旧尺子够不着的那一族）",
    left(d, ".part") == [], left(d, ".part"))
chk("C3 老固定名与子目录残稿照旧清",
    not os.path.exists(os.path.join(d, "_catalog.json"))
    and not os.path.exists(os.path.join(d, "chapters")))
mp = json.load(open(os.path.join(d, "meta.json"), encoding="utf-8"))
chk("C4 书身份留着，done 打回假",
    mp.get("title") == "同步残件书" and mp.get("author") == "某人"
    and mp.get("done") is False, mp)
chk("C5 用户笔记不受牵连",
    os.path.isfile(os.path.join(d, "notes.json"))
    and json.load(open(os.path.join(d, "notes.json"),
                       encoding="utf-8"))["marks"], "notes.json 丢了")
chk("C6 应答里报的删除数把 .part 残件也算了进去", r.get("removed", 0) >= 3, r)


# ── D. 接线与对照 ─────────────────────────────────────────────────────
print("── D. 接线与对照 ─────────────────────────────────────")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(REPO, "ui_server.py"), encoding="utf-8").read()
body = src.split("def drop_stray_tmps", 1)[-1].split("\ndef ", 1)[0]
chk("D1 `.part` 尾巴只有一处真相（STRAY_PART_SUFFIX）",
    'STRAY_PART_SUFFIX = ".part"' in src)
chk("D2 drop_stray_tmps 真的按这个常量收（不是写了不用）",
    "STRAY_PART_SUFFIX" in body, body[:260])
chk("D3 老的 `.tmp` 常量与收法都还在（本次是合并、不是替换）",
    'STRAY_TMP_SUFFIX = ".tmp"' in src and "STRAY_TMP_SUFFIX" in body)

src_sync = open(os.path.join(REPO, "sync.py"), encoding="utf-8").read()
src_dl = open(os.path.join(REPO, "download_images.py"), encoding="utf-8").read()
chk("D4 同步取回真的先写 `<目标>.part`（残件的真实来源）",
    'tmp = path + ".part"' in src_sync, "sync._write_atomic 换了落盘写法")
chk("D5 插图下载真的先写 `<图>.part`（残件的真实来源）",
    'tmp = fpath + ".part"' in src_dl, "download_one 换了落盘写法")


def old_rule_only_tmp(target):
    """对照：老尺子只认 `.tmp`，对 `.part` 一个也数不到。"""
    n = 0
    for root, _dirs, files in os.walk(target):
        n += sum(1 for name in files if name.endswith(".tmp"))
    return n


control = new_book("SP_E")
write(os.path.join(control, "notes.json.part"), "半截")
write(os.path.join(control, "boards", "board.json.part"), "半截")
chk("D6 对照：只认 .tmp 的旧尺子量不到 .part（这一条不是空转）",
    old_rule_only_tmp(control) == 0 and ui_server.drop_stray_tmps(control) == 2,
    (old_rule_only_tmp(control), ui_server.drop_stray_tmps(control)))


shutil.rmtree(ROOT, ignore_errors=True)

print()
if FAIL:
    print("✗ check_stray_part：%d 条没过" % len(FAIL))
    for n in FAIL:
        print("   - " + n)
    sys.exit(1)
print("✓ check_stray_part：%d 条全过" % PASSED)
