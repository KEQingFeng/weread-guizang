#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""转写时长与段落时间戳的「无穷值」收口：inf / NaN 不许变成 JSON 里的 Infinity。

故障长什么样
    `float()` 对四个坏输入**不抛异常**，回的全是非有限值：

        float("inf") == inf      float("-inf") == -inf
        float("nan") == nan      float("1e400") == inf
        float("9" * 400) == inf  float("Infinity") == inf

    时长那条路上有两个口子原来是裸 `float(...)`：

      · `ffmpeg_tool.probe_duration()` —— 它念的是 ffprobe 的 stdout。正常片源回
        `"123.45"`，但坏片源 / 认不出的容器 / 被手改过的探针回 `"inf"` / `"N/A"`
        之外还可能是 `"nan"` 与 `"1e400"`；`"N/A"` 会被 ValueError 接住，后三个不会。
      · `video_note._audio_seconds()` —— 把上面那个值原样交给 `offset`。`offset` 一旦
        是 inf，**后面每一段**的 start / end 都成了 inf（`inf + 10 == inf`）。
      · `video_note._pack_result()` —— 云 ASR 是外站 JSON，段落时间戳同样可能带
        `Infinity` / `NaN` / 负值 / 字符串；这里原来是裸 `float(...)`。

    这些非有限值最终写进 `transcript.json` 与分片缓存（`_write_json`，
    `ensure_ascii=False`）。Python 的 `json.dump` 默认 `allow_nan=True`，照写
    `"start": Infinity`；这不是 JSON 规范里的值，浏览器 `JSON.parse` 碰到整份抛
    —— 用户看到的是转写工作台那一屏整个空白，而不是「某一段的时间差一点」。

收口
    · `probe_duration`：`math.isfinite` 是第二条进门条件，非有限一律回 None。
    · `_audio_seconds`：走既有的 `_sec` 那把尺子（有限、正数，否则 0）。
    · `_transcribe_cloud` 组段那一句 `float(seg.get("start")) + offset` 也换成 `_sec`。
    · `_pack_result` 与 `_segments_excerpt`（喂模型的提示词那段 JSON）：start / end
      一并走 `_sec`，与 `_sec` 的注释里写的三条来源同一口径。

这一条钉四层：
    A 探针层：坏 stdout 一律回 None，正常时长一个字不改；
    B 偏移层：`_audio_seconds` 对 inf / NaN / 负数回 0，偏移链不再被污染；
    C 包层：`_pack_result` 收下的每段时间戳都是有限浮点，能被严格 JSON 写出；
    D 端到端：三片音频（一片正常、两片探针回坏值）走一遍 `_transcribe_cloud`，
      返回的段落、落在盘上的分片缓存、以及喂给模型的段落摘录都必须是严格 JSON。

全程离线：假 ffprobe 顶掉 subprocess，假 `_post_audio` 顶掉网络，不碰用户真实书库。
归 --fast 静态段。
"""
import json
import math
import os
import pathlib
import re
import shutil
import sys
import tempfile
import types

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 数据目录指进临时沙盒再 import video_note：免得它在用户真实 cache/ 里建目录。
SANDBOX = pathlib.Path(tempfile.mkdtemp(prefix="guizang-viddur-"))
os.environ["GUIZANG_DATA"] = str(SANDBOX / "cache")
os.environ["GUIZANG_BOOKS"] = str(SANDBOX / "books")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)

import ffmpeg_tool as ft  # noqa: E402
import video_note as vn  # noqa: E402

FAILED = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:220]))


def strict_json(text):
    """严格 JSON 解析：`Infinity` / `NaN` 这类 Python 放行、浏览器拒收的值当场判失败。"""
    def boom(const):
        raise ValueError("出现 JSON 规范不收的 " + const)
    return json.loads(text, parse_constant=boom)


# ── 假 ffprobe：按「第几个分片」回一份编好的 stdout ──────────────────
# 直接换掉 ffmpeg_tool 那个模块引用，不碰真正的 subprocess 模块（别的套件可能要用）。
_NEXT_STDOUT = {"value": ""}
_REAL_SUBPROCESS = ft.subprocess
_REAL_FFPROBE_PATH = ft.ffprobe_path


class _FakeProc(object):
    def __init__(self, stdout):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


def _fake_run(argv, **kwargs):
    path = argv[-1]
    table = _NEXT_STDOUT.get("by_path")
    if isinstance(table, dict) and path in table:
        return _FakeProc(table[path])
    return _FakeProc(_NEXT_STDOUT["value"])


ft.subprocess = types.SimpleNamespace(run=_fake_run)
ft.ffprobe_path = lambda: "/fake/ffprobe"


def probe(stdout):
    _NEXT_STDOUT.pop("by_path", None)
    _NEXT_STDOUT["value"] = stdout
    return ft.probe_duration("/fake/media.mp4")


try:
    # ── A 探针层：非有限一律 None，正常时长一字不改 ────────────────
    chk("A1 正常时长照认：'123.45' → 123.45", probe("123.45") == 123.45, probe("123.45"))
    chk("A2 带空白与换行也照认：'  60.5\\n' → 60.5", probe("  60.5\n") == 60.5, probe("  60.5\n"))
    chk("A3 零时长照认（0.0，不是 None）", probe("0") == 0.0, probe("0"))
    chk("A4 很大但有限的时长照认：'1e300'", probe("1e300") == 1e300, probe("1e300"))

    for raw in ("inf", "-inf", "Infinity", "-Infinity", "nan", "NaN", "1e400", "-1e400", "9" * 400):
        got = probe(raw)
        chk("A5 坏 stdout %r → None（不是 inf / nan / 天文数字）" % raw[:18],
            got is None, got)

    chk("A6 认不出的字面量（N/A / 空 / 字母）照旧 None",
        probe("N/A") is None and probe("") is None and probe("abc") is None,
        [probe("N/A"), probe(""), probe("abc")])

    # 对照：这些坏值确实穿得过裸 float() —— 断言不是空转。
    chk("A7 对照：裸 float('1e400') / float('nan') / float('inf') 都不抛且非有限",
        not math.isfinite(float("1e400"))
        and not math.isfinite(float("nan"))
        and not math.isfinite(float("inf")))

    # ── B 偏移层：_audio_seconds 对坏值回 0，好值不改 ───────────────
    for raw in ("inf", "nan", "1e400", "-5", "abc", ""):
        _NEXT_STDOUT.pop("by_path", None)
        _NEXT_STDOUT["value"] = raw
        got = vn._audio_seconds("/fake/media.mp4")
        chk("B1 探针回 %r 时 _audio_seconds() → 0.0" % raw[:12],
            got == 0.0 and math.isfinite(got), got)

    _NEXT_STDOUT["value"] = "12.5"
    chk("B2 正常时长一个字不改：12.5 → 12.5", vn._audio_seconds("/x.mp4") == 12.5,
        vn._audio_seconds("/x.mp4"))

    # 偏移链本身：inf 不许把后面的每一段都带成 inf。
    offsets = []
    off = 0.0
    for raw in ("10.0", "inf", "nan", "5.0"):
        _NEXT_STDOUT["value"] = raw
        off = off + vn._audio_seconds("/x.mp4")
        offsets.append(off)
    chk("B3 偏移链对坏片回 0：10 → 10 → 10 → 15（不是 inf）",
        offsets == [10.0, 10.0, 10.0, 15.0], offsets)
    chk("B4 链条末端仍是有限值", math.isfinite(offsets[-1]), offsets[-1])

    # ── C 包层：_pack_result 每段时间戳都有限 ───────────────────────
    dirty = {"segments": [
        {"start": float("inf"), "end": float("nan"), "text": "越界", },
        {"start": "1e400", "end": -5, "text": "字符串越界与负值"},
        {"start": "1.5", "end": "2.5", "text": "数字串照认"},
        {"start": None, "end": {"a": 1}, "text": "空与半个对象"},
        "这不是一段",                       # 裸字符串，跳过而不是抛
        None,                              # None，同上
        {"start": 0.0, "end": 48.0, "text": "好的一段"},
    ]}
    packed = vn._pack_result(dirty)
    segs = packed["segments"]
    chk("C1 脏条目一条不抛，认得出的段落全在（7 段）", len(segs) == 7, len(segs))
    bad = [(s["start"], s["end"]) for s in segs
           if not (isinstance(s["start"], float) and isinstance(s["end"], float)
                   and math.isfinite(s["start"]) and math.isfinite(s["end"])
                   and s["start"] >= 0 and s["end"] >= 0)]
    chk("C2 每段 start / end 都是有限的非负浮点", not bad, bad)
    chk("C3 好的一段一字不改：1.5 / 2.5 照认",
        segs[2]["start"] == 1.5 and segs[2]["end"] == 2.5 and segs[2]["text"] == "数字串照认",
        segs[2])
    chk("C4 0 / 48 那段原样保留",
        segs[-1]["start"] == 0.0 and segs[-1]["end"] == 48.0, segs[-1])
    chk("C5 非字典条目跳过、文字不丢",
        packed["text"] and "好的一段" in packed["text"], packed["text"])

    try:
        text = json.dumps(packed, ensure_ascii=False)
        strict_json(text)
        chk("C6 _pack_result 的结果能写成严格 JSON（无 Infinity / NaN）", True)
    except Exception as e:  # noqa: BLE001
        chk("C6 _pack_result 的结果能写成严格 JSON（无 Infinity / NaN）", False,
            "%s: %s" % (type(e).__name__, e))

    # 对照：旧写法（裸 float）确实写出 Infinity —— 防门禁空转。
    old = json.dumps({"start": float("1e400") + 10.0, "end": float("nan")})
    chk("C7 对照：旧写法 float('1e400') + 10 == inf，json.dumps 写出 Infinity / NaN",
        "Infinity" in old and "NaN" in old, old)

    # ── D 端到端：三片音频走一遍 _transcribe_cloud ──────────────────
    TMP = SANDBOX / "asr"
    TMP.mkdir(parents=True, exist_ok=True)
    audio = TMP / "clip.mp3"
    audio.write_bytes(b"x" * 16)            # 只有「大小」有用，内容是假的
    parts = [str(TMP / ("p%d.mp3" % i)) for i in (1, 2, 3)]
    durations = {parts[0]: "600.5", parts[1]: "inf", parts[2]: "nan"}

    saved_split = vn._silence_split
    saved_post = vn._post_audio
    saved_chunk = vn.CLOUD_CHUNK_BYTES
    vn._silence_split = lambda path, work_dir: list(parts)
    vn._post_audio = lambda endpoint, key, model, lang, part: {
        "text": "第 %s 段" % os.path.basename(part),
        "segments": [{"start": "inf", "end": "1e400", "text": "脏时间"},
                     {"start": 1.0, "end": 2.0, "text": "好时间"}]}
    vn.CLOUD_CHUNK_BYTES = 1               # 逼它走切段那条分支
    _NEXT_STDOUT.pop("value", None)
    _NEXT_STDOUT["by_path"] = durations
    try:
        out = vn._transcribe_cloud(str(audio),
                                   {"url": "https://example.test/v1", "key": "k"},
                                   "whisper-1", "zh", lambda *a, **k: None, None)
    finally:
        vn._silence_split = saved_split
        vn._post_audio = saved_post
        vn.CLOUD_CHUNK_BYTES = saved_chunk

    chk("D1 返回 3 段 × 2 条 = 6 段", len(out["segments"]) == 6, len(out["segments"]))
    bad = [(s["start"], s["end"]) for s in out["segments"]
           if not (math.isfinite(s["start"]) and math.isfinite(s["end"]))]
    chk("D2 端到端返回的每段时间戳都有限（inf 时长不污染偏移）", not bad, bad)
    chk("D3 认得出的时间照认：第一片好那段的 start 仍是 1.0（偏移 0）",
        out["segments"][1]["start"] == 1.0 and out["segments"][1]["end"] == 2.0,
        out["segments"][1])
    chk("D4 第二片的好那段挂上第一片 600.5 的偏移（1.0 → 601.5）",
        out["segments"][3]["start"] == 601.5 and out["segments"][3]["end"] == 602.5,
        out["segments"][3])
    try:
        strict_json(json.dumps(out, ensure_ascii=False))
        chk("D5 整份转写结果能写成严格 JSON", True)
    except Exception as e:  # noqa: BLE001
        chk("D5 整份转写结果能写成严格 JSON", False, "%s: %s" % (type(e).__name__, e))

    # 分片缓存（真实写法：_write_json(_part_cache_path(part), {..., segments})）也必须是严格 JSON。
    cache_ok, cache_bad = True, []
    for part in parts:
        fp = vn._part_cache_path(part)
        vn._write_json(fp, {"model": "whisper-1", "language": "zh", "text": "第 " + part,
                            "segments": vn._pack_result(
                                {"segments": [{"start": "inf", "end": "nan", "text": "脏"}]})["segments"],
                            "saved_at": 1})
        raw = pathlib.Path(fp).read_text(encoding="utf-8")
        try:
            strict_json(raw)
        except Exception as e:  # noqa: BLE001
            cache_ok = False
            cache_bad.append("%s: %s" % (os.path.basename(fp), e))
    chk("D6 盘上的分片缓存是严格 JSON（没有 Infinity / NaN）", cache_ok, cache_bad)

    # D7 段落摘录（喂给模型的提示词那段 JSON）同样不许带 Infinity / NaN。
    excerpt = vn._segments_excerpt([
        {"start": float("inf"), "text": "越界"},
        {"start": "1e400", "text": "字符串越界"},
        {"start": 12.34, "text": "好的一段"},
        "裸字符串",
    ])
    try:
        rows = strict_json(excerpt)
        chk("D7 段落摘录是严格 JSON（无 Infinity / NaN）", True)
    except Exception as e:  # noqa: BLE001
        rows = None
        chk("D7 段落摘录是严格 JSON（无 Infinity / NaN）", False, "%s：%r" % (e, excerpt[:160]))
    chk("D8 摘录里好的一段照认（12.34 → 12.3）、坏条目不带走整段",
        isinstance(rows, list) and len(rows) == 4
        and rows[2]["start"] == 12.3 and rows[2]["text"] == "好的一段",
        rows)

    # ── E 源码接线：三处都走同一把尺子 ─────────────────────────────
    SRC_FT = (REPO / "ffmpeg_tool.py").read_text(encoding="utf-8")
    SRC_VN = (REPO / "video_note.py").read_text(encoding="utf-8")

    chk("E1 probe_duration 里有 math.isfinite 的闸",
        "math.isfinite(seconds)" in SRC_FT)
    chk("E2 probe_duration 不再直接 return float(...)",
        not re.search(r"return float\(\(out\.stdout", SRC_FT),
        [l.strip() for l in SRC_FT.splitlines() if "return float((out.stdout" in l][:2])
    chk("E3 _audio_seconds 走 _sec 那把尺子",
        "_sec(ffmpeg_tool.probe_duration(path))" in SRC_VN)
    chk("E4 _pack_result 里不再有裸 float((item or {}).get(\"start\"))",
        'float((item or {}).get("start")' not in SRC_VN
        and 'float((item or {}).get("end")' not in SRC_VN)
    chk("E5 _pack_result 的 start / end 都过 _sec",
        '_sec(item.get("start"))' in SRC_VN and '_sec(item.get("end"))' in SRC_VN)
    chk("E6 云转写组段与段落摘录里不再有裸 float(seg.get(\"start\"))",
        'float(seg.get("start")' not in SRC_VN
        and 'float(seg.get("end")' not in SRC_VN
        and '_sec(seg.get("start"))' in SRC_VN)
    chk("E7 段落摘录的 start 也走 _sec",
        'round(_sec(seg.get("start")), 1)' in SRC_VN)

    # ── F 干净数据回归：既有那套纯函数一个口径不动 ──────────────────
    chk("F1 _sec 仍然是「有限且为正，否则 0」",
        vn._sec(11.0) == 11.0 and vn._sec(0) == 0.0 and vn._sec(-3) == 0.0
        and vn._sec(float("inf")) == 0.0 and vn._sec(float("nan")) == 0.0,
        [vn._sec(11.0), vn._sec(0), vn._sec(-3), vn._sec(float("inf"))])
    chk("F2 干净的 _pack_result 与原来逐字段相同",
        vn._pack_result({"segments": [{"start": 0.0, "end": 11.0, "text": "第一段"}],
                         "text": ""})["segments"]
        == [{"start": 0.0, "end": 11.0, "text": "第一段"}])
finally:
    ft.subprocess = _REAL_SUBPROCESS
    ft.ffprobe_path = _REAL_FFPROBE_PATH
    shutil.rmtree(SANDBOX, ignore_errors=True)

print()
if FAILED:
    print("转写时长越界值体检：通过 %d 项，失败 %d 项" % (PASSED, len(FAILED)))
    for name in FAILED:
        print("  ✗ " + name)
    sys.exit(1)
print("转写时长越界值体检：通过 %d 项，失败 0 项" % PASSED)
