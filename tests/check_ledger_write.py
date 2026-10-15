#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏门禁：全后端账本落盘那一处坏字符收口（ui_server._write_json）。

为什么单开一条
    `_write_json` 是书架账 / config.json / 阅读时长 / 阅读计划 / 划线索引这些小账本
    **唯一**的落盘出口（先写临时件再原子顶替）。账本不只本进程在写：用户会手工编辑、
    云同步会把别处那份落回来、旧版本也写过别的形状 —— 于是 config.json 的 `export_dir`
    里可能躺着一个落单的半个代理码点（U+D800 这一区，由 `"\\ud800"` 这种 JSON 转义
    读进来）。它 `json.dump(..., ensure_ascii=False)` 写不出去：utf-8 这一关当场抛
    UnicodeEncodeError。

实测的故障（改之前）
    `save_cfg({"export_dir": "坏" + HI + "路径"})`
      · 抛 UnicodeEncodeError 穿出 `/api/config` 的处理：点「保存」等不到回包，
        BaseHTTPRequestHandler 只是把连接关掉（前端看见 RemoteDisconnected）；
      · 账本旁边留下一个 `config.json.<pid>.<rand>.tmp`，没人认领。

收口
    与响应那一侧的 `_encode_body` 同一把尺子：干净的路照旧 dump 一次；真编不动时才按
    `book_layout.drop_lone_surrogates` 摘掉落单的坏码点重编（成对的代理码点在
    `json.load` 那一步早合成真字符，只有落单的才是垃圾）。任何写失败都把 `.tmp` 收干净
    再抛，旧档不受影响 —— 与 mindmap / board 那两个写口同一规矩。

这一条把六面一起钉住：写得出、读得回、坏码点真被摘掉、干净内容一个字不改、失败不留
垃圾且旧档完好、以及「旧写法确实会抛」的对照（免得门禁空转）。

跑法：纯函数 + 系统临时目录沙盒，不联网、不起服务、不开浏览器，归 --fast 静态段。
"""
import atexit
import json
import os
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

# 自带临时沙盒：绝不碰用户真实的 cache/，也不和别的套件共用那一份。
SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-ledger-write-"))
atexit.register(lambda: shutil.rmtree(SB, ignore_errors=True))
os.environ["GUIZANG_DATA"] = str(SB / "data")
os.environ["GUIZANG_BOOKS"] = str(SB / "books")
os.environ["GUIZANG_SHOT_DIR"] = str(SB / "shots")

import ui_server as U  # noqa: E402 —— 必须在环境变量设好之后 import

# 落单的半个代理码点：chr() 现造，免得源码里出现字面上的半个码点（文件本身会读不动）
HI = chr(0xD800)
LO = chr(0xDFFF)

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def tmp_left(path):
    folder = os.path.dirname(path)
    base = os.path.basename(path)
    return [n for n in os.listdir(folder) if n.startswith(base) and n.endswith(".tmp")]


# ── 1. 直接打 _write_json：脏值写得出、坏码点摘掉、干净字不改 ──────────
D = str(SB / "case1")
P = os.path.join(D, "ledger.json")
obj = {"title": "坏" + HI + "字符" + LO + "收尾", "n": 3,
       "items": [{"t": "好"}, {"t": HI + "开头"}]}
U._write_json(P, obj)                      # 改之前：这里抛 UnicodeEncodeError
got = read_json(P)
chk("A1 落单代理码点：不再抛，账本写得出、解得动", isinstance(got, dict), got)
chk("A2 坏码点真被摘掉、正常字一个不改",
    got.get("title") == "坏字符收尾" and got.get("n") == 3,
    got.get("title"))
chk("A3 嵌套列表里的坏码点也摘（元素不丢）",
    got.get("items") == [{"t": "好"}, {"t": "开头"}], got.get("items"))
chk("A4 落单代理码点全清（读回的字里一个都不剩）",
    not any("\ud800" <= c <= "\udfff" for c in json.dumps(got, ensure_ascii=False)))

# 成对的表情符号是正经字符，不许被顺手摘掉。
P2 = os.path.join(D, "emoji.json")
U._write_json(P2, {"t": "图😀好", "k": "café"})
chk("A5 成对 emoji 与重音字符一个字不改",
    read_json(P2) == {"t": "图😀好", "k": "café"}, read_json(P2))

# 干净的路必须与老格式逐字节一致：不能因为这一改就换排版 / 多留空行。
P3 = os.path.join(D, "clean.json")
clean = {"b": 2, "a": [1, "x", {"y": None}]}
U._write_json(P3, clean)
with open(P3, encoding="utf-8") as f:
    raw = f.read()
chk("A6 干净账本仍是老格式（ensure_ascii=False + indent=2，逐字节一致）",
    raw == json.dumps(clean, ensure_ascii=False, indent=2), repr(raw[:60]))

# 整份就是一个坏字符串时也要落得下（重编后成空串，而不是抛）。
P4 = os.path.join(D, "onlybad.json")
U._write_json(P4, HI)
chk("A7 整份都是坏码点也写得下（编成空串，不抛）", read_json(P4) == "", read_json(P4))

# ── 2. 写失败：不留 .tmp，旧档一个字不动 ─────────────────────────────
D2 = str(SB / "case2")
P5 = os.path.join(D2, "keep.json")
U._write_json(P5, {"good": "旧的一份"})
before = open(P5, "rb").read()
raised = False
try:
    U._write_json(P5, {"bad": {1, 2}})     # set 不是 JSON 类型，必然写不进去
except TypeError:
    raised = True
except Exception:                          # noqa: BLE001 —— 别的异常也算「没接住这一档」
    raised = False
chk("B1 真写不进去时照旧抛（不吞异常）", raised)
chk("B2 写崩后 .tmp 收干净了", tmp_left(P5) == [], tmp_left(P5))
chk("B3 写崩不毁旧档（旧的一份逐字节还在）", open(P5, "rb").read() == before)

# 崩过一次之后，同一个口子还能正常干活。
U._write_json(P5, {"good": "新的一份"})
chk("B4 写崩之后同一个口子照旧可用", read_json(P5) == {"good": "新的一份"})

# 缺目录也要自己建出来（原来那句 os.makedirs 不能被这层 try 吃掉）。
P6 = os.path.join(D2, "deep", "nested", "x.json")
U._write_json(P6, {"ok": 1})
chk("B5 深层目录仍自动创建", read_json(P6) == {"ok": 1})

# ── 3. 真接口那一趟：save_cfg → load_cfg / export_dir 全链 ──────────
D3 = str(SB / "case3")
OLD_CFG = U.CFG_PATH
U.CFG_PATH = os.path.join(D3, "config.json")
try:
    U.save_cfg({"weread_key": "wrk-abc123", "export_dir": "坏" + HI + "路径"})
    cfg = U.load_cfg()
    chk("C1 save_cfg 不再抛、config.json 落得下（点保存有得回）",
        cfg.get("weread_key") == "wrk-abc123", cfg)
    chk("C2 读回时坏码点已被摘掉", cfg.get("export_dir") == "坏路径", cfg.get("export_dir"))
    chk("C3 export_dir() 这条读取链拿到的是干净路径",
        U.export_dir() == "坏路径", U.export_dir())
    chk("C4 落盘之后不留 .tmp", tmp_left(U.CFG_PATH) == [], tmp_left(U.CFG_PATH))

    # 对照：同一份对象交给「旧写法」（裸 json.dump）确实抛 —— 防门禁空转。
    legacy_raised = False
    try:
        with open(os.path.join(D3, "legacy.json"), "w", encoding="utf-8") as f:
            json.dump({"export_dir": "坏" + HI + "路径"}, f, ensure_ascii=False, indent=2)
    except UnicodeEncodeError:
        legacy_raised = True
    chk("C5 对照：旧写法（裸 json.dump）拿同一份确实抛 UnicodeEncodeError", legacy_raised)
finally:
    U.CFG_PATH = OLD_CFG

# ── 4. 接线：收口长在唯一那个写口里 ──────────────────────────────────
src = (REPO / "ui_server.py").read_text(encoding="utf-8")
body = src.split("def _write_json(", 1)[1].split("\ndef ", 1)[0]
chk("D1 回退用书库那把同一尺子（drop_lone_surrogates）",
    "book_layout.drop_lone_surrogates" in body, body[:200])
chk("D2 写失败收 .tmp 的逻辑在这个写口里", "os.remove(tmp)" in body)
chk("D3 save_cfg 仍走唯一的 _write_json", "_write_json(CFG_PATH, d, 0o600)" in src)

print()
print("账本写口坏字符体检：%d 项通过，失败 %d 项" % (PASSED, len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
