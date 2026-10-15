#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 小结的坏字符（ai_sum.py）：模型回的半个代理码点不许让小结**悄悄存不下去**。

模型那一边回来的正文是外站 JSON：JS / 上游把字符串拦腰截断时会留下一个落单的代理
码点（U+D800–U+DFFF 不成对），`json.loads` 收得下（形如 "\\ud83d" 那个转义），于是
它一路走到 `save_result` 落盘那一步 —— `_ai/learn-whole.md` 按 utf-8 写，当场
UnicodeEncodeError。而 `save_result` 的规矩是「写不动也不抛，回空串」，于是症状是：

  · 用户屏幕上那段小结好好的（响应编码那道口子会顺手把坏字符摘掉），
  · 重开这一页却什么都没有 —— 落进 `_ai/` 的一个字都没有，
  · 流式口那边还写着「这次没能落档」，用户不知道是模型的问题还是自己的问题。

这一份专补「脏字符写不进 utf-8」这一类，与 feed / flomo / writer / mindmap 的存盘
收尾同一个口径。只碰 ai_sum 的纯函数与系统临时目录里的沙盒书库，不起服务、不联网、
不碰真实书库，所以归静态段，--fast 也要带。

钉四件事：
  · 落单的代理码点被摘掉（高位 / 低位 / 只在中间 / 只剩它），其余字符一个字不改；
  · 真 emoji（成对代理码点解码出的）与 ZWJ 连字（🧑‍💻）一个字不动；
  · 洗过的正文真能按 utf-8 落盘、读回来还是干净的，回执也编得出去；
  · 落盘这道口子确实排在写之前（旧写法直接写会抛 UnicodeEncodeError 作对照）。
"""
import inspect
import json
import os
import pathlib
import shutil
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 数据目录指进临时沙盒再 import：免得 ai_sum 之外的路径真去动用户真实书库。
SANDBOX = pathlib.Path(tempfile.mkdtemp(prefix="guizang-ai-sum-surrogate-"))
os.environ["GUIZANG_DATA"] = str(SANDBOX / "cache")
os.environ["GUIZANG_BOOKS"] = str(SANDBOX / "books")

import ai_sum  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


HI = "\ud83d"      # 落单的高位代理码点
LO = "\ude00"      # 落单的低位代理码点
EMOJI = json.loads(r'"\ud83d\ude00"')       # 成对：json 解码时合成真字符 😀
ZWJ = "\U0001f9d1\u200d\U0001f4bb"          # 真 ZWJ 连字 🧑‍💻，半个都不许动


def json_encodable(obj):
    """回执那一路是 json.dumps(..., ensure_ascii=False) 再编码，脏字符在这里就会拦下。"""
    try:
        json.dumps(obj, ensure_ascii=False).encode("utf-8")
        return True
    except (UnicodeEncodeError, TypeError, ValueError):
        return False


BOOK = str(SANDBOX / "book")

# ── 一、纯函数：只摘落单的，不动成对的 ───────────────────────────
chk("只剩一个高位代理码点 → 摘成空串", ai_sum.drop_lone_surrogates(HI) == "")
chk("只剩一个低位代理码点 → 摘成空串", ai_sum.drop_lone_surrogates(LO) == "")
chk("夹在正文中间的半个码点被摘掉、两边一个字不改",
    ai_sum.drop_lone_surrogates("前" + HI + "中" + LO + "后") == "前中后",
    ai_sum.drop_lone_surrogates("前" + HI + "中" + LO + "后"))
chk("真 emoji（成对）一个字不动", ai_sum.drop_lone_surrogates("看" + EMOJI + "图") == "看" + EMOJI + "图",
    ai_sum.drop_lone_surrogates("看" + EMOJI + "图"))
chk("ZWJ 连字（🧑‍💻）不被拆开", ai_sum.drop_lone_surrogates(ZWJ) == ZWJ,
    ai_sum.drop_lone_surrogates(ZWJ))
chk("非字符串按老口径 str() 后再洗，不抛",
    ai_sum.drop_lone_surrogates(None) == "" and ai_sum.drop_lone_surrogates(123) == "123")

# ── 二、落盘这道口子：坏字符不再让整份小结存不下去 ───────────────
DIRTY = "小结开头" + HI + "正文" + LO + "结尾"
p = ai_sum.save_result(BOOK, "whole", "learn", "", DIRTY)
chk("save_result 带半个代理码点也回真路径（原来是回空串）", bool(p), p)
chk("文件真的落下来了", bool(p) and os.path.isfile(p), p)
chk("load_result 读回来是洗干净的正文", ai_sum.load_result(BOOK, "whole", "learn", "") == "小结开头正文结尾",
    ai_sum.load_result(BOOK, "whole", "learn", ""))
if p and os.path.isfile(p):
    on_disk = pathlib.Path(p).read_text(encoding="utf-8")
    chk("盘上那份本身是干净、可编码的", on_disk == "小结开头正文结尾", on_disk)
    chk("回执（路径 / 落定正文）能按 utf-8 编出去", json_encodable({"path": p, "text": on_disk}))

# 干净正文一个字不改（落盘这一道不是「顺手改写」）
CLEAN = "正常小结：一、二、三。\n\n- 要点 " + EMOJI + " " + ZWJ
p2 = ai_sum.save_result(BOOK, "chapter", "brief", "0002.md", CLEAN)
chk("干净正文（含 emoji / ZWJ）原样落盘、原样读回",
    bool(p2) and ai_sum.load_result(BOOK, "chapter", "brief", "0002.md") == CLEAN,
    ai_sum.load_result(BOOK, "chapter", "brief", "0002.md"))

# 目录不存在时先建出来：坏字符的修复不许把「建目录」这条老路弄坏
p3 = ai_sum.save_result(str(SANDBOX / "made" / "up" / "book"), "whole", "brief", "", "x")
chk("书目录不存在时仍会先建目录再落盘", bool(p3) and os.path.isfile(p3), p3)

# ── 三、对照组：旧写法直接写确实会抛（证明这道收口不是空转）────────
bad_path = SANDBOX / "old-way.md"
threw = False
try:
    with open(str(bad_path), "w", encoding="utf-8") as f:
        f.write(DIRTY)          # 旧 save_result 的原样：先写后吞异常
except UnicodeEncodeError:
    threw = True
chk("对照：不清就写当场 UnicodeEncodeError", threw)

# ── 四、接线：清洗排在 open 之前，不是事后补救 ───────────────────
src = inspect.getsource(ai_sum.save_result)
chk("save_result 先把正文洗完再写（旧写法是 f.write(text) 原样）",
    "drop_lone_surrogates(text)" in src and "f.write(text)" in src
    and src.index("drop_lone_surrogates(text)") < src.index("f.write(text)"),
    src)

shutil.rmtree(SANDBOX, ignore_errors=True)

print("\nAI 小结坏字符体检：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
for name in FAIL:
    print("  x " + name)
sys.exit(1 if FAIL else 0)
