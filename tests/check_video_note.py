# -*- coding: utf-8 -*-
"""video_note 模块的离线自测：一行一条结论，失败就非零退出。

全程不联网、不下模型：下载器 / 转写器 / LLM 调用三处模块级函数都被换成假的，
所以这条流水线能在没有网络、没有 ffmpeg、没有 Whisper 权重的机器上端到端跑一遍，
验的是「接线接对了没有」而不是「模型好不好」。

GUIZANG_DATA 指向系统临时目录下的沙盒，所以流水线写出来的书、留下的临时目录
都不会碰用户真实的数据目录与书库。跑完自己扫干净（atexit）。

真机冒烟那一条挂在 GUIZANG_VIDEO_LIVE=1 后面（还要 GUIZANG_ASR_MODEL 指个小模型），
默认跳过 —— 跳过就是跳过，会明说，不会假装跑过。

跑法：.venv/bin/python tests/check_video_note.py
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# GUIZANG_DATA 必须在 import video_note 之前设好：模块在导入时就把 DATA_DIR /
# TMP_ROOT 定下来了，设晚了临时音频还是往用户真实缓存里写。
SANDBOX = tempfile.mkdtemp(prefix="gz-video-check-")
atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))
os.environ["GUIZANG_DATA"] = os.path.join(SANDBOX, "data")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)

import video_note as vn  # noqa: E402

# 存一份真的 transcribe：假件是按段整块换掉的，云分片续跑那条要跑真的分发逻辑
# （它验的就是 _transcribe_cloud 自己，不能让假转写替它做完）。
REAL_TRANSCRIBE = vn.transcribe

FAIL = []
CHECKS = []


def chk(name, cond, extra=""):
    CHECKS.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if not cond:
        FAIL.append(name)


def has_cjk(text):
    return any("\u4e00" <= c <= "\u9fff" for c in str(text))


# ── 假件 ────────────────────────────────────────────────────────────
FAKE_CHAPTERS = [
    {"title": "为什么先讲概念", "start": 0, "summary": "先把概念讲清楚，后面的推导才有落点。"},
    {"title": "方法怎么落地", "start": 12, "summary": "给出三步做法，并说明每步的适用条件。"},
    {"title": "常见误用与结论", "start": 30, "summary": "列了两种误用，最后给出一句可带走的判断。"},
]
FAKE_TRANSCRIPT = "这一段是假的转写内容，用来验证流水线接线是否正确。" * 6
FAKE_SEGMENTS = [{"start": 0.0, "end": 11.0, "text": "第一段转写文字"},
                 {"start": 12.0, "end": 29.0, "text": "第二段转写文字"},
                 {"start": 30.0, "end": 48.0, "text": "第三段转写文字"}]
LLM_SEEN = {"stages": []}
# 假下载 / 假转写各被调了几次：断点续跑那几条靠的就是「第二次没有再调它」，
# 光看产物看不出区别（产物本来就一样）。
CALLS = {"download": [], "transcribe": []}


def fake_probe_meta(url):
    """假元信息：不联网，只验 plan 把链接归一成了什么。"""
    return {"title": "示例视频・假元信息", "uploader": "示例UP主",
            "duration": 48, "thumbnail": "https://example.invalid/c.jpg",
            "id": url.rsplit("/", 1)[-1].split("?")[0]}


def fake_download(url, task_dir, progress=None, should_stop=None):
    os.makedirs(task_dir, exist_ok=True)
    path = os.path.join(task_dir, "audio.m4a")
    with open(path, "wb") as f:
        f.write(b"\x00\x00\x00\x20ftypM4A ")          # 只要求「有个文件」，接的是假转写
    if progress:
        progress("download", 30, "假下载中")
    CALLS["download"].append({"url": url, "task_dir": task_dir})
    return path


def fake_transcribe(audio_path, asr_cfg, progress=None, should_stop=None, resume=False):
    if progress:
        progress("asr", 60, "假转写中")
    CALLS["transcribe"].append({"audio": audio_path, "resume": bool(resume),
                                "engine": asr_cfg.get("engine") or "auto",
                                "language": asr_cfg.get("language") or ""})
    return {"text": FAKE_TRANSCRIPT, "segments": [dict(s) for s in FAKE_SEGMENTS],
            "engine": "fake"}


def _stage_of(messages):
    blob = "".join(str((m or {}).get("content") or "") for m in messages or [])
    if "knowledgeNoteMarkdown" in blob:
        return "note"
    if "time_anchor" in blob:
        return "mindmap"
    return "summary"


def make_fake_llm(broken_mindmap=False, fail_all=False):
    """假 LLM：按提示词认出这是哪一段，回对应的形状。"""
    def call(cfg, messages, timeout=180):
        stage = _stage_of(messages)
        LLM_SEEN["stages"].append(stage)
        if fail_all:
            raise RuntimeError("假装接口挂了")
        if stage == "summary":
            return json.dumps({"title": "示例视频总结", "overview": "一句话说清这支视频在讲什么。"
                              "中间交代关键论据。最后给一个可带走的结论。",
                              "bulletPoints": ["要点一", "要点二", "要点三"],
                              "chapters": FAKE_CHAPTERS}, ensure_ascii=False)
        if stage == "note":
            return json.dumps({"knowledgeNoteMarkdown":
                               "# 示例视频总结\n\n## 核心结论\n\n这是假的笔记正文。"},
                              ensure_ascii=False)
        if broken_mindmap:
            return "这不是 JSON，模型跑偏了 {{"
        return json.dumps({"version": 1, "title": "示例视频总结", "root": "root",
                           "nodes": [
                               {"id": "root", "label": "示例视频总结", "type": "root",
                                "summary": "总览", "children": ["t1", "t2", "t3"],
                                "time_anchor": None},
                               {"id": "t1", "label": "概念", "type": "theme",
                                "summary": "概念说明", "children": [], "time_anchor": 0},
                               {"id": "t2", "label": "方法", "type": "theme",
                                "summary": "方法说明", "children": [], "time_anchor": 12},
                               {"id": "t3", "label": "结论", "type": "theme",
                                "summary": "结论说明", "children": [], "time_anchor": 30},
                           ]}, ensure_ascii=False)
    return call


def use_fakes(llm=None):
    vn.probe_meta = fake_probe_meta
    vn.download_audio = fake_download
    vn.transcribe = fake_transcribe
    vn.llm_chat = llm or make_fake_llm()


BOOKS = os.path.join(SANDBOX, "books")
LLM_CFG = {"url": "https://example.invalid/v1", "key": "k", "model": "fake-model"}
OPTS = {"asr": {"engine": "auto", "model": ""}, "llm": LLM_CFG, "language": "zh"}


# ── 1. plan 的链接归一（纯字符串 + 假元信息，不联网） ───────────────
use_fakes()
cases = [
    ("B 站 BV 号（带分 P 参数）", "https://www.bilibili.com/video/BV1xx411c7mD?p=3",
     "bilibili", "BV1xx411c7mD", "https://www.bilibili.com/video/BV1xx411c7mD?p=3"),
    ("B 站裸 BV 号", "BV1xx411c7mD", "bilibili", "BV1xx411c7mD",
     "https://www.bilibili.com/video/BV1xx411c7mD"),
    ("B 站整段分享文字", "看看这个 https://www.bilibili.com/video/BV1xx411c7mD?p=2 挺好的",
     "bilibili", "BV1xx411c7mD", "https://www.bilibili.com/video/BV1xx411c7mD?p=2"),
    ("YouTube watch?v=", "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3s",
     "youtube", "dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("YouTube youtu.be", "https://youtu.be/dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ",
     "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("YouTube shorts", "https://www.youtube.com/shorts/abc123XYZ",
     "youtube", "abc123XYZ", "https://www.youtube.com/watch?v=abc123XYZ"),
]
for label, raw, want_platform, want_vid, want_url in cases:
    got = vn.plan(raw)
    ok = (got["platform"] == want_platform and got["vid"] == want_vid
          and got["url"] == want_url)
    chk("归一：%s" % label, ok, got)

paged = vn.plan("https://www.bilibili.com/video/BV1xx411c7mD?p=3")
chk("归一：BV 认出了分 P 号", vn.normalize_url(
    "https://www.bilibili.com/video/BV1xx411c7mD?p=3")[3] == 3,
    vn.normalize_url("https://www.bilibili.com/video/BV1xx411c7mD?p=3"))
chk("plan：标题/作者/时长/封面都带出来了",
    paged["title"] and paged["uploader"] == "示例UP主"
    and paged["duration"] == 48 and paged["cover"], paged)
chk("plan：短视频也给出 pages（单 P 至少一条）",
    isinstance(paged["pages"], list) and len(paged["pages"]) >= 1
    and paged["pages"][0]["url"] == paged["url"], paged.get("pages"))


# ── 2. 认不出的链接报人话（中文），不是 traceback ───────────────────
for label, raw in [("陌生站点", "https://example.com/a/b"),
                   ("空串", ""),
                   ("没有视频编号的站内页", "https://www.youtube.com/feed/subscriptions")]:
    try:
        vn.plan(raw)
        chk("拒绝：%s 应当抛 ValueError" % label, False, raw)
    except ValueError as e:
        ok = has_cjk(e) and "Traceback" not in str(e)
        chk("拒绝：%s 抛的是中文人话" % label, ok, str(e))
    except Exception as e:  # noqa: BLE001
        chk("拒绝：%s 抛的是 ValueError" % label, False, repr(e))


# ── 3. 整条 run() 跑通，落出来的书要能进阅读器 ──────────────────────
use_fakes()
LLM_SEEN["stages"] = []
seen_progress = []


def watch(stage, pct, note):
    seen_progress.append((stage, pct, note))


result = vn.run("https://www.bilibili.com/video/BV1xx411c7mD?p=1", BOOKS, OPTS, watch)
book_dir = result["book"].get("dir") or ""
chk("落盘：书目录真建出来了", bool(book_dir) and os.path.isdir(book_dir), book_dir)
meta = json.load(open(os.path.join(book_dir, "meta.json"), encoding="utf-8")) \
    if os.path.isfile(os.path.join(book_dir, "meta.json")) else {}
catalog = json.load(open(os.path.join(book_dir, "_catalog.json"), encoding="utf-8")) \
    if os.path.isfile(os.path.join(book_dir, "_catalog.json")) else []
ch_dir = os.path.join(book_dir, "chapters")
chapter_files = sorted(f for f in os.listdir(ch_dir)) if os.path.isdir(ch_dir) else []

chk("阅读器：meta.json 存在且 source=video",
    meta.get("source") == "video", meta.get("source"))
chk("阅读器：meta.done 为真（不然书架不认这本书）", meta.get("done") is True, meta.get("done"))
chk("阅读器：meta 带 url / duration / vid / asr_engine",
    all(k in meta for k in ("url", "duration", "vid", "asr_engine")), sorted(meta))
chk("阅读器：chapters/ 里章节数与目录一致",
    len(chapter_files) == len(FAKE_CHAPTERS), (len(chapter_files), len(FAKE_CHAPTERS)))
chk("阅读器：_catalog.json 就是假 LLM 给的章节名",
    catalog == [c["title"] for c in FAKE_CHAPTERS], catalog)
titles_from_files = []
for name in chapter_files:
    with open(os.path.join(ch_dir, name), encoding="utf-8") as f:
        first = next((ln.strip() for ln in f if ln.strip()), "")
    titles_from_files.append(first.lstrip("#").strip())
chk("阅读器：每章首行就是章名（book_outline 靠它取标题）",
    titles_from_files == [c["title"] for c in FAKE_CHAPTERS], titles_from_files)
chk("阅读器：_progress.json 落下了（书架看进度）",
    os.path.isfile(os.path.join(book_dir, "_progress.json")), chapter_files)
chk("产物：transcript.txt 里有真转写",
    os.path.isfile(result["transcript_path"])
    and FAKE_TRANSCRIPT[:20] in open(result["transcript_path"], encoding="utf-8").read(),
    result["transcript_path"])
summary_doc = json.load(open(os.path.join(book_dir, "summary.json"), encoding="utf-8")) \
    if os.path.isfile(os.path.join(book_dir, "summary.json")) else {}
chk("产物：summary.json 有摘要与分段",
    summary_doc.get("summary", {}).get("overview") and summary_doc.get("segments"),
    sorted(summary_doc))
chk("产物：按 book_notes 的 schema 落了 notes.json",
    os.path.isfile(os.path.join(book_dir, "notes.json"))
    and len(vn.book_notes.load_notes(book_dir).get("entries") or []) == 1,
    vn.book_notes.load_notes(book_dir).get("entries"))
chk("产物：notes.md 导出了", os.path.isfile(os.path.join(book_dir, "notes.md")),
    os.listdir(book_dir))
chk("产物：mindmap.json 是约定的 {version,title,root,nodes[]}",
    os.path.isfile(result["mindmap_path"])
    and json.load(open(result["mindmap_path"], encoding="utf-8")).get("root") == "root",
    result["mindmap_path"])
# meta 里 mindmap_svg 只存文件名（跟 book_notes 落盘时一个规矩），拼上书目录再验存不存在。
chk("产物：mindmap.svg 也用现成画法出图了",
    os.path.isfile(os.path.join(result["book"].get("dir") or "",
                                result["book"].get("mindmap_svg") or "")),
    os.path.join(result["book"].get("dir") or "",
                 result["book"].get("mindmap_svg") or ""))
chk("返回：chapters / words / asr_engine / elapsed 都给了",
    result["chapters"] == len(FAKE_CHAPTERS) and result["words"] > 0
    and result["asr_engine"] == "fake" and result["elapsed"] >= 0, result)
chk("返回：AI 这轮没出错，ai_error 为空", result["ai_error"] == "", result["ai_error"])
chk("接线：LLM 三段都按顺序调了",
    LLM_SEEN["stages"] == ["summary", "note", "mindmap"], LLM_SEEN["stages"])


# ── 4. LLM 全挂了：转写照存，书照进书架，并说清为什么 ──────────────
use_fakes(make_fake_llm(fail_all=True))
books_fail = os.path.join(SANDBOX, "books-llm-fail")
failed = vn.run("https://youtu.be/dQw4w9WgXcQ", books_fail, OPTS, None)
fail_dir = failed["book"].get("dir") or ""
fail_meta = json.load(open(os.path.join(fail_dir, "meta.json"), encoding="utf-8")) \
    if os.path.isfile(os.path.join(fail_dir, "meta.json")) else {}
fail_text = open(failed["transcript_path"], encoding="utf-8").read() \
    if os.path.isfile(failed["transcript_path"]) else ""
chk("降级：LLM 全挂也能落成一本书", os.path.isdir(fail_dir), fail_dir)
chk("降级：转写完整保留", FAKE_TRANSCRIPT[:20] in fail_text, len(fail_text))
chk("降级：返回里说清了 AI 为什么没成", bool(failed["ai_error"]), failed["ai_error"])
chk("降级：meta 里也记下了失败原因",
    bool(fail_meta.get("ai_error")) and fail_meta.get("llm_ok") is None, fail_meta.get("ai_error"))
chk("降级：退成单独一章「全文转写」",
    json.load(open(os.path.join(fail_dir, "_catalog.json"), encoding="utf-8")) == ["全文转写"],
    json.load(open(os.path.join(fail_dir, "_catalog.json"), encoding="utf-8")))
chk("降级：笔记条目还在（正文是摘要兜底那几句）",
    len(vn.book_notes.load_notes(fail_dir).get("entries") or []) == 1, None)


# ── 5. 导图 JSON 坏掉：本地兜底，绝不抛 ─────────────────────────────
use_fakes(make_fake_llm(broken_mindmap=True))
try:
    bad_map, warn = vn.build_mindmap(
        LLM_CFG, "示例视频总结",
        {"overview": "总览", "bulletPoints": ["要点一", "要点二", "要点三"],
         "chapters": FAKE_CHAPTERS}, "笔记正文")
    ok = True
except Exception as e:  # noqa: BLE001
    bad_map, warn, ok = {}, repr(e), False
chk("导图：模型回烂 JSON 时不抛异常", ok, warn)
chk("导图：兜底树至少有 3 个一级分支",
    vn._mindmap_top_branches(bad_map) >= 3, bad_map)
chk("导图：兜底也说明了原因", bool(warn), warn)
chk("导图：schema 齐整（version/title/root/nodes）",
    bad_map.get("version") == 1 and bad_map.get("root") == "root"
    and isinstance(bad_map.get("nodes"), list), sorted(bad_map))
svg = vn.book_notes.build_mindmap_svg(vn.mindmap_to_tree(bad_map), "示例视频总结")
chk("导图：兜底树能画成 SVG", svg.startswith("<svg") and len(svg) > 200, len(svg))


# ── 6. should_stop 在阶段之间把整条线拦下来 ────────────────────────
use_fakes()
books_stop = os.path.join(SANDBOX, "books-stop")
stopped = {"hit": None}
try:
    vn.run("https://www.bilibili.com/video/BV1xx411c7mD", books_stop,
           {**OPTS, "should_stop": lambda: True}, None)
    stopped["hit"] = "no-raise"
except vn.Aborted as e:
    stopped["hit"] = "aborted"
    stopped["msg"] = str(e)
except Exception as e:  # noqa: BLE001
    stopped["hit"] = repr(e)
chk("中止：should_stop 为真时抛 video_note.Aborted（约定的中止信号）",
    stopped["hit"] == "aborted", stopped)
chk("中止：话是中文且说明手头产物还在",
    has_cjk(stopped.get("msg")) and "还在" in stopped.get("msg", ""), stopped.get("msg"))
chk("中止：没留下半本书",
    not os.path.isdir(books_stop) or not os.listdir(books_stop), 
    os.listdir(books_stop) if os.path.isdir(books_stop) else None)


# ── 7. progress 的 pct 单调不减 ────────────────────────────────────
use_fakes()
run_progress = []
vn.run("https://www.bilibili.com/video/BV1xx411c7mD?p=2",
       os.path.join(SANDBOX, "books-progress"), OPTS,
       lambda stage, pct, note: run_progress.append((stage, pct, note)))
pcts = [pct for _stage, pct, _note in run_progress if pct is not None]
mono = all(pcts[i] <= pcts[i + 1] for i in range(len(pcts) - 1))
chk("进展：回调被调了多次", len(run_progress) >= 6, len(run_progress))
chk("进展：pct 单调不减", mono, pcts)
chk("进展：stage 用的是文档里那几个键",
    {s for s, _p, _n in run_progress} <= {"plan", "download", "asr", "summary",
                                          "note", "mindmap", "save", "done"},
    sorted({s for s, _p, _n in run_progress}))
chk("进展：每条 note 都是中文",
    all(has_cjk(n) for _s, _p, n in run_progress),
    [n for _s, _p, n in run_progress][:3])


# ── 8. transcript.json：带时间戳的那一份终于落了盘 ──────────────────
TRANS_JSON = os.path.join(book_dir, "transcript.json")
merged_path = result.get("merged_path") or ""
chk("落盘：run 给了 transcript_json / merged_path，两个文件都真在",
    result.get("transcript_json") == TRANS_JSON and os.path.isfile(TRANS_JSON)
    and os.path.isfile(merged_path), (result.get("transcript_json"), merged_path))
tdoc = json.load(open(TRANS_JSON, encoding="utf-8")) if os.path.isfile(TRANS_JSON) else {}
chk("落盘：transcript.json 带 engine / language / duration / segments",
    tdoc.get("engine") == "fake" and tdoc.get("language") == "zh"
    and tdoc.get("duration") == 48.0 and isinstance(tdoc.get("segments"), list),
    sorted(tdoc))
chk("落盘：段落原样带出时间戳（浮秒，没被抹平成整数或只剩整篇文字）",
    [(s.get("start"), s.get("end"), s.get("text")) for s in tdoc.get("segments") or []]
    == [(s["start"], s["end"], s["text"]) for s in FAKE_SEGMENTS], tdoc.get("segments"))
chk("落盘：transcript_path 的含义没变（还是那份纯文本）",
    result["transcript_path"] == os.path.join(book_dir, "transcript.txt")
    and open(result["transcript_path"], encoding="utf-8").read().strip() == FAKE_TRANSCRIPT,
    result["transcript_path"])
with open(merged_path, encoding="utf-8") as f:
    merged_text = f.read()
part_texts = []
for name in chapter_files:
    with open(os.path.join(ch_dir, name), encoding="utf-8") as f:
        part_texts.append(f.read())
chk("落盘：merged.md 就是分章文件拼出来的那一本（重建与落盘共用一份拼法）",
    merged_text == "\n".join(part_texts).strip() + "\n", merged_text[:60])
chk("落盘：meta 补了 segments / language / transcript_edited（前端认这几个）",
    meta.get("segments") == len(FAKE_SEGMENTS) and meta.get("language") == "zh"
    and meta.get("transcript_edited") is False, sorted(meta))
chk("落盘：新加的文件不冒充章节（chapters/ 里只有 md，阅读器目录没被顶偏）",
    all(n.endswith(".md") for n in os.listdir(ch_dir))
    and catalog == [c["title"] for c in FAKE_CHAPTERS], os.listdir(book_dir))
chk("返回：result 补了 segments / duration / language（时长是浮秒）",
    result.get("segments") == len(FAKE_SEGMENTS)
    and isinstance(result.get("duration"), float) and result.get("language") == "zh"
    and "resume_dir" in result, (result.get("duration"), result.get("segments")))


# ── 9. 时间戳收敛：-0.0 / None / 脏值都不许把导出带崩 ───────────────
chk("时间：None / 脏字符串 / 负数都兜成 0，且不会留下 -0.0",
    vn._sec(None) == 0.0 and vn._sec("abc") == 0.0 and vn._sec(-3) == 0.0
    and repr(vn._sec(-0.0)) == "0.0" and repr(vn._sec(None)) == "0.0",
    (repr(vn._sec(-0.0)), repr(vn._sec(None))))
chk("时间：「没有时间」收成 None 而不是 0（老书兜底就靠这个区分）",
    vn._maybe_sec(None) is None and vn._maybe_sec("") is None
    and vn._maybe_sec("乱码") is None and vn._maybe_sec(12) == 12.0,
    (vn._maybe_sec(None), vn._maybe_sec("乱码")))
chk("时间：SRT 用逗号、VTT 用点号，毫秒固定三位整数",
    vn._cue_time(0) == "00:00:00,000"
    and vn._cue_time(3661.5, ".") == "01:01:01.500"
    and vn._cue_time(None) == "00:00:00,000" and vn._cue_time(-0.0) == "00:00:00,000",
    (vn._cue_time(3661.5, "."), vn._cue_time(-0.0)))
chk("时间：先四舍五入成总毫秒，不会甩出六位小数",
    vn._cue_time(3.14159) == "00:00:03,142", vn._cue_time(3.14159))
chk("时间：_stamp 取整秒（导图锚点与 Markdown 用的是 HH:MM:SS）",
    vn._stamp(12.9) == "00:00:12" and vn._stamp(None) == "00:00:00", vn._stamp(12.9))


# ── 10. load_transcript：编辑器拿到的形状 ───────────────────────────
loaded = vn.load_transcript(book_dir)
chk("读取：新版书认到 transcript.json（source=json），带出引擎/语言/时长",
    loaded["source"] == "json" and loaded["engine"] == "fake"
    and loaded["language"] == "zh" and loaded["duration"] == 48.0
    and loaded["timed"] is True and loaded["edited"] is False, sorted(loaded))
chk("读取：段落全量给回，text 是拼好的纯文本视图",
    loaded["total"] == len(FAKE_SEGMENTS) and loaded["matched"] == len(FAKE_SEGMENTS)
    and "第一段转写文字" in loaded["text"], loaded["total"])
by_key = vn.load_transcript(book_dir, keyword="第二段")
chk("筛选：关键词只留命中的那段（total 仍是全量，matched 是命中数）",
    by_key["matched"] == 1 and by_key["total"] == len(FAKE_SEGMENTS)
    and by_key["segments"][0]["text"] == "第二段转写文字", by_key["matched"])
by_time = vn.load_transcript(book_dir, start=11.5, end=29.5)
chk("筛选：按时间取交集（11.5~29.5 只命中中间那段，0~11 那段不算）",
    by_time["matched"] == 1 and by_time["segments"][0]["start"] == 12.0,
    [(s["start"], s["end"]) for s in by_time["segments"]])
by_open = vn.load_transcript(book_dir, start=30)
chk("筛选：只给起点也筛得动（30 秒往后是最后一段）",
    by_open["matched"] == 1 and by_open["segments"][0]["start"] == 30.0, by_open["matched"])


# ── 11. 老书兜底：升级前转的那本只有 transcript.txt ─────────────────
LEGACY = os.path.join(SANDBOX, "legacy", "只有txt的老书")
os.makedirs(LEGACY)
with open(os.path.join(LEGACY, "transcript.txt"), "w", encoding="utf-8") as f:
    f.write("第一行老转写\n第二行老转写\n\n   \n")
with open(os.path.join(LEGACY, "meta.json"), "w", encoding="utf-8") as f:
    json.dump({"title": "升级前转的那本", "source": "video",
               "asr_engine": "mlx-whisper", "duration": 20}, f, ensure_ascii=False)
old = vn.load_transcript(LEGACY)
chk("兜底：只有 transcript.txt 的老书读得开（source=txt，不抛）",
    old["source"] == "txt" and old["total"] == 2
    and old["path"] == os.path.join(LEGACY, "transcript.txt"),
    (old["source"], old["total"]))
chk("兜底：老书段落时间戳是 None（不假装它从 0 秒开始）",
    all(s["start"] is None and s["end"] is None for s in old["segments"])
    and old["timed"] is False, old["segments"])
chk("兜底：引擎 / 时长 / 书名从 meta.json 认出来",
    old["engine"] == "mlx-whisper" and old["duration"] == 20.0
    and old["title"] == "升级前转的那本", (old["engine"], old["duration"]))
chk("兜底：老书带时间条件时诚实全不命中（没时间戳没法算交集）",
    vn.load_transcript(LEGACY, start=0, end=5)["matched"] == 0
    and vn.load_transcript(LEGACY, start=0, end=5)["total"] == 2, None)
NO_TRANS = os.path.join(SANDBOX, "legacy", "空书")
os.makedirs(NO_TRANS)
for label, target in [("两份转写都没有的书", NO_TRANS),
                      ("压根不存在的目录", os.path.join(SANDBOX, "legacy", "没这本书"))]:
    try:
        vn.load_transcript(target)
        chk("兜底：%s 应当抛 ValueError" % label, False, target)
    except ValueError as e:
        chk("兜底：%s 抛的是中文人话不是 traceback" % label, has_cjk(e), str(e))
    except Exception as e:  # noqa: BLE001
        chk("兜底：%s 抛的是 ValueError" % label, False, repr(e))


# ── 12. save_transcript：编辑器保存 ─────────────────────────────────
EDIT_BOOK = os.path.join(SANDBOX, "books-edit", os.path.basename(book_dir))
shutil.copytree(book_dir, EDIT_BOOK)
NEW_SEGS = [{"start": 0.0, "end": 5.5, "text": "改过的第一句", "id": "a",
             "confidence": 0.91},
            {"start": 5.5, "end": 11.0, "text": "改过的第二句", "id": "b",
             "speaker": "甲"},
            {"start": 12.0, "end": 29.0, "text": "第二段转写文字", "id": "c"}]
saved = vn.save_transcript(EDIT_BOOK, NEW_SEGS)
sdic = json.load(open(os.path.join(EDIT_BOOK, "transcript.json"), encoding="utf-8"))
chk("保存：写在原路上（不是一路读一路写），返回段落数与字数",
    saved["path"] == os.path.join(EDIT_BOOK, "transcript.json")
    and saved["segments"] == 3 and saved["words"] > 0 and saved["timed"] is True
    and saved["rebuild_needed"] is True, saved)
chk("保存：原子写没留半截（目录里搜不到 .tmp）",
    not any(n.endswith(".tmp") for n in os.listdir(EDIT_BOOK)),
    [n for n in os.listdir(EDIT_BOOK) if n.endswith(".tmp")])
chk("保存：没让前端传的顶层字段原样留着（书名/编号/第几P/生成时间）",
    sdic.get("vid") == "BV1xx411c7mD" and sdic.get("page") == 1
    and sdic.get("title") and sdic.get("generated_at"), sorted(sdic))
chk("保存：标了已修订（edited / edited_at），读出来就知道这本动过手",
    sdic.get("edited") is True and sdic.get("edited_at"), sorted(sdic))
chk("保存：段里认不出的字段不丢（confidence 是引擎给的，编辑器不该擦掉）",
    sdic["segments"][0].get("confidence") == 0.91
    and sdic["segments"][1].get("speaker") == "甲", sdic["segments"])
chk("保存：start/end/text 排在最前（导出的 JSON 给人看时顺序稳定）",
    list(sdic["segments"][1])[:4] == ["start", "end", "text", "speaker"],
    list(sdic["segments"][1]))
chk("保存：纯文本那份跟着同步（两份是同一个转写的两个视图）",
    "改过的第一句" in open(os.path.join(EDIT_BOOK, "transcript.txt"),
                         encoding="utf-8").read(), None)
chk("保存：改过的词筛得着，说话人也筛得着",
    vn.load_transcript(EDIT_BOOK, keyword="改过的")["matched"] == 2
    and vn.load_transcript(EDIT_BOOK, keyword="甲")["matched"] == 1, None)
twice = vn.save_transcript(EDIT_BOOK, [{"id": "a", "text": "又改了一遍的第一句"}])
twice_doc = json.load(open(os.path.join(EDIT_BOOK, "transcript.json"), encoding="utf-8"))
chk("保存：同一 id 再存一次，没重给的字段（时间戳/置信度）按 id 认回来了",
    twice["segments"] == 1
    and twice_doc["segments"][0]["start"] == 0.0
    and twice_doc["segments"][0]["confidence"] == 0.91
    and twice_doc["segments"][0]["text"] == "又改了一遍的第一句", twice_doc["segments"])
chk("保存：段落顺序就是传进来的顺序（前端怎么排就怎么存）",
    [s["text"] for s in
     json.load(open(os.path.join(EDIT_BOOK, "transcript.json"), encoding="utf-8")
               )["segments"]] == ["又改了一遍的第一句"], None)
vn.save_transcript(EDIT_BOOK, NEW_SEGS)          # 恢复成三段，给重建那段用
for label, args in [("段落不是列表", (EDIT_BOOK, "不是列表")),
                    ("一段都不给", (EDIT_BOOK, [])),
                    ("书目录不存在", (os.path.join(SANDBOX, "没这本书"), []))]:
    try:
        vn.save_transcript(*args)
        chk("保存：%s 应当抛 ValueError" % label, False, args)
    except ValueError as e:
        chk("保存：%s 抛的是中文人话" % label, has_cjk(e), str(e))
    except Exception as e:  # noqa: BLE001
        chk("保存：%s 抛的是 ValueError" % label, False, repr(e))
old_txt_before = open(os.path.join(LEGACY, "transcript.txt"), encoding="utf-8").read()
vn.save_transcript(LEGACY, [{"start": None, "end": None, "text": "老书里改的一行"}],
                   sync_txt=False)
chk("保存：老书也改得动（没 json 也能存出一份），sync_txt=False 时纯文本没动",
    open(os.path.join(LEGACY, "transcript.txt"), encoding="utf-8").read()
    == old_txt_before
    and vn.load_transcript(LEGACY)["source"] == "json", None)
legacy_doc = json.load(open(os.path.join(LEGACY, "transcript.json"), encoding="utf-8"))
chk("保存：老书存的这份仍认得出引擎与书名（顶层没被清空）",
    vn.load_transcript(LEGACY)["engine"] == "mlx-whisper"
    and legacy_doc.get("edited") is True, sorted(legacy_doc))


# ── 13. rebuild_book：改完转写，全书跟着变，旧的可回退 ──────────────
edit_ch = os.path.join(EDIT_BOOK, "chapters")
with open(os.path.join(edit_ch, "0000.md"), encoding="utf-8") as f:
    before_ch0 = f.read()
notes_doc = vn.book_notes.load_notes(EDIT_BOOK)
notes_doc.setdefault("marks", []).append({"id": "m_mine01", "ch": "0001.md",
                                         "text": "这是我自己的划线", "kind": "highlight"})
vn.book_notes.save_notes(EDIT_BOOK, notes_doc)
with open(os.path.join(EDIT_BOOK, "_progress.json"), "w", encoding="utf-8") as f:
    json.dump({"at": 2, "max": 3}, f)
reb = vn.rebuild_book(EDIT_BOOK)
reb_meta = json.load(open(os.path.join(EDIT_BOOK, "meta.json"), encoding="utf-8"))
backup = reb.get("backup") or ""
chk("重建：返回给了章数/字数/备份目录，备份是 .bak-时间戳/ 那种",
    reb["ok"] is True and reb["chapters"] == 3 and reb["words"] > 0
    and os.path.isdir(backup) and os.path.basename(backup).startswith(".bak-"), reb)
chk("重建：旧文件是「挪」进备份的（章目录/纯文本/目录/元信息/笔记 md）",
    sorted(reb["moved"]) == sorted(["chapters", "transcript.txt", "_catalog.json",
                                    "meta.json", "notes.md"]), reb["moved"])
bk_ch0 = os.path.join(backup, "chapters", "0000.md")
chk("回退：旧章节文件在备份里完好（想退回去把整批挪回来就行）",
    os.path.isfile(bk_ch0) and open(bk_ch0, encoding="utf-8").read() == before_ch0,
    before_ch0[:40])
chk("回退：备份里另存了不重写的三份参照（转写/摘要/笔记）",
    all(os.path.isfile(os.path.join(backup, n))
        for n in ("transcript.json", "summary.json", "notes.json")), os.listdir(backup))
with open(os.path.join(edit_ch, "0000.md"), encoding="utf-8") as f:
    after_ch0 = f.read()
chk("重建：章正文跟着新转写走了（旧句子从章里消失）",
    "改过的第一句" in after_ch0 and "第一段转写文字" not in after_ch0, after_ch0[:60])
chk("重建：章数与章名沿用上一版（重建不叫 AI，改的是正文归属）",
    json.load(open(os.path.join(EDIT_BOOK, "_catalog.json"), encoding="utf-8"))
    == [c["title"] for c in FAKE_CHAPTERS]
    and len([n for n in os.listdir(edit_ch) if n.endswith(".md")]) == 3, None)
chk("重建：合并稿与纯文本都跟着改了（不留一份旧的在那儿骗人）",
    "改过的第一句" in open(reb["merged_path"], encoding="utf-8").read()
    and "改过的第一句" in open(reb["transcript_path"], encoding="utf-8").read(), None)
chk("重建：章正文里说明了这是手工修订后的重建（读者认得出这本书动过）",
    "手工修订" in after_ch0, after_ch0[-200:])
kept_notes = vn.book_notes.load_notes(EDIT_BOOK)
chk("重建：用户自己的划线一个字没丢（notes.json 不重写）",
    any(m.get("id") == "m_mine01" and m.get("text") == "这是我自己的划线"
        for m in (kept_notes.get("marks") or [])), kept_notes.get("marks"))
with open(reb["notes_path"], encoding="utf-8") as f:
    notes_md = f.read()
chk("重建：notes.md 按新目录重导过（划线那句出现在里面）",
    "这是我自己的划线" in notes_md and os.path.isfile(reb["notes_path"]),
    reb["notes_path"])
reb_prog = json.load(open(os.path.join(EDIT_BOOK, "_progress.json"), encoding="utf-8"))
chk("重建：阅读进度没被重置（读到第 2 章还在第 2 章）",
    reb_prog.get("at") == 2 and reb_prog.get("max") == 3, reb_prog)
chk("重建：meta 字数/段数按新转写重算，并标了已修订",
    reb_meta.get("words") == reb["words"] and reb_meta.get("segments") == 3
    and reb_meta.get("transcript_edited") is True and reb_meta.get("rebuilt_at"),
    (reb_meta.get("words"), reb["words"]))
chk("重建：书还是那本阅读器认得的格式（章名仍是每份文件首行）",
    all(open(os.path.join(edit_ch, n), encoding="utf-8").readline().startswith("# ")
        for n in sorted(os.listdir(edit_ch))), None)

ROLL_BOOK = os.path.join(SANDBOX, "books-rollback", os.path.basename(book_dir))
shutil.copytree(book_dir, ROLL_BOOK)
real_write_json = vn._write_json


def boom_write_json(path, obj):
    if str(path).endswith("_catalog.json"):
        raise RuntimeError("假装重建写到一半挂了")
    real_write_json(path, obj)


vn._write_json = boom_write_json
raised = None
try:
    vn.rebuild_book(ROLL_BOOK)
    chk("重建：中途写挂了应当抛出来", False, "no-raise")
except Exception as e:  # noqa: BLE001
    raised = e
finally:
    vn._write_json = real_write_json
chk("重建：中途写挂了确实把错抛给调用方（不是吞掉后回一句重建好了）",
    raised is not None and "挂了" in str(raised), repr(raised))
roll_ch0 = os.path.join(ROLL_BOOK, "chapters", "0000.md")
chk("回滚：写挂了旧的挪回来（章文件还是原来那本，没半新半旧）",
    os.path.isfile(roll_ch0)
    and open(roll_ch0, encoding="utf-8").read() == before_ch0, None)
chk("回滚：写挂了目录/元信息/纯文本也都在原位（书照样打得开）",
    all(os.path.exists(os.path.join(ROLL_BOOK, n))
        for n in ("_catalog.json", "meta.json", "transcript.txt", "notes.md"))
    and vn.load_transcript(ROLL_BOOK)["matched"] == 3, os.listdir(ROLL_BOOK))

EMPTYT = os.path.join(SANDBOX, "books-empty-transcript")
os.makedirs(EMPTYT)
with open(os.path.join(EMPTYT, "transcript.json"), "w", encoding="utf-8") as f:
    json.dump({"schema": 1, "segments": []}, f)
for label, target in [("转写是空的", EMPTYT), ("目录不存在", os.path.join(SANDBOX, "没那本书"))]:
    try:
        vn.rebuild_book(target)
        chk("重建:%s 应当抛 ValueError" % label, False, target)
    except ValueError as e:
        chk("重建：%s 抛的是中文人话" % label, has_cjk(e), str(e))
    except Exception as e:  # noqa: BLE001
        chk("重建：%s 抛的是 ValueError" % label, False, repr(e))


# ── 14. export_transcript：字幕 / 纯文本 / Markdown / JSON ──────────
srt = vn.export_transcript(book_dir, "srt")
srt_body = open(srt["path"], encoding="utf-8").read()
chk("导出：SRT 时间是 HH:MM:SS,mmm（逗号三位毫秒）",
    srt_body.startswith("1\n00:00:00,000 --> 00:00:11,000\n第一段转写文字\n\n"
                        "2\n00:00:12,000 --> 00:00:29,000\n第二段转写文字\n"),
    srt_body[:80])
chk("导出：SRT 尾巴干净（三条 cue、两个空行分隔、没有多余的 4）",
    srt_body.count("\n\n") == 2 and "\n4\n" not in srt_body
    and srt_body.rstrip().endswith("第三段转写文字"), srt_body[-60:])
vtt = vn.export_transcript(book_dir, "vtt")
vtt_body = open(vtt["path"], encoding="utf-8").read()
chk("导出：VTT 以 WEBVTT 开头、时间是 HH:MM:SS.mmm（点号）",
    vtt_body.startswith("WEBVTT\n\n1\n00:00:00,000") is False
    and vtt_body.startswith("WEBVTT\n\n1\n00:00:00.000 --> 00:00:11.000\n"),
    vtt_body[:60])
chk("导出：txt 是纯文本没有时间码，md 带 **[HH:MM:SS]** 前缀",
    "-->" not in open(vn.export_transcript(book_dir, "txt")["path"],
                      encoding="utf-8").read()
    and "**[00:00:12]** 第二段转写文字" in open(
        vn.export_transcript(book_dir, "md")["path"], encoding="utf-8").read(), None)
js = vn.export_transcript(book_dir, "json")
js_doc = json.load(open(js["path"], encoding="utf-8"))
chk("导出：json 那份带 schema/source/engine/duration/segments/text",
    js_doc.get("source") == "json" and js_doc.get("engine") == "fake"
    and js_doc.get("duration") == 48.0 and len(js_doc.get("segments") or []) == 3
    and js_doc.get("text"), sorted(js_doc))
chk("导出：默认落在书目录的 exports/ 里，不占 transcript.txt 那种有正用的名字",
    os.path.dirname(srt["path"]) == os.path.join(book_dir, "exports")
    and srt["name"].endswith(".srt") and "/" not in srt["name"]
    and not os.path.exists(os.path.join(book_dir, "exports", "transcript.txt")),
    srt["path"])
chk("导出：返回给前端下载的四样（绝对路径 / 字节 / 去空白字数 / 段数）",
    os.path.isabs(srt["path"]) and srt["bytes"] > 0 and srt["chars"] > 0
    and srt["segments"] == 3 and srt["timed"] is True
    and srt["book_dir"] == book_dir, srt)
BLANK = os.path.join(SANDBOX, "books-export-blank")
os.makedirs(BLANK)
with open(os.path.join(BLANK, "transcript.json"), "w", encoding="utf-8") as f:
    json.dump({"schema": 1, "engine": "fake", "language": "zh", "duration": 15.0,
               "title": "有空段的书",
               "segments": [{"start": 0.0, "end": 5.0, "text": "有词"},
                            {"start": 5.0, "end": 10.0, "text": "   "},
                            {"start": 10.0, "end": 15.0, "text": "也有词"}]},
              f, ensure_ascii=False)
gap = vn.export_transcript(BLANK, "srt")
gap_body = open(gap["path"], encoding="utf-8").read()
chk("导出：空段跳过去、序号仍然连续（不占号）",
    "1\n00:00:00,000 --> 00:00:05,000\n有词" in gap_body
    and "2\n00:00:10,000 --> 00:00:15,000\n也有词" in gap_body
    and "\n3\n" not in gap_body and gap["segments"] == 2, gap_body)
OLD_EXP = os.path.join(SANDBOX, "books-export-legacy")
os.makedirs(OLD_EXP)
with open(os.path.join(OLD_EXP, "transcript.txt"), "w", encoding="utf-8") as f:
    f.write("老书的一句\n老书的第二句\n")
untimed = vn.export_transcript(OLD_EXP, "SRT")
untimed_body = open(untimed["path"], encoding="utf-8").read()
chk("导出：老书（没时间戳）导字幕不炸，时间码兜成 0 且绝不出现负号",
    untimed["format"] == "srt" and untimed["timed"] is False
    and "00:00:00,000 --> 00:00:00,000" in untimed_body and "-0" not in untimed_body
    and "老书的一句" in untimed_body, untimed_body[:80])
chk("导出：老书导 Markdown 不塞假时间戳",
    "**[" not in open(vn.export_transcript(OLD_EXP, "md")["path"],
                      encoding="utf-8").read(), None)
sp_srt = vn.export_transcript(EDIT_BOOK, "srt",
                             os.path.join(SANDBOX, "sp.srt"))
sp_vtt = vn.export_transcript(EDIT_BOOK, "vtt",
                             os.path.join(SANDBOX, "sp.vtt"))
chk("导出：说话人 SRT 用方括号、VTT 用语音标记（两家各自的写法）",
    "[甲] 改过的第二句" in open(sp_srt["path"], encoding="utf-8").read()
    and "<v 甲>改过的第二句</v>" in open(sp_vtt["path"], encoding="utf-8").read(), None)
chk("导出：给了 out_path 就落在那儿（前端选「另存为」时用得上）",
    sp_srt["path"] == os.path.join(SANDBOX, "sp.srt")
    and os.path.isfile(sp_srt["path"]), sp_srt["path"])
try:
    vn.export_transcript(book_dir, "ass")
    chk("导出：不会的格式应当抛 ValueError", False, "no-raise")
except ValueError as e:
    chk("导出：不会的格式抛的是中文人话，还列了会哪几种", has_cjk(e), str(e))
except Exception as e:  # noqa: BLE001
    chk("导出：不会的格式抛的是 ValueError", False, repr(e))


# ── 15. 断点续跑：挂了不扔过程产物，重跑不重付一遍钱 ────────────────
use_fakes()
del CALLS["download"][:]
del CALLS["transcribe"][:]
URL_RESUME = "https://www.bilibili.com/video/BVresume0001"
books_resume = os.path.join(SANDBOX, "books-resume")
resume_dir = os.path.join(vn.TMP_ROOT, "bilibili_BVresume0001_p1")
real_import = vn.book_import.import_book


def boom_import(*args, **kwargs):
    raise RuntimeError("假装落盘那一刻挂了")


vn.book_import.import_book = boom_import
crashed = None
try:
    vn.run(URL_RESUME, books_resume, OPTS, None)
except Exception as e:  # noqa: BLE001
    crashed = e
vn.book_import.import_book = real_import
chk("续跑：落盘挂了整条 run 如实报错（不装作成功了）",
    crashed is not None and "挂了" in str(crashed), repr(crashed))
chk("续跑：挂之前下载与转写确实各跑了一遍",
    len(CALLS["download"]) == 1 and len(CALLS["transcribe"]) == 1, CALLS)
chk("续跑：失败时临时目录没被清掉（下好的音频还在）",
    os.path.isdir(resume_dir) and os.path.isfile(os.path.join(resume_dir, "audio.m4a")),
    os.listdir(resume_dir) if os.path.isdir(resume_dir) else None)
staged = json.load(open(os.path.join(resume_dir, "transcript.json"), encoding="utf-8")) \
    if os.path.isfile(os.path.join(resume_dir, "transcript.json")) else {}
chk("保产物：转写成功那一刻就暂存了，AI/落盘挂了也保住",
    staged.get("text", "")[:20] == FAKE_TRANSCRIPT[:20]
    and len(staged.get("segments") or []) == len(FAKE_SEGMENTS), sorted(staged))
resume_notes = []
again = vn.run(URL_RESUME, books_resume, OPTS,
               lambda s, p, n: resume_notes.append((s, n)))
chk("续跑：重跑不再下一遍音频", len(CALLS["download"]) == 1, len(CALLS["download"]))
chk("续跑：重跑不再转一遍（用的上回暂存那份）",
    len(CALLS["transcribe"]) == 1, len(CALLS["transcribe"]))
chk("续跑：进度里说清了是沿用的（不是悄悄少跑一步）",
    any("沿用" in n for _s, n in resume_notes)
    and any(s in ("download", "asr") for s, _n in resume_notes), resume_notes)
chk("续跑：第二次真的落成了书，段落数跟上回一致",
    os.path.isdir(again["book"].get("dir") or "")
    and again["segments"] == len(FAKE_SEGMENTS), again["book"].get("dir"))
chk("续跑：跑成功了临时目录才清干净",
    not os.path.isdir(resume_dir), os.listdir(vn.TMP_ROOT))
vn.book_import.import_book = boom_import
forced = None
try:
    vn.run(URL_RESUME, os.path.join(SANDBOX, "books-resume-off"),
           {**OPTS, "resume": False}, None)
except Exception:  # noqa: BLE001
    forced = True
vn.book_import.import_book = real_import
chk("续跑：resume=False 时老老实实重下一遍、重转一遍",
    forced and len(CALLS["download"]) == 2 and len(CALLS["transcribe"]) == 2, CALLS)
chk("续跑：传给转写器的 resume 跟着选项走（换引擎时不会拿旧缓存糊弄）",
    [c["resume"] for c in CALLS["transcribe"]] == [True, False], CALLS["transcribe"])

task_probe = tempfile.mkdtemp(prefix="gz-stage-", dir=SANDBOX)
vn._stage_transcript(task_probe, {"text": "旧转写", "engine": "fake",
                                 "segments": [{"start": 0.0, "end": 1.0, "text": "旧"}]},
                     {"model": "", "language": "zh"})
chk("暂存：模型/语言对得上就直接用（转写结果没带语言时也认配置那份）",
    vn._staged_transcript(task_probe, {"model": "", "language": "zh"}) is not None, None)
chk("暂存：换了语言的暂存不认（不把两种口径混在一本书里）",
    vn._staged_transcript(task_probe, {"model": "", "language": "en"}) is None, None)
chk("暂存：换了模型的暂存不认",
    vn._staged_transcript(task_probe, {"model": "别的模型", "language": "zh"}) is None, None)
blank_task = tempfile.mkdtemp(prefix="gz-stage-blank-", dir=SANDBOX)
vn._stage_transcript(blank_task, {"text": "", "segments": []}, {"language": "zh"})
chk("暂存：空的暂存不算数（重跑该老实转一遍）",
    vn._staged_transcript(blank_task, {"language": "zh"}) is None, None)

CHUNK = tempfile.mkdtemp(prefix="gz-cloud-part-", dir=SANDBOX)
big_audio = os.path.join(CHUNK, "audio.mp3")
with open(big_audio, "wb") as f:
    f.truncate(vn.CLOUD_CHUNK_BYTES + 8)     # 稀疏文件：只是要让体积过切段阈值
POSTS = []


def fake_split(audio_path, work_dir, seconds=600):
    os.makedirs(work_dir, exist_ok=True)
    out = []
    for i in range(2):
        p = os.path.join(work_dir, "part-%03d.mp3" % i)
        with open(p, "wb") as fp:
            fp.write(b"x" * 32)
        out.append(p)
    return sorted(out)


def fake_post(endpoint, key, model, lang, audio_path, timeout=600):
    index = int(os.path.basename(audio_path)[-7:-5])
    POSTS.append({"model": model, "lang": lang, "part": os.path.basename(audio_path)})
    return {"text": "第 %d 段的文字" % index,
            "segments": [{"start": 0.0, "end": 3.0, "text": "第 %d 段" % index}]}


real_split, real_post, real_seconds = vn._silence_split, vn._post_audio, vn._audio_seconds
real_ensure = vn.ffmpeg_tool.ensure_on_path
vn._silence_split = fake_split
vn._post_audio = fake_post
vn._audio_seconds = lambda path: 10.0
vn.ffmpeg_tool.ensure_on_path = lambda *a, **k: False
# 这一段验的是真 transcribe 的分发与云分片缓存，所以把假件临时换回真的
vn.transcribe = REAL_TRANSCRIBE
CLOUD_CFG = {"engine": "cloud", "model": "m1", "language": "zh",
             "cloud": {"url": "https://example.invalid/v1", "key": "k",
                       "model": "m1"}}
try:
    first = vn.transcribe(big_audio, CLOUD_CFG, None, None, resume=True)
    chk("云分片：第一次两段各传一次，第二段的时间带上前一段的偏移",
        len(POSTS) == 2 and first["segments"][1]["start"] == 10.0
        and first["engine"] == "cloud" and first["language"] == "zh", (len(POSTS), first))
    cached_parts = [p for p in os.listdir(os.path.join(CHUNK, "chunks"))
                    if p.endswith(".asr.json")]
    chk("云分片：每片转好就落一份结果（下次重跑认得出）",
        len(cached_parts) == 2, os.listdir(os.path.join(CHUNK, "chunks")))
    second = vn.transcribe(big_audio, CLOUD_CFG, None, None, resume=True)
    chk("云分片：断点续跑跳过已完成的分片（零新上传）",
        len(POSTS) == 2, len(POSTS))
    chk("云分片：复用之后的文字与时间轴跟第一次完全一致（续跑不会串位）",
        second["text"] == first["text"] and second["segments"] == first["segments"],
        (second["segments"], first["segments"]))
    vn.transcribe(big_audio, {**CLOUD_CFG, "model": "m2"}, None, None, resume=True)
    chk("云分片：换了模型就重传（不拿旧模型的口径凑数）",
        len(POSTS) == 4, len(POSTS))
    vn.transcribe(big_audio, {**CLOUD_CFG, "language": "en"}, None, None, resume=True)
    chk("云分片：换了语言也重传", len(POSTS) == 6, len(POSTS))
    vn.transcribe(big_audio, CLOUD_CFG, None, None, resume=False)
    chk("云分片：resume=False 时不认缓存，老实重转", len(POSTS) == 8, len(POSTS))
    chk("云分片：模型与语言如实传到接口（配置没在半路丢掉）",
        all(p["model"] in ("m1", "m2") and p["lang"] in ("zh", "en") for p in POSTS),
        POSTS[:2])
finally:
    vn._silence_split = real_split
    vn._post_audio = real_post
    vn._audio_seconds = real_seconds
    vn.ffmpeg_tool.ensure_on_path = real_ensure
    vn.transcribe = fake_transcribe


# ── 16. 多 P：取第几 P 由调用方说了算，并写进 meta ──────────────────
def fake_probe_multi(url):
    """假元信息：一条合集，底下三个 P，各有各的标题与时长。"""
    return {"title": "合集・整支", "uploader": "示例UP主", "duration": 100,
            "thumbnail": "https://example.invalid/c.jpg", "id": "BVmulti0001",
            "entries": [{"id": "BVmulti0001", "title": "P1 开篇", "duration": 30,
                         "webpage_url": "https://www.bilibili.com/video/BVmulti0001?p=1"},
                        {"id": "BVmulti0001", "title": "P2 进阶", "duration": 25,
                         "webpage_url": "https://www.bilibili.com/video/BVmulti0001?p=2"},
                        {"id": "BVmulti0001", "title": "P3 答疑", "duration": 45,
                         "webpage_url": "https://www.bilibili.com/video/BVmulti0001?p=3"}]}


use_fakes()
vn.probe_meta = fake_probe_multi
p3 = vn.plan("https://www.bilibili.com/video/BVmulti0001", page=3)
chk("多P：显式指定第 3 P 就下第 3 P（地址与标题都跟着换）",
    p3["page"] == 3 and p3["url"].endswith("p=3") and p3["title"] == "P3 答疑"
    and p3["duration"] == 45, p3)
chk("多P：pages 把三个 P 都列出来（前端选 P 靠它）",
    [x["i"] for x in p3["pages"]] == [1, 2, 3]
    and [x["title"] for x in p3["pages"]] == ["P1 开篇", "P2 进阶", "P3 答疑"], p3["pages"])
chk("多P：只给链接里的 p= 也认（不给显式参数时用地址那个）",
    vn.plan("https://www.bilibili.com/video/BVmulti0001?p=2")["page"] == 2, None)
chk("多P：显式参数比链接里的 p= 更有权威（用户点的那一 P 才算数）",
    vn.plan("https://www.bilibili.com/video/BVmulti0001?p=2", page=3)["page"] == 3, None)
chk("多P：P 数越界不炸，夹到最后一 P",
    vn.plan("https://www.bilibili.com/video/BVmulti0001", page=9)["page"] == 3, None)
chk("多P：page 给 0 / 空值当作没给（回第一 P）",
    vn.plan("https://www.bilibili.com/video/BVmulti0001", page=0)["page"] == 1, None)
use_fakes()
sp = vn.plan("BV1xx411c7mD", page=3)
chk("多P：单 P 视频选第 3 P 时地址补上 p=（最常见的粘贴场景不静默失效）",
    sp["page"] == 3 and sp["url"].endswith("p=3") and sp["url"].count("p=") == 1, sp["url"])
chk("多P：不给 page 时地址不硬塞 p 参数（老行为没变）",
    vn.plan("BV1xx411c7mD")["url"] == "https://www.bilibili.com/video/BV1xx411c7mD"
    and vn.plan("BV1xx411c7mD")["page"] == 1, None)
vn.probe_meta = fake_probe_multi
books_page = os.path.join(SANDBOX, "books-page")
p2run = vn.run("https://www.bilibili.com/video/BVmulti0001", books_page,
               {**OPTS, "page": 2}, None)
p2dir = p2run["book"].get("dir") or ""
p2meta = json.load(open(os.path.join(p2dir, "meta.json"), encoding="utf-8"))
p2trans = json.load(open(os.path.join(p2dir, "transcript.json"), encoding="utf-8"))
chk("多P：run 认 opts.page（书名用的是那一 P 自己的标题）",
    p2meta.get("page") == 2 and p2meta.get("page_title") == "P2 进阶"
    and p2meta.get("page_duration") == 25, (p2meta.get("page"),
                                             p2meta.get("page_title")))
chk("多P：整支的 P 数也记进 meta（书架上能提示还有别的 P）",
    p2meta.get("pages") == 3 and p2meta.get("duration") == 25,
    (p2meta.get("pages"), p2meta.get("duration")))
chk("多P：transcript.json 里也留了第几 P（导出与重建认得到）",
    p2trans.get("page") == 2 and p2trans.get("title") == "P2 进阶", p2trans.get("page"))
key_p1 = vn._task_key(vn.plan("https://www.bilibili.com/video/BVmulti0001", page=1), 1)
key_p2 = vn._task_key(vn.plan("https://www.bilibili.com/video/BVmulti0001", page=2), 2)
key_again = vn._task_key(vn.plan("https://www.bilibili.com/video/BVmulti0001", page=1), 1)
chk("多P：不同 P 各用各的临时目录，同一 P 重跑撞回同一个（续跑才找得到）",
    key_p1 != key_p2 and key_p1 == key_again and key_p1.endswith("_p1"), (key_p1, key_p2))
use_fakes()


# ── 17. 工作台的两样地基：段号与时间写法 ─────────────────────────────
# 为什么单开一节：这一轮的转写工作台整屏都压在这两点上 —— 「改的是哪一段」靠 id 认，
# 起点/终点那两个框靠 parse_stamp 认。它们一旦变形，界面上的表现是
# 「改完存下来字跑到别的段上」和「填了时间筛不出东西」，都属于用户看不见自己错了
# 那一类，所以钉死在这儿，而不是等真机走查碰运气。
seg_ids = [s.get("id") for s in loaded["segments"]]
chk("段号：读出来的每段都有 id，第一句是 s00000",
    all(str(i or "") for i in seg_ids) and seg_ids[0] == "s00000", seg_ids[:3])
chk("段号：id 各不相同（撞号就是改 A 存进 B）",
    len(set(seg_ids)) == len(seg_ids), seg_ids)
chk("段号：同一份文件读两遍 id 不挪位（改到一半刷新不会错对位）",
    [s["id"] for s in vn.load_transcript(book_dir)["segments"]] == seg_ids, None)
chk("段号：筛出来的那段 id 与全量读时同一个（筛着保存才不会把没筛的丢了）",
    vn.load_transcript(book_dir, keyword="第二段")["segments"][0]["id"] == seg_ids[1],
    (vn.load_transcript(book_dir, keyword="第二段")["segments"][0]["id"], seg_ids[1]))
chk("段号：只有 transcript.txt 的老书也现补 id（老书一样进得了编辑器）",
    [s["id"] for s in old["segments"]] == ["s00000", "s00001"], old["segments"])
LEGACY2 = os.path.join(SANDBOX, "legacy-升上来")
shutil.copytree(LEGACY, LEGACY2)
vn.save_transcript(LEGACY2, [{"id": "s00000", "text": "老书第一行改过了"},
                             old["segments"][1]])
up = vn.load_transcript(LEGACY2)
chk("段号：老书存一次就有 transcript.json，下次读 source 变 json",
    up["source"] == "json"
    and [s["id"] for s in up["segments"]] == ["s00000", "s00001"], up["source"])
chk("段号：老书升上来了也不假装有时间戳（第一行还是 None，导出不会编几秒到几秒）",
    up["timed"] is False and up["segments"][0]["start"] is None, up["segments"][0])

STAMPS = [("1:23", 83.0), ("0:05:30", 330.0), ("1:02:03", 3723.0), ("90", 90.0),
          ("  0:45 ", 45.0), ("90.5", 90.5), ("0", 0.0)]
bad_stamps = ["", "   ", "1:2a", "abc", "1:", ":23", "1:2:3:4", "-5", "nan", "1::2"]
chk("时间写法：mm:ss / hh:mm:ss / 纯秒数都认（工作台那两个框就靠这个）",
    all(vn.parse_stamp(k) == v for k, v in STAMPS),
    {k: vn.parse_stamp(k) for k, v in STAMPS if vn.parse_stamp(k) != v})
chk("时间写法：认不住的回 None，不是 0（把打错的当 0 秒会筛出整本，比报错更坏）",
    all(vn.parse_stamp(k) is None for k in bad_stamps),
    {k: vn.parse_stamp(k) for k in bad_stamps if vn.parse_stamp(k) is not None})
chk("时间写法：None / 数字 0 这些边界不抛异常",
    vn.parse_stamp(None) is None and vn.parse_stamp(0) == 0.0, None)

# 编辑器插段时靠一个临时的 after 记锚点，那是界面内部的话，不该留在用户文件里。
AFTER_BOOK = os.path.join(SANDBOX, "books-after", os.path.basename(book_dir))
shutil.copytree(book_dir, AFTER_BOOK)
full = [dict(s) for s in loaded["segments"]]
full[0]["text"] = "这一句是用户手改的"
full[0]["after"] = full[1]["id"]          # 界面挂上去的锚点，保存时该被吃掉
full[1]["speaker"] = "讲师"
vn.save_transcript(AFTER_BOOK, full)
after_doc = json.load(open(os.path.join(AFTER_BOOK, "transcript.json"), encoding="utf-8"))
chk("保存：界面内部的 after 锚点不写进用户的 transcript.json",
    all("after" not in s for s in after_doc["segments"]), after_doc["segments"][0])
chk("保存：没动的段落一个字不丢（这份接口是整本覆盖，所以编辑器必须先凑齐全量）",
    [s["text"] for s in after_doc["segments"]][1:] == [s["text"] for s in full][1:]
    and len(after_doc["segments"]) == len(full),
    [s["text"] for s in after_doc["segments"]][:2])
chk("保存：说话人这类字段留得下来（按人筛与重建章节都要用它）",
    after_doc["segments"][1].get("speaker") == "讲师", after_doc["segments"][1])
chk("保存：手改那句落在原来那段上、id 没挪（改 A 存进 B 就是这一条在管）",
    after_doc["segments"][0]["text"] == "这一句是用户手改的"
    and after_doc["segments"][0]["id"] == seg_ids[0], after_doc["segments"][0])


# ── 真机冒烟：默认不跑，跑了就说实话 ───────────────────────────────
print()
if os.environ.get("GUIZANG_VIDEO_LIVE") != "1":
    print("SKIP  真机冒烟：没设 GUIZANG_VIDEO_LIVE=1，这次没跑（不算通过，也不算失败）")
else:
    import importlib
    importlib.reload(vn)                 # 把上面换掉的假件还原成真函数
    live_model = os.environ.get("GUIZANG_ASR_MODEL") or "mlx-community/whisper-tiny"
    live_url = (os.environ.get("GUIZANG_VIDEO_LIVE_URL")
                or "https://www.youtube.com/watch?v=jNQXAC9IVRw")
    try:
        live_dir = tempfile.mkdtemp(prefix="gz-video-live-", dir=SANDBOX)
        atexit.register(lambda: shutil.rmtree(live_dir, ignore_errors=True))
        info = vn.plan(live_url)
        audio = vn.download_audio(info["url"], live_dir)
        out = vn.transcribe(audio, {"engine": "mlx", "model": live_model,
                                    "language": "en"})
        print("PASS  真机冒烟：%s 下到音频（%.1f KB）并用 mlx-whisper(%s) 转出 %d 字"
              % (info["platform"], os.path.getsize(audio) / 1024.0, live_model,
                 len(out["text"])))
        print("      转写开头：%s" % out["text"][:80].replace("\n", " "))
    except Exception as e:  # noqa: BLE001
        FAIL.append("真机冒烟")
        print("FAIL  真机冒烟：%r" % e)


# ── 收摊 ────────────────────────────────────────────────────────────
print()
print("共 %d 项检查" % len(CHECKS))
if FAIL:
    print("失败 %d 项：%s" % (len(FAIL), "、".join(FAIL)))
    sys.exit(1)
print("全部通过")