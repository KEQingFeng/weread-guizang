#!/usr/bin/env python3
"""归藏门禁：视频转笔记里「正文归哪一章 / 哪个字段归哪一段」两条归属规矩（video_note.py）。

不起服务、不联网、不开浏览器：切章与保存转写都是纯函数加临时目录里的一本假书，
直接对着函数打就够了。

它守的是两句话：

    · **正文不许全堆进最后一章。** 章节起点是 LLM 给的；模型漏给 start 时清洗层会把
      每一章都填成 0，而旧代码「取最后一个 start <= 段 start 的章」在这种输入下永远
      落到最后一章 —— 前几章只剩标题、正文看着像丢了。起点不可用时按字数均分，
      内容一个字都不能少。
    · **新插入的段落不许继承旧段落的人声。** transcript 是整本覆盖着存的，旧段落的
      speaker / confidence 只在 id 对得上时才算数；编辑器新插一段没有旧 id，却被按
      位置认领了别人的说话人，就等于给这句话栽了赃。
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import video_note as vn  # noqa: E402

FAILED = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))


def plain(s):
    """去掉空白后的正文，用来验「内容一个字没少」。"""
    return "".join(str(s or "").split())


def bodies_of(segs, chapters):
    return [body for _title, body in vn._chapter_bodies("", segs, chapters)]


SEGS = [{"start": 0, "text": "甲甲甲甲"}, {"start": 10, "text": "乙乙乙乙"},
        {"start": 20, "text": "丙丙丙丙"}]
INC = [{"title": "一", "start": 0}, {"title": "二", "start": 10}, {"title": "三", "start": 20}]
ZERO = [{"title": "一", "start": 0}, {"title": "二", "start": 0}, {"title": "三", "start": 0}]

# ── 1. 章节起点严格递增：照时间切（这条是原口径，不能被顺手改掉）─────────
out = bodies_of(SEGS, INC)
chk("递增起点：三段各归各章",
    out == ["甲甲甲甲", "乙乙乙乙", "丙丙丙丙"], out)

# ── 2. 起点全为 0（LLM 漏给 start）：不许全堆进最后一章 ────────────────
out = bodies_of(SEGS, ZERO)
chk("起点全 0：正文铺到每一章，不留空标题",
    all(b for b in out), out)
chk("起点全 0：内容一个字没少",
    plain("".join(out)) == plain("".join(s["text"] for s in SEGS)),
    (plain("".join(out)), plain("".join(s["text"] for s in SEGS))))
chk("起点全 0：不是「前两章空、全在末章」那个老样子",
    out[0] and out[1] and not (out[0] == "" and out[1] == ""), out)

# ── 3. 起点重复 / 倒挂：同样退回均分，内容不丢 ─────────────────────────
for label, chapters in [
        ("重复起点", [{"title": "一", "start": 0}, {"title": "二", "start": 0},
                      {"title": "三", "start": 30}]),
        ("起点乱序", [{"title": "一", "start": 30}, {"title": "二", "start": 0},
                      {"title": "三", "start": 10}])]:
    out = bodies_of(SEGS, chapters)
    chk("起点不可用（%s）：正文铺到首尾章，不留空标题" % label,
        out[0] and out[-1], out)
    chk("起点不可用（%s）：内容一个字没少" % label,
        plain("".join(out)) == plain("".join(s["text"] for s in SEGS)), out)

# ── 4. 脏起点值不能把切章带崩 ───────────────────────────────────────────
out = vn._chapter_bodies("", [{"start": "oops", "text": "一句"}], ZERO)
chk("脏起点（字符串）：不抛异常，内容仍落在第一章",
    plain("".join(b for _t, b in out)) == "一句", out)

# ── 5. 单章 / 没章节：兜底不丢内容 ─────────────────────────────────────
out = bodies_of(SEGS, [{"title": "只有一章", "start": 0}])
chk("单章：整篇都收进去", plain("".join(out)) == plain("".join(s["text"] for s in SEGS)), out)
out = vn._chapter_bodies("整段转写", [], [])
chk("没章节：回「全文转写」一段", out == [("全文转写", "整段转写")], out)

# ── 6. save_transcript：新段的字段归属 ─────────────────────────────────
SANDBOX = tempfile.mkdtemp(prefix="guizang-chapter-")
try:
    book = os.path.join(SANDBOX, "book")
    os.makedirs(book)
    with open(os.path.join(book, "transcript.json"), "w", encoding="utf-8") as f:
        json.dump({"schema": 1, "segments": [
            {"start": 0.0, "end": 5.0, "text": "旧 A", "id": "a",
             "speaker": "甲", "confidence": 0.9},
            {"start": 5.0, "end": 9.0, "text": "旧 B", "id": "b", "speaker": "乙"},
        ]}, f, ensure_ascii=False)

    vn.save_transcript(book, [
        {"id": "new", "start": 0.0, "end": 1.0, "text": "新插进来的一句"},
        {"id": "a", "start": 5.0, "end": 9.0, "text": "改过的 A"},
    ])
    with open(os.path.join(book, "transcript.json"), encoding="utf-8") as f:
        saved = json.load(f)["segments"]
    by_id = {s.get("id"): s for s in saved}
    chk("保存：带新 id 的插入段不继承旧段的人声 / 置信度",
        "speaker" not in by_id["new"] and "confidence" not in by_id["new"], by_id["new"])
    chk("保存：id 对得上的老段照旧认回时间戳与说话人",
        by_id["a"].get("speaker") == "甲" and by_id["a"].get("confidence") == 0.9
        and by_id["a"].get("text") == "改过的 A", by_id["a"])
    chk("保存：段落顺序就是传进来的顺序",
        [s.get("id") for s in saved] == ["new", "a"], [s.get("id") for s in saved])

    # 更老的前端不带 id：位置对齐这条退路必须还在
    legacy = os.path.join(SANDBOX, "legacy")
    os.makedirs(legacy)
    with open(os.path.join(legacy, "transcript.json"), "w", encoding="utf-8") as f:
        json.dump({"schema": 1, "segments": [
            {"start": 0.0, "end": 5.0, "text": "旧", "id": "a", "speaker": "甲"}]},
            f, ensure_ascii=False)
    vn.save_transcript(legacy, [{"start": 0.0, "end": 5.0, "text": "没带 id 的一段"}])
    with open(os.path.join(legacy, "transcript.json"), encoding="utf-8") as f:
        leg = json.load(f)["segments"]
    chk("保存：没带 id 的老前端仍按位置认回字段（退路没被顺手砍掉）",
        leg[0].get("speaker") == "甲" and leg[0].get("text") == "没带 id 的一段", leg)
finally:
    shutil.rmtree(SANDBOX, ignore_errors=True)

print()
if FAILED:
    print("视频转笔记归属体检：失败 %d 项" % len(FAILED))
    for name in FAILED:
        print("  ✗ " + name)
    sys.exit(1)
print("视频转笔记归属体检：%d 项通过，失败 0 项" % PASSED)
