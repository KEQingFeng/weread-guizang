# -*- coding: utf-8 -*-
"""清残稿顺手收走随机名临时件（`*.tmp`）：强杀留下的半截件不再永远占盘。

为什么还要这一条
    落盘原子化的那一轮（`#155`–`#158`）把临时件名从固定名 `meta.json.tmp` 换成了
    pid + 一小串随机（`meta.json.4242.1a2b3c4d.tmp`，见 `ui_server._write_json` /
    `book_notes._tmp_name` / `mindmap` / `video_note` / `ai_sum`）。正常路径由各自的
    `except BaseException` 把残件收干净，可断电 / SIGKILL 那一档只有磁盘看得见：
    它不进任何读口，也不在老 `RESIDUE_FILES` 那份固定名单里 —— 界面上删不掉，
    攒着就是吃盘洞（一本导图或 `_ai` 几 MB）。

    清残稿（`reset_book_output`）是唯一一处「保留书目录、只抹产物」的口。它此前按
    固定名点掉三本账本，随机名那一族一个也点不到。现在补上 `drop_stray_tmps(d)`：
    按 `.tmp` 尾巴把书目录里剩下的半截临时件一并收走，`boards` / `mindmaps` /
    `video` / `_ai` 这些子目录也一起扫。

这一条钉四面：
  A. 本体：随机名 / 固定名 / 各子目录里的 `.tmp` 都收得掉，正经产物（meta / notes /
     合并稿 / board / mindmap）一个不碰，返回值就是删掉的个数；
  B. 红线：符号链接（哪怕名字以 `.tmp` 收尾）与同名目录都不碰，`os.walk` 也不跟
     符号链接目录走出这本书；
  C. 端到端：`reset_book_output` 之后随机名残件真没了，书身份（meta）与用户笔记
     （notes）留着，`done` 打回假；
  D. 接线：源码里 `reset_book_output` 真的调了 `drop_stray_tmps`，而且老固定名那份
     `RESIDUE_FILES` 没有被这次改动顺手删掉。

跑法：纯函数 + 系统临时目录沙盒，不联网、不起服务、不开浏览器，归 --fast。
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = tempfile.mkdtemp(prefix="guizang-stray-tmp-")
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


def tmp_left(d):
    out = []
    for root, _dirs, files in os.walk(d):
        out += [os.path.join(root, n) for n in files if n.endswith(".tmp")]
    return out


# ── A. 本体：随机名 / 固定名 / 各子目录都收，正经产物不动 ───────────────
print("── A. drop_stray_tmps 本体 ───────────────────────────")
a = new_book("ST_A")
strays = [
    os.path.join(a, "meta.json.4242.1a2b3c4d.tmp"),          # 固定名换随机名那一族
    os.path.join(a, "meta.json.tmp"),                        # 老固定名（同尾巴）
    os.path.join(a, "mindmaps", "mindmap.json.99.deadbeef.tmp"),
    os.path.join(a, "boards", "board.json.7.cafe0123.tmp"),
    os.path.join(a, "_ai", "sum.json.1.aaaa1111.tmp"),
]
for p in strays:
    write(p, "半截")
# 正经产物（都不以 .tmp 收尾）得原地不动
keep = [os.path.join(a, "meta.json"), os.path.join(a, "notes.json"),
        os.path.join(a, "mindmap.json"),
        os.path.join(a, "boards", "board.json"),
        os.path.join(a, "_ai", "config.json")]
for p in keep:
    write(p, "{}")

gone = ui_server.drop_stray_tmps(a)
chk("A1 五个随机/固定名残件都收掉，返回值就是个数", gone == len(strays), gone)
chk("A2 盘上不再剩一个 .tmp", tmp_left(a) == [], tmp_left(a))
chk("A3 正经产物（meta / notes / board / mindmap / _ai 配置）一个不碰",
    all(os.path.isfile(p) for p in keep),
    [p for p in keep if not os.path.isfile(p)])


# ── B. 红线：链接 / 同名目录 / 不跟链接走出这本书 ──────────────────────
print("── B. 红线 ───────────────────────────────────────────")
b = new_book("ST_B")
outside = os.path.join(ROOT, "别人的东西.txt")
write(outside, "别人的东西")
made_link = False
try:
    os.symlink(outside, os.path.join(b, "evil.tmp"))
    made_link = True
except (OSError, NotImplementedError):
    made_link = False                       # 没有建链接的权限就跳过这一面
# 以 .tmp 收尾的目录：可能装着用户自己的东西，只认普通文件
os.makedirs(os.path.join(b, "keep.tmp"), exist_ok=True)
write(os.path.join(b, "keep.tmp", "inner.txt"), "用户的东西")
b_gone = ui_server.drop_stray_tmps(b)
chk("B1 同名目录 `keep.tmp` 不碰，里面那份还在",
    os.path.isdir(os.path.join(b, "keep.tmp"))
    and os.path.isfile(os.path.join(b, "keep.tmp", "inner.txt")))
chk("B2 目录里的普通文件不被算进删除数", b_gone == 0, b_gone)
if made_link:
    chk("B3 链接（名字以 .tmp 收尾）不删、指向的目标逐字不动",
        os.path.islink(os.path.join(b, "evil.tmp"))
        and os.path.isfile(outside), outside)

# 不跟符号链接目录走出这本书：链接那头也有 .tmp，不许被顺手带走
c = new_book("ST_C")
out_dir = os.path.join(ROOT, "别人的夹子")
write(os.path.join(out_dir, "别动.tmp"), "别人的半截件")
linked = False
try:
    os.symlink(out_dir, os.path.join(c, "linked"))
    linked = True
except (OSError, NotImplementedError):
    linked = False
chk("B4 os.walk 不跟链接目录，书外面的 .tmp 一个不少",
    (not linked) or os.path.isfile(os.path.join(out_dir, "别动.tmp")),
    os.listdir(out_dir) if os.path.isdir(out_dir) else out_dir)
chk("B5 书自己目录里没东西可收时返回 0",
    ui_server.drop_stray_tmps(c) == 0)


# ── C. 端到端：reset_book_output ─────────────────────────────────────
print("── C. reset_book_output 端到端 ───────────────────────")
d = new_book("ST_D", title="清稿书", done=True)
write(os.path.join(d, "meta.json.4242.1a2b3c4d.tmp"), "半截")
write(os.path.join(d, "boards", "board.json.3.feed0001.tmp"), "半截")
write(os.path.join(d, "_ai", "sum.json.5.abcd0001.tmp"), "半截")
write(os.path.join(d, "chapters", "001.md"), "# 第1章")
write(os.path.join(d, "_catalog.json"), "[]")

r = ui_server.reset_book_output("ST_D")
chk("C1 清稿有应答", isinstance(r, dict) and r.get("ok") is True, r)
chk("C2 随机名残件（含 boards / _ai 子目录）收干净了",
    tmp_left(d) == [], tmp_left(d))
chk("C3 老固定名与子目录残稿照旧清",
    not os.path.exists(os.path.join(d, "_catalog.json"))
    and not os.path.exists(os.path.join(d, "chapters")))
mp = json.load(open(os.path.join(d, "meta.json"), encoding="utf-8"))
chk("C4 书身份留着，done 打回假",
    mp.get("title") == "清稿书" and mp.get("author") == "某人"
    and mp.get("done") is False, mp)
chk("C5 用户笔记不受牵连",
    os.path.isfile(os.path.join(d, "notes.json"))
    and json.load(open(os.path.join(d, "notes.json"),
                       encoding="utf-8"))["marks"], "notes.json 丢了")
chk("C6 应答里报的删除数把随机名残件也算了进去", r.get("removed", 0) >= 4, r)


# ── D. 接线 ─────────────────────────────────────────────────────────
print("── D. 接线 ───────────────────────────────────────────")
src = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "ui_server.py"), encoding="utf-8").read()
body = src.split("def reset_book_output", 1)[-1].split("\ndef ", 1)[0]
chk("D1 清残稿真的调了 drop_stray_tmps", "drop_stray_tmps(" in body, body[:200])
chk("D2 那句调用在 drop_stray_md 之前（顺序与注释一致）",
    body.index("drop_stray_tmps(") < body.index("drop_stray_md("), body[:300])
chk("D3 老固定名那份 RESIDUE_FILES 没有被这次改动顺手删掉",
    "_catalog.json.tmp" in src and "_progress.json.tmp" in src
    and "_stuck.json.tmp" in src)
chk("D4 统一按 `.tmp` 尾巴收（常量只有一处真相）",
    'STRAY_TMP_SUFFIX = ".tmp"' in src
    and "STRAY_TMP_SUFFIX" in src.split("def drop_stray_tmps", 1)[-1].split("def ", 1)[0])

shutil.rmtree(ROOT, ignore_errors=True)

print()
if FAIL:
    print("✗ check_reset_stray_tmp：%d 条没过" % len(FAIL))
    for n in FAIL:
        print("   - " + n)
    sys.exit(1)
print("✓ check_reset_stray_tmp：%d 条全过" % PASSED)
