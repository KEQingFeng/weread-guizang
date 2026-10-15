# -*- coding: utf-8 -*-
"""视频转写这条线落单的半个代理码点（U+D800–U+DFFF）的离线验收。

半个代理码点不是一个「奇怪的字符」，是一个 utf-8 编不出来的坏值：JSON 里写
`"\\ud800"` 能被 json.load 收成这个码点，可 `.encode("utf-8")` 与
`json.dump(..., ensure_ascii=False)` 碰到它当场 UnicodeEncodeError。转写这一格
有两条路会撞上它，两条都是真实可复现的：

  A 云端 ASR 的回包 / 编辑器存回来的段落里带着它（浏览器把一段 emoji 拦腰截断、
    再 JSON.stringify 就是 `\\ud83d`）。`save_transcript` 走到 `_write_json` 的
    ensure_ascii=False 上抛 UnicodeEncodeError —— 界面是「点了保存没反应」，
    书目录里还留一个半截 transcript.json.tmp。

  B transcript.json 被手改 / 云同步落下 / 旧版本写过，值里（或自由字段的**键**里）
    留着它。之后读出来（load_transcript）再存一次、导出或重建，同样炸在编码上。

  C 整条转笔记落成一本书的 `_save_book`：云转写与 LLM 都是「别人家的 JSON」，
    分片缓存 `_write_json`、成品正文 `body_md.encode("utf-8")`、`transcript.txt` /
    `merged.md` 的 `_write_text` 三处撞同一刀。外站元信息（标题 / 上传者）也会脏。

这一份把三条路连同四条底线钉住：

  E1 只摘落单的那半个：真 emoji（😀 / 👍🏽）与 ZWJ 连字（🧑‍💻）一个字不动，洗完是幂等的。
  E2 干净的转写一个字不改：加固不许把好数据洗坏。
  E3 落盘后不留 .tmp，json 与跟着写的 transcript.txt 是同一份干净数据。
  E4 `_save_book` 整条喂脏数据不抛，落出来的每一份文件都不含落单码点。

全程离线、不联网、不开浏览器、不写用户真实书库（全部在系统临时目录里）。
"""
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 数据目录指进临时沙盒再 import video_note：免得 TMP_ROOT 真去动用户真实缓存。
SANDBOX = pathlib.Path(tempfile.mkdtemp(prefix="guizang-vidsur-"))
os.environ["GUIZANG_DATA"] = str(SANDBOX / "cache")
os.environ["GUIZANG_BOOKS"] = str(SANDBOX / "books")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)

import video_note as vn  # noqa: E402

FAILED = []
PASSED = 0
HI = "\ud800"                       # 落单的高位代理码点
LO = "\udfff"                       # 落单的低位代理码点
HALF = "\ud83d"                     # 一个 emoji 被拦腰截断后剩下的那半个
EMOJI = "\U0001F600"                # 😀 真字符，必须原样留着
TONE = "\U0001F44D\U0001F3FD"       # 👍🏽 肤色修饰符，必须原样留着
ZWJ = "\U0001F9D1\u200D\U0001F4BB"  # 🧑‍💻 ZWJ 连字，必须原样留着
SURROGATE = re.compile("[\ud800-\udfff]")


def chk(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:220]))


def has_surrogate(v):
    """整棵树（含字典键）里还有没有落单的代理码点。"""
    if isinstance(v, str):
        return any(0xD800 <= ord(c) <= 0xDFFF for c in v)
    if isinstance(v, dict):
        return any(has_surrogate(k) or has_surrogate(x) for k, x in v.items())
    if isinstance(v, (list, tuple)):
        return any(has_surrogate(x) for x in v)
    return False


def no_raise(fn, *args, **kw):
    try:
        return True, fn(*args, **kw)
    except Exception as e:  # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, e)


def book(name):
    d = os.path.join(str(SANDBOX), name)
    os.makedirs(d, exist_ok=True)
    return d


def tmp_left(d):
    return sorted(p for p in os.listdir(d) if p.endswith(".tmp"))


def read_text(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def file_is_clean(path):
    """盘上这份文件里不该有落单的半个代理码点。"""
    try:
        return not SURROGATE.search(read_text(path))
    except OSError:
        return False


DIRTY_SEGS = [
    {"id": "s1", "start": 0.0, "end": 1.0, "text": "你" + HI + "好",
     "speaker": "说" + HALF, HI + "自由字段": "值" + LO},
    {"id": "s2", "start": 1.0, "end": 2.0, "text": "真" + EMOJI + ZWJ + TONE + "字"},
]

try:
    # ── A 对照：旧写法（裸 json.dump ensure_ascii=False）确实抛、确实留 .tmp ──
    ctl = book("control")
    ctl_path = os.path.join(ctl, "x.json")
    tmp = ctl_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"segments": DIRTY_SEGS}, f, ensure_ascii=False)
        legacy_raises = False
    except UnicodeEncodeError:
        legacy_raises = True
    chk("A1 对照：裸 ensure_ascii=False 写脏段落确实抛 UnicodeEncodeError", legacy_raises)
    chk("A2 对照：旧写法真会在书目录里留下半截 .tmp", tmp_left(ctl) != [], tmp_left(ctl))
    if os.path.exists(tmp):
        os.remove(tmp)                                 # 收掉对照留下的垃圾

    # ── B 写口：脏段落经 save_transcript 不再抛、不留 .tmp、两处视图都干净 ──
    d = book("write")
    jpath = os.path.join(d, vn.TRANS_FILE)
    tpath = os.path.join(d, vn.TRANS_TXT)
    ok, res = no_raise(vn.save_transcript, d, DIRTY_SEGS, engine="cloud", language="zh")
    chk("B1 脏段落保存不抛", ok, res)
    chk("B2 落盘成功不留 .tmp", tmp_left(d) == [], tmp_left(d))
    chk("B3 保存的段数与给出去的一致", ok and res.get("segments") == len(DIRTY_SEGS), res)

    raw = read_text(jpath)
    chk("B4 transcript.json 里没有落单码点的转义", "\\ud" not in raw.lower(),
        raw[:120])
    doc = no_raise(vn._read_json, jpath)[1]
    chk("B5 transcript.json 读回来整棵树（含键）都干净",
        isinstance(doc, dict) and not has_surrogate(doc), doc)
    chk("B6 段落里的自由字段键也洗了（'\\ud800自由字段' → '自由字段'）",
        isinstance(doc, dict) and "自由字段" in doc["segments"][0]
        and "值" in doc["segments"][0]["自由字段"], doc.get("segments") if isinstance(doc, dict) else doc)

    txt = read_text(tpath)
    chk("B7 transcript.txt 里也没有坏码点", not has_surrogate(txt), txt[:120])
    chk("B8 字数与落盘文字是同一份（words 按洗干净的字数算）",
        ok and res.get("words") == len("".join(txt.split())), (res, txt))

    # ── C 只摘坏的那半个：真 emoji / ZWJ 一个字不动，洗是幂等的 ──────────
    chk("C1 emoji 与 ZWJ 连字在 transcript.json 里原样留着",
        isinstance(doc, dict) and doc["segments"][1]["text"] == "真" + EMOJI + ZWJ + TONE + "字",
        doc["segments"][1] if isinstance(doc, dict) else doc)
    chk("C2 emoji 与 ZWJ 连字在 transcript.txt 里原样留着",
        EMOJI in txt and ZWJ in txt and TONE in txt, txt[:120])
    clean_tree = {"t": EMOJI + ZWJ, "list": [EMOJI, "顶" + HI + "点"], "n": 7,
                  ZWJ: {"深层": [EMOJI + HALF]}}
    washed = vn._drop_surrogates(clean_tree)
    chk("C3 _drop_surrogates：成对字符原样、半个摘掉、别的类型不动",
        washed["t"] == EMOJI + ZWJ and washed["list"][0] == EMOJI
        and washed["list"][1] == "顶点" and washed["n"] == 7, washed)
    chk("C4 _drop_surrogates：字典键也洗，真字符键留着",
        ZWJ in washed and washed[ZWJ]["深层"] == [EMOJI], washed)
    chk("C5 _drop_surrogates 幂等：洗过再洗结果不变",
        vn._drop_surrogates(washed) == washed)
    chk("C6 只基于「落单」下手：正常中文 / 换行 / 制表都留",
        vn._drop_surrogates("正\n常\t文") == "正\n常\t文")

    # ── D 读口：手改 / 旧版本写下的脏 transcript.json 读得开且干净 ────────
    d2 = book("read")
    dirty_doc = {"schema": vn.TRANS_SCHEMA, "engine": "cloud" + HI,
                 "language": "zh", "duration": 2.0,
                 "title": "标" + HI + "题",
                 "segments": [{"id": "s1", "start": 0.0, "end": 1.0,
                               "text": "旧" + HI + "字", "speaker": "人" + HALF},
                              {"id": "s2", "start": 1.0, "end": 2.0,
                               "text": "好" + EMOJI, HALF + "备注": "注" + LO}]}
    # ensure_ascii=True（默认）是关键：坏码点被写成 `\ud800` 转义，写得进去、读得出来
    with open(os.path.join(d2, vn.TRANS_FILE), "w", encoding="utf-8") as f:
        json.dump(dirty_doc, f)

    ok2, got = no_raise(vn.load_transcript, d2)
    chk("D1 脏 transcript.json 读得开", ok2, got)
    chk("D2 读出来的整棵树（含段落）都干净",
        ok2 and not has_surrogate(got), got if not ok2 else [s for s in got["segments"] if has_surrogate(s)])
    chk("D3 干净后文字是『旧字』（半个码点摘掉、别的字不动）",
        ok2 and got["segments"][0]["text"] == "旧字", got.get("segments") if ok2 else got)
    chk("D4 真 emoji 在脏账本里也留着",
        ok2 and got["segments"][1]["text"] == "好" + EMOJI,
        got.get("segments") if ok2 else got)
    chk("D5 全文视图里没有坏码点", ok2 and not has_surrogate(got["text"]), got.get("text") if ok2 else got)
    chk("D6 解析缓存那一份也干净",
        not has_surrogate(vn._read_json_cached(os.path.join(d2, vn.TRANS_FILE))))

    # 脏账本再存一次：读口洗过之后写口照常，且不留 .tmp
    ok3, res3 = no_raise(vn.save_transcript, d2, got["segments"] if ok2 else [], engine="cloud")
    chk("D7 脏账本读出来再存一次不抛、不留 .tmp",
        ok3 and tmp_left(d2) == [], (res3, tmp_left(d2)))

    # meta.json 也是同一道读口：书名里的半个码点不许读出来（load_transcript 拿它
    # 当兜底标题，转写里没有 title 时就会一路带出去）
    with open(os.path.join(d2, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"title": "书" + HI + "名", "asr_engine": "cloud" + LO,
                   "duration": 2.0}, f)
    ok8, meta8 = no_raise(vn._read_meta, d2)
    chk("D8 脏 meta.json 的书名读出来也干净",
        ok8 and meta8.get("title") == "书名" and not has_surrogate(meta8), meta8)

    # ── E 好数据一个字不改：干净转写存读回来是同一形状 ───────────────────
    d3 = book("clean")
    clean_segs = [{"id": "s1", "start": 0.0, "end": 1.5,
                   "text": "正" + EMOJI + ZWJ + "文", "speaker": "甲"},
                  {"id": "s2", "start": 1.5, "end": 3.0, "text": "第二段"}]
    ok4, _ = no_raise(vn.save_transcript, d3, clean_segs, engine="cloud", language="zh")
    ok5, back = no_raise(vn.load_transcript, d3)
    chk("E1 干净转写存读不抛", ok4 and ok5, (ok4, back))
    chk("E2 干净转写的文字 / 说话人 / 时间一个字没动",
        ok5 and [s["text"] for s in back["segments"]] == ["正" + EMOJI + ZWJ + "文", "第二段"]
        and back["segments"][0]["speaker"] == "甲"
        and back["segments"][0]["start"] == 0.0, back.get("segments") if ok5 else back)

    # ── F 两个裸写口本身也兜住：直接收脏数据不抛、不留 .tmp ───────────────
    d4 = book("raw")
    j4 = os.path.join(d4, vn.TRANS_FILE)
    ok6, _ = no_raise(vn._write_json, j4, {"note": "x" + HI + "y", HI + "k": [LO]})
    chk("F1 _write_json 直接收脏对象不抛、不留 .tmp",
        ok6 and tmp_left(d4) == [], (ok6, tmp_left(d4)))
    ok7, _ = no_raise(vn._write_text, os.path.join(d4, "a.txt"), "字" + HALF + "句")
    chk("F2 _write_text 直接收脏字符串不抛、不留 .tmp",
        ok7 and tmp_left(d4) == [], (ok7, tmp_left(d4)))
    chk("F3 两个裸写口落盘的内容都已干净",
        not has_surrogate(read_text(j4))
        and not has_surrogate(read_text(os.path.join(d4, "a.txt"))),
        (read_text(j4)[:80], read_text(os.path.join(d4, "a.txt"))))

    # ── G _save_book 整条：脏数据进、干净的书出 ──────────────────────────
    def fake_probe_meta(url):
        return {"title": "示例视频", "uploader": "示例UP主", "duration": 48,
                "thumbnail": "https://example.invalid/c.jpg", "id": "BV1xx411c7mD"}

    vn.probe_meta = fake_probe_meta
    plan_info = vn.plan("https://www.bilibili.com/video/BV1xx411c7mD")
    # 外站元信息也会脏：标题 / 上传者里夹一个落单码点。
    plan_info["title"] = "示例" + HI + "视频"
    plan_info["uploader"] = "示例" + LO + "UP主"

    BOOKS = str(SANDBOX / "books")
    os.makedirs(BOOKS, exist_ok=True)
    spoken = {"text": "第一段转写" + HI + "。第二段带 emoji " + EMOJI + "。",
              "segments": [{"start": 0.0, "end": 11.0, "text": "第一段" + HI + "转写"},
                           {"start": 12.0, "end": 29.0, "text": "第二段"}],
              "engine": "cloud", "language": "zh"}
    summary = {"title": "示例" + HI + "总结", "overview": "一句话说清" + LO + "这支视频。",
               "bulletPoints": ["要点" + HI + "一", "要点二"],
               "chapters": [{"title": "为什么" + LO + "先讲概念", "start": 0,
                             "summary": "先把概念" + HI + "讲清楚。"},
                            {"title": "方法怎么落地", "start": 12,
                             "summary": "给出三步做法。"}]}
    note_markdown = "# 笔记" + HI + "标题\n\n这是假的笔记正文，夹一个 " + LO + " 坏码。"
    mindmap = {"version": 1, "title": "导图" + HI + "标题", "root": "root",
               "nodes": [{"id": "root", "label": "根" + LO + "节点", "type": "root",
                          "summary": "总览", "children": [], "time_anchor": None}]}

    okg, gbook = no_raise(vn._save_book, BOOKS, plan_info, spoken, summary,
                          note_markdown, mindmap, {"language": "zh"}, "")
    chk("G1 _save_book 喂脏数据整条不抛", okg, gbook if not okg else "")

    if okg and isinstance(gbook, dict):
        bdir = gbook.get("dir") or ""
        chk("G2 返回的书书名里那个坏码被摘掉", HI not in str(gbook.get("title") or "")
            and LO not in str(gbook.get("title") or ""), gbook.get("title"))

        want = [vn.TRANS_TXT, vn.TRANS_FILE, vn.SUMMARY_FILE, "merged.md",
                "meta.json", "notes.md", "notes.json", "mindmap.json"]
        missing = [n for n in want if not os.path.isfile(os.path.join(bdir, n))]
        chk("G3 该落的那几份都在", not missing, missing)
        dirty = [n for n in want
                 if os.path.isfile(os.path.join(bdir, n)) and not file_is_clean(os.path.join(bdir, n))]
        chk("G4 落出来的每一份都不含落单的半个代理码点", not dirty, dirty)
        chk("G5 整本书里没留半截 .tmp",
            not [p for p in os.listdir(bdir) if p.endswith(".tmp")])

        # 真 emoji 得原样躺在转写里，不能被「洗坏字符」顺手洗掉。
        gtrans = read_text(os.path.join(bdir, vn.TRANS_TXT))
        chk("G6 转写里那个真 emoji 一个字没动", EMOJI in gtrans)
    else:
        for nm in ("G2 返回的书书名里那个坏码被摘掉", "G3 该落的那几份都在",
                   "G4 落出来的每一份都不含落单的半个代理码点",
                   "G5 整本书里没留半截 .tmp", "G6 转写里那个真 emoji 一个字没动"):
            chk(nm, False, "整条已经抛了")
finally:
    shutil.rmtree(str(SANDBOX), ignore_errors=True)

print()
print("视频转写落单代理码点：通过 %d 项，失败 %d 项" % (PASSED, len(FAILED)))
if FAILED:
    for f in FAILED:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  脏段落 / 脏账本 / 整本书里的半个码点不再把保存与读取打崩，真 emoji 一个字不动")
