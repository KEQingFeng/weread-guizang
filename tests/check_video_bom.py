#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频转写这条线的读口认 BOM：记事本另存的 UTF-8 不该被当成坏档。

为什么单开一条
    视频这一格有两份**用户会拿记事本直接改**的登记档：`transcript.json`（转写真相，
    带时间戳与用户改过的字）与 `meta.json`（书名 / 时长），外加应用自己的
    `cache/config.json`（AI 三件套）。Windows 记事本另存 UTF-8 默认带 BOM，
    而 `video_note._read_json` 与 `_agent_cfg_from_disk` 原先用的是裸 `utf-8`：

        json.load(open(path, encoding="utf-8"))   # BOM 那三字节 -> JSONDecodeError

    异常被宽 `except` 收成 None / 空串，于是两种症状都出现了：

      · `load_transcript` 认不出 `transcript.json`，悄悄退回旧的 `transcript.txt`
        （时间戳没了、用户改的字没了，界面看着像「转写回到上一版」）——没有 txt 时
        干脆报「这本书里没有转写文件」，文件明明在；
      · `save_transcript` 把整份当成空档重写，`by_id` 里按 id 留着的说话人 /
        置信度全体丢失 —— 正好违反这条接口自己写下的「认不出的字段原样留着」；
      · `_agent_cfg_from_disk` 回空，视频那一栏一直说「AI 没配」，配置明明填着。

    项目里其余读口（feed / board / book_notes / mindmap / 写作册账 / config 主读口）
    早就是 `utf-8-sig`，只有视频这几处漏了。这一条把它们钉住。

这一条钉住
  B1 对照：裸 `utf-8` 读带 BOM 的 JSON 当场失败（洞是真的，不是理论上的）；
  B2 `_read_json` 读 BOM 档与读无 BOM 档同值；缺文件 / 半截档仍回 None、不抛；
  B3 `_read_meta` 读带 BOM 的 `meta.json` 保得住书名；
  B4 `load_transcript` 对带 BOM 的 `transcript.json` 走 `source="json"`，
     时间戳 / 说话人 / 用户改过的字都在，不被旧 txt 顶掉；
  B5 `save_transcript` 在 BOM 档上仍按 id 留住认不出的字段（不静默丢元数据）；
  B6 `_agent_cfg_from_disk` 读带 BOM 的 `config.json` 回得到三件套，`available()`
     据此把 llm 报成可用；
  B7 无 BOM 的档一个字不改（加固不许把好数据洗坏），存完不留 `.tmp`。

全程离线、不开浏览器、不碰用户真实书库：`GUIZANG_DATA` / `GUIZANG_BOOKS` 在 import
`video_note` 之前就指进系统临时目录（`TMP_ROOT` 也是那一刻算出来的）。
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SANDBOX = pathlib.Path(tempfile.mkdtemp(prefix="guizang-vidbom-"))
os.environ["GUIZANG_DATA"] = str(SANDBOX / "cache")
os.environ["GUIZANG_BOOKS"] = str(SANDBOX / "books")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)
os.makedirs(os.environ["GUIZANG_BOOKS"], exist_ok=True)

import video_note as vn  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("  ok  " if cond else " FAIL ") + name + ("" if cond else "  <- " + repr(extra)))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def write_json(path, obj, bom=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig" if bom else "utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


book = str(SANDBOX / "book_bom")
os.makedirs(book, exist_ok=True)

SEG = {"start": 0.0, "end": 1.5, "text": "用户改过的字", "id": "s00000",
       "speaker": "甲", "confidence": 0.87}

# ── B1. 对照：洞是真的 ────────────────────────────────────────────
bom_path = os.path.join(book, "probe.json")
write_json(bom_path, {"x": 1}, bom=True)
chk("对照：写出来的档确实带 UTF-8 BOM", read_bytes(bom_path).startswith(b"\xef\xbb\xbf"))
plain_ok = True
try:
    with open(bom_path, encoding="utf-8") as f:
        json.load(f)
except Exception:
    plain_ok = False
chk("对照：裸 utf-8 读带 BOM 的 JSON 当场失败（收口前就是这条路）", plain_ok is False)

# ── B2. _read_json 一把尺子 ──────────────────────────────────────
write_json(os.path.join(book, "clean.json"), {"a": 1})
write_json(os.path.join(book, "bom.json"), {"a": 1}, bom=True)
chk("_read_json：无 BOM 照常读",
    vn._read_json(os.path.join(book, "clean.json")) == {"a": 1})
chk("_read_json：带 BOM 读出来同值（不再回 None）",
    vn._read_json(os.path.join(book, "bom.json")) == {"a": 1})
chk("_read_json：缺文件仍是 None，不抛",
    vn._read_json(os.path.join(book, "nope.json")) is None)
half = os.path.join(book, "half.json")
with open(half, "w", encoding="utf-8") as f:
    f.write('{"a": ')
chk("_read_json：半截档仍是 None，不抛", vn._read_json(half) is None)

# ── B3. _read_meta 认 BOM 的 meta.json ───────────────────────────
meta_book = str(SANDBOX / "book_meta")
write_json(os.path.join(meta_book, "meta.json"),
           {"title": "带 BOM 的视频书", "duration": 12}, bom=True)
meta = vn._read_meta(meta_book)
chk("_read_meta：带 BOM 的 meta.json 保得住书名",
    meta.get("title") == "带 BOM 的视频书", meta)

# ── B4. load_transcript 认 BOM 的 transcript.json ────────────────
write_json(os.path.join(book, vn.TRANS_FILE),
           {"schema": vn.TRANS_SCHEMA, "segments": [SEG], "edited": True}, bom=True)
# 故意放一份过时的 txt：json 才是真相，读口不许退回它。
with open(os.path.join(book, vn.TRANS_TXT), "w", encoding="utf-8") as f:
    f.write("过时的纯文本\n")
loaded = vn.load_transcript(book)
chk("load_transcript：带 BOM 也走 source=json（不退回旧 txt）",
    loaded.get("source") == "json", loaded.get("source"))
chk("load_transcript：用户改过的字还在",
    loaded["segments"][0]["text"] == "用户改过的字", loaded["segments"][:1])
chk("load_transcript：时间戳还在（没退化成无时间的 txt）",
    loaded["segments"][0].get("start") == 0.0 and loaded.get("timed") is True,
    loaded["segments"][:1])
chk("load_transcript：说话人还在", loaded["segments"][0].get("speaker") == "甲",
    loaded["segments"][:1])

# ── B5. save_transcript 在 BOM 档上不丢认不出的字段 ──────────────
# 编辑器存回来时只带 id / 时间 / 正文：说话人与自定义字段得靠旧档 by_id 认领，
# 旧档读不出来就一起没了 —— 这一条才是 BOM 真正会造成的静默丢数据。
res = vn.save_transcript(book, [{"id": "s00000", "start": 0.0, "end": 1.5,
                                 "text": "又改了一次"}])
chk("save_transcript：返回段数对得上", res.get("segments") == 1, res)
after = vn.load_transcript(book)
seg0 = after["segments"][0]
chk("save_transcript：改后的字落盘了", seg0["text"] == "又改了一次", seg0)
chk("save_transcript：按 id 留住的说话人没丢（BOM 档不当空档重写）",
    seg0.get("speaker") == "甲", seg0)
chk("save_transcript：认不出的自定义字段也没丢",
    seg0.get("confidence") == 0.87, seg0)
chk("save_transcript：存完不留 .tmp",
    not os.path.exists(os.path.join(book, vn.TRANS_FILE + ".tmp")))
# 落盘那一份仍是干净的（无 BOM）—— 我们只在读口宽容，写口不主动种 BOM。
chk("save_transcript：落盘仍是无 BOM 的干净 utf-8",
    not read_bytes(os.path.join(book, vn.TRANS_FILE)).startswith(b"\xef\xbb\xbf"))

# ── B6. _agent_cfg_from_disk 认 BOM 的 config.json ───────────────
cfg_dir = SANDBOX / "cache"
write_json(str(cfg_dir / "config.json"),
           {"agent_url": "http://ai.example/v1", "agent_key": "sk-x",
            "agent_model": "m-1"}, bom=True)
saved_data = vn.DATA_DIR
vn.DATA_DIR = str(SANDBOX)
try:
    got = vn._agent_cfg_from_disk()
    chk("_agent_cfg_from_disk：带 BOM 的 config.json 读得到三件套",
        got == ("http://ai.example/v1", "sk-x", "m-1"), got)
    chk("available()：据此把 llm 报成可用",
        vn.available().get("llm") is True, vn.available().get("llm"))
finally:
    vn.DATA_DIR = saved_data

# ── B7. 无 BOM 的档一个字不改 ────────────────────────────────────
clean_book = str(SANDBOX / "book_clean")
write_json(os.path.join(clean_book, vn.TRANS_FILE),
           {"schema": vn.TRANS_SCHEMA, "segments": [
               dict(SEG, text="原样的字")]})
before = read_bytes(os.path.join(clean_book, vn.TRANS_FILE))
chk("无 BOM：读出来与写下去的段一致",
    vn.load_transcript(clean_book)["segments"][0]["text"] == "原样的字")
chk("无 BOM：读一次不改盘上一个字节",
    read_bytes(os.path.join(clean_book, vn.TRANS_FILE)) == before)

shutil.rmtree(str(SANDBOX), ignore_errors=True)
print()
print("视频转写读口认 BOM：%d 项通过，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    print("未过：" + " / ".join(FAIL))
sys.exit(1 if FAIL else 0)
