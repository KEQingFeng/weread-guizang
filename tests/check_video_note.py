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
    return path


def fake_transcribe(audio_path, asr_cfg, progress=None, should_stop=None):
    if progress:
        progress("asr", 60, "假转写中")
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