# -*- coding: utf-8 -*-
"""视频转笔记：一条链接 → 音频 → 转写 → 结构化摘要 / 知识笔记 / 思维导图 → 一本书。

为什么单独一个文件：这条线和「取书」「剪藏」「订阅」都不一样 —— 它要吃视频站点的
音频，要跑本地 ASR（mlx-whisper / faster-whisper）或云 ASR，还要三次 LLM 调用才
能落出一本书。塞进 clip_article 的话，那个文件「一次抓一篇网页」的假设会被整条
音视频流水线撑破。

整条流水线跑在调用方进程里，不起后台线程、不起守护进程：界面那边照旧用子进程拉起
（见 ui_server 的 script()），中止靠 opts 里的 should_stop 在阶段之间查一次。
下载器 / 转写器 / LLM 调用是三处可替换的模块级函数（probe_meta / download_audio /
transcribe / llm_chat），自测套件靠换掉它们做到不联网、不下模型。

progress(stage, pct, note) 的 stage 用固定英文键，note 是给人看的中文，界面按
stage 分组、按 pct 画进度条：

  plan      0-5    认链接、取元信息
  download  5-45   下音频（pct 随字节走）
  asr       45-75  转写
  summary   75-82  结构化摘要
  note      82-88  知识笔记
  mindmap   88-95  思维导图
  save      95-100 落盘成书

任何一次 LLM 调用失败都不算整条线失败：转写照存、书照进书架，只是 AI 那部分缺席，
回来的结果里带 ai_error 说明原因。音频下不来或转写不出东西才是真的失败（抛异常）。

转写落三份文件：transcript.json（带时间戳的段落，编辑改的是它）、transcript.txt
（纯文本视图）、merged.md（整本书一份 Markdown）。围绕它们另有一组给界面调的函数：
load_transcript 读（老书只有 txt 就从 txt 兜底，时间戳给空）、save_transcript 原子写回、
rebuild_book 按改后的转写重建章节与笔记（旧文件先整体挪进 .bak-时间戳/，可回退）、
export_transcript 导成 srt / vtt / txt / md / json。

跑挂了不扔过程产物：临时目录按「视频编号 + 第几 P」定名，重跑时已经下好的音频、
已经转好的分片和整份转写都直接复用（opts 的 resume 给 False 就从头重做）。

不 import ui_server：LLM 三件套（url / key / model）由调用方从 agent_cfg() 取了传进来。
"""

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid

import book_import
import book_notes
import ffmpeg_tool
import media_setup as ms
import platform_compat as pc

REPO = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = pc.data_dir(REPO)

# 子进程 → 服务交回结构化结果的行前缀：`##GUIZANG## {json}`。
# 为什么要有这条约定：ui_server 只能拿到子进程的退出码，而一次视频转笔记要回来的
# 东西不止「成没成」——还有书 id、字数、哪台引擎转的、AI 那部分缺没缺。退出码只有
# 0-255 一个数，装不下这些；日志行又会被用户读，不适合当接口。
# 定义在这里、由 ui_server 引用，改一处两边同时生效（别在别处再写一遍字面量）。
RESULT_MARK = "##GUIZANG## "
# 音频临时目录：跟 cache/ 走（.gitignore 挡掉的那一片），装成 app 时落在用户目录、
# 应用包之外；一本书一个子目录，跑完就删。
TMP_ROOT = os.path.join(DATA_DIR, "cache", "video")

# 浏览器 UA：B 站对非浏览器 UA 的视频流会收紧。这里只取音频，不伪装登录态。
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
BILIBILI_HEADERS = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}

# 模型名单一真源在 media_setup（那边还负责下它、报告它到没到），这里只取来用，
# 免得「装的时候按 A 下、跑的时候按 B 找」这种两处各写一遍的错。
DEFAULT_MLX_MODEL = ms.ENGINES["mlx"]["model"]
DEFAULT_FASTER_MODEL = ms.ENGINES["faster"]["model"]        # CTranslate2 权重短名，不是 HF repo id
DEFAULT_CLOUD_MODEL = "whisper-1"
DEFAULT_LANGUAGE = "zh"

# 云 ASR 单次请求的体积上限：再大就按段切开分别转（ffmpeg 切 10 分钟一段）。
CLOUD_CHUNK_BYTES = 24 * 1024 * 1024

# 思维导图的一级分支下限：低于这个数就是模型没按契约来，先修一次再退回本地兜底。
MIN_MINDMAP_BRANCHES = 3
MINDMAP_TYPES = ("root", "theme", "topic", "leaf")

# 正文写盘/正文长度上限：转录上万字的视频也够用，纯防跑飞。
MAX_TRANSCRIPT_CHARS = 400000
LLM_INPUT_LIMIT = 24000                  # 单次喂给 LLM 的转写节选上限（字符）

# 转写产物的三份文件：transcript.json 是带时间戳的那一份真相（编辑器改的就是它），
# transcript.txt 是它的纯文本视图（老书只有这一份，读的时候拿它兜底），
# summary.json 里还留着章节表，重建章节时按它来切。
TRANS_FILE = "transcript.json"
TRANS_TXT = "transcript.txt"
SUMMARY_FILE = "summary.json"
# 导出物放进一个子目录：不跟 transcript.txt / transcript.json 抢名字（那两个另有用途），
# 而且是纯派生物，删了随时能再生成。
EXPORT_DIR = "exports"
EXPORT_EXT = {"srt": "srt", "vtt": "vtt", "txt": "txt", "md": "md", "json": "json"}
TRANS_SCHEMA = 1
MAX_SEGMENTS = 20000                     # 段数上限：正常视频用不满，防传进来的数据跑飞
MAX_SEG_CHARS = 4000                     # 单段文字上限（跟笔记划线的量级一致）

_BVID = re.compile(r"(BV[0-9A-Za-z]+)", re.IGNORECASE)
_YT_ID = re.compile(r"^[A-Za-z0-9_-]{6,20}$")


class Aborted(Exception):
    """调用方通过 should_stop 要求中止时抛这个（阶段之间才查，手头这一段做完就停）。"""


# ─────────────────────────── 基础 ───────────────────────────

def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _clip(text, limit):
    s = "" if text is None else str(text)
    return s[:limit]


def _sec(value):
    """秒数收敛成 float：脏值 / None / 负数 / -0.0 / 无穷都当 0 处理。

    为什么单独一个函数：时间戳从三条路来（ASR 给的 float、LLM 给的 int 秒、用户在
    编辑器里手打的字符串），任何一条给了怪值都不能把导出或落盘带崩，也不能让
    `-0.0` 混进时间轴 —— 那玩意格式化出来是 `-00:00:00,000`，字幕播放器直接拒收。
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(out) or out <= 0:      # <= 0 顺手把 -0.0 也归零
        return 0.0
    return out


def _maybe_sec(value):
    """同上，但允许「没有时间」：老书从 transcript.txt 兜底出来的段落就是 None。"""
    if value is None or value == "":
        return None
    try:
        float(value)
    except (TypeError, ValueError):
        return None
    return _sec(value)


def parse_stamp(value):
    """把「1:23」「0:05:30」「90」「90.5」这一类时间收成秒；收不住回 None。

    为什么要有它：工作台筛转写时，人会在框里打 mm:ss，也可能直接打秒数。
    None 的意思是「这一侧不设条件」，不是 0 —— 把打错的「1:2a」当成 0 秒，
    筛出来的是整本书，那比报一句「没看懂这个时间」更坏。
    """
    s = str(value if value is not None else "").strip()
    if not s:
        return None
    if ":" in s:
        parts = [p.strip() for p in s.split(":")]
        if len(parts) > 3 or not all(parts):
            return None
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            return None
        if any(not math.isfinite(n) or n < 0 for n in nums):
            return None
        total = 0.0
        for n in nums:
            total = total * 60 + n
        return total if math.isfinite(total) else None
    try:
        n = float(s)
    except ValueError:
        return None
    return n if math.isfinite(n) and n >= 0 else None


def _stamp(value):
    """秒 → HH:MM:SS（导图锚点、md 导出的时间戳用）。"""
    total = int(_sec(value))
    return "%02d:%02d:%02d" % (total // 3600, (total % 3600) // 60, total % 60)


def _cue_time(value, sep=","):
    """秒 → 字幕时间码：SRT 用 HH:MM:SS,mmm，VTT 用 HH:MM:SS.mmm。

    毫秒必须是整数（两家规范都写死 3 位），所以先四舍五入成总毫秒再拆，不拿
    浮点格式化去凑 —— 那样偶尔会甩出 6 位小数。
    """
    total = int(round(_sec(value) * 1000.0))
    hours, rest = divmod(total, 3600000)
    minutes, rest = divmod(rest, 60000)
    seconds, millis = divmod(rest, 1000)
    return "%02d:%02d:%02d%s%03d" % (hours, minutes, seconds, sep, millis)


def _join_text(segments):
    """段落 → 纯文本视图（transcript.txt 的内容）。"""
    return "\n".join(str((s or {}).get("text") or "").strip()
                     for s in (segments or [])
                     if str((s or {}).get("text") or "").strip())


def _say(progress, stage, pct, note):
    """回报进展。回调本身出问题不该把整条流水线带走。"""
    if not progress:
        return
    try:
        progress(stage, pct, note)
    except Exception:
        pass


def _check_stop(opts):
    fn = (opts or {}).get("should_stop")
    if callable(fn):
        try:
            hit = bool(fn())
        except Exception:
            hit = False
        if hit:
            raise Aborted("已经停下来了，音频和转写下来之前的内容都还在")


def _importable(name):
    try:
        import importlib.util
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _agent_cfg_from_disk():
    """从应用自己的 config.json 读那三件套 —— 只给 available() 报状态用。

    真正的调用配置由调用方通过 opts 传进来（见模块开头），这里不越权、不缓存。
    """
    path = os.path.join(DATA_DIR, "cache", "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return "", "", ""
    if not isinstance(cfg, dict):
        return "", "", ""
    return ((cfg.get("agent_url") or "").strip(),
            (cfg.get("agent_key") or "").strip(),
            (cfg.get("agent_model") or "").strip())


def available():
    """这条流水线现在能走到哪一步。永远不抛 —— 界面拿它显示状态，抛了就变「点了没反应」。

    asr.cloud 报的是「云转写这条路在代码里可用」（地址在 run 的 opts 里给），
    不代表用户已经配过；llm 则看配置文件里到底填没填地址和模型。
    """
    out = {"ytdlp": False, "ffmpeg": False,
           "asr": {"local": False, "cloud": True}, "llm": False, "engines": []}
    try:
        out["ytdlp"] = _importable("yt_dlp")
    except Exception:
        pass
    try:
        out["ffmpeg"] = bool(ffmpeg_tool.status().get("found"))
    except Exception:
        pass
    try:
        engines = []
        if _importable("mlx_whisper"):
            engines.append("mlx")
        if _importable("faster_whisper"):
            engines.append("faster")
        engines.append("cloud")
        out["engines"] = engines
        out["asr"]["local"] = ("mlx" in engines) or ("faster" in engines)
    except Exception:
        pass
    # 引擎「装了没」与模型「下齐了没」是两件事：引擎几十 MB 随安装就位，模型要几百 MB
    # 到 1.6GB，进门之后才在后台下。界面那几盏灯分开报，用户才知道该等什么。
    try:
        out["engine_ready"] = ms.engine_ready()
        out["model"] = ms.model_id()
        out["model_ready"] = ms.model_ready()
    except Exception:
        pass
    try:
        url, _key, model = _agent_cfg_from_disk()
        out["llm"] = bool(url and model)
    except Exception:
        pass
    return out


# ─────────────────────────── 链接归一 ───────────────────────────

def normalize_url(value):
    """把用户粘的任何东西收敛成 (platform, vid, url, page)。

    B 站认 BV 号（粘整段带文字的分享也认），YouTube 认 youtu.be / watch?v= /
    shorts。p=N 分 P 参数原样带过去。认不出来返回 platform=""，由 plan 报人话。
    """
    raw = (value or "").strip().strip("\"'“”‘’《》<>")
    if not raw:
        return "", "", "", None

    hit = _BVID.search(raw)
    if hit:
        bvid = hit.group(1)
        url = "https://www.bilibili.com/video/%s" % bvid
        page = _bilibili_page(raw)
        if page:
            url += "?%s" % urllib.parse.urlencode({"p": page})
        return "bilibili", bvid, url, page

    try:
        parsed = urllib.parse.urlparse(raw)
    except ValueError:
        parsed = None
    if parsed is not None:
        host = (parsed.netloc or "").lower()
        path = parsed.path or ""
        if host.endswith("youtu.be"):
            vid = path.strip("/").split("/", 1)[0]
            if _YT_ID.match(vid):
                return "youtube", vid, "https://www.youtube.com/watch?v=%s" % vid, None
        if "youtube.com" in host:
            if path == "/watch":
                vid = (urllib.parse.parse_qs(parsed.query).get("v") or [""])[0].strip()
                if _YT_ID.match(vid):
                    return "youtube", vid, "https://www.youtube.com/watch?v=%s" % vid, None
            if path.startswith("/shorts/"):
                # 只切前几段：短链后面可能还跟着 ?feature=share 之类
                parts = path.split("/", 3)
                vid = parts[2] if len(parts) > 2 else ""
                if _YT_ID.match(vid):
                    return "youtube", vid, "https://www.youtube.com/watch?v=%s" % vid, None
        if host.endswith("b23.tv"):
            # 短链要跳一次才知道 BV 号，交给 yt-dlp 自己去跟；这里只认平台。
            return "bilibili", "", raw, None
    return "", "", raw, None


def _bilibili_page(value):
    """分 P 号：先用 `[?&]p=数字` 抠（粘整段分享文字时只有这条路靠得住），
    再退回解析查询串。两条路都要把值裁成纯数字 —— 整段文字粘进来时 p 的值后面
    会跟着「 挺好的」这种尾巴，直接 int() 会失败，分 P 就悄悄丢了。
    """
    if not _BVID.search(value or "") and not re.search(r"(bilibili\.com|b23\.tv)",
                                                      value or "", re.IGNORECASE):
        return None
    hit = re.search(r"[?&]p=(\d+)", value or "", re.IGNORECASE)
    if hit:
        return int(hit.group(1)) or None
    try:
        page = (urllib.parse.parse_qs(urllib.parse.urlparse(value).query).get("p")
                or [None])[0]
    except ValueError:
        page = None
    digits = re.match(r"\d+", str(page)) if page is not None else None
    if not digits:
        return None
    number = int(digits.group(0))
    return number if number > 0 else None


# ─────────────────────────── yt-dlp ───────────────────────────

def _ydl_options(extra=None):
    """yt-dlp 的公共选项（照抄 bilisum 的稳健参数：重试、超时、UA、Referer）。"""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "http_headers": dict(BILIBILI_HEADERS),
        "retries": 3,
        "extractor_retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
    }
    opts.update(extra or {})
    return opts


def _ydl_class():
    try:
        from yt_dlp import YoutubeDL
    except Exception as e:
        raise ValueError("这台机器上还没装 yt-dlp，先跑一次安装（pip install yt-dlp）") from e
    return YoutubeDL


def _ydl_error(err):
    """yt-dlp 的报错翻成用户看得懂的话，别把 traceback 甩给用户。"""
    text = str(err)
    if "HTTP Error 412" in text and "ili" in text.lower():
        return ValueError("B 站把这次请求拦了（412 风控）。过一会儿再试，"
                          "或者换个网络；要登录才给看的视频得先自己下好再导进来。")
    if "Sign in to confirm" in text or "age" in text.lower() and "restricted" in text.lower():
        return ValueError("这个视频要登录或年龄确认，归藏这边取不到它的音频。")
    return ValueError("取视频信息没成：%s" % _clip(text, 160))


def probe_meta(url):
    """只取元信息，不下载。返回 yt-dlp 的 info dict（多 P 时带 entries）。"""
    YoutubeDL = _ydl_class()
    opts = _ydl_options({"noplaylist": False, "skip_download": True})
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise _ydl_error(e) from e
    if not isinstance(info, dict):
        raise ValueError("这个链接没读出视频信息，可能不是视频页")
    return info


def download_audio(url, task_dir, progress=None, should_stop=None):
    """下音频到 task_dir，返回文件路径。

    format 优先 m4a —— 多数 ASR 引擎（mlx / faster-whisper 的 PyAV 解码）直接吃它，
    不需要 ffmpeg 转码，所以 ffmpeg 没装时这条线照样能跑。ffmpeg 装着就顺手告诉
    yt-dlp 它在哪，合并流时用得上。
    """
    YoutubeDL = _ydl_class()
    os.makedirs(task_dir, exist_ok=True)
    outtmpl = os.path.join(task_dir, "audio.%(ext)s")

    def hook(data):
        if should_stop and callable(should_stop):
            try:
                if should_stop():
                    raise Aborted("已经停下来了")
            except Aborted:
                raise
            except Exception:
                pass
        if (data or {}).get("status") != "downloading":
            return
        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        got = int(data.get("downloaded_bytes") or 0)
        if total:
            pct = 5 + int(40 * min(1.0, float(got) / float(total)))
        else:
            pct = 5 + min(38, int(math.log10(max(got, 1)) * 5))
        _say(progress, "download", pct, "正在取音频 %.1f MB" % (got / 1048576.0))

    opts = _ydl_options({
        "format": "bestaudio[ext=m4a]/bestaudio",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "progress_hooks": [hook],
    })
    try:
        opts["ffmpeg_location"] = ffmpeg_tool.ffmpeg_path()
    except ValueError:
        pass                              # 没有 ffmpeg 也没关系，m4a 直接给 ASR 解码
    try:
        with YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Aborted:
        raise
    except Exception as e:
        raise _ydl_error(e) from e
    hits = [os.path.join(task_dir, n) for n in sorted(os.listdir(task_dir))
            if n.startswith("audio.") and not n.endswith(".part")]
    if not hits:
        raise ValueError("音频没下下来，可能这个视频对未登录用户不开放")
    _say(progress, "download", 45, "音频就绪")
    return hits[0]


# ─────────────────────────── 计划 ───────────────────────────

def plan(url, page=None):
    """认链接 + 只取元信息（不下载）。返回平台、vid、标题、作者、时长、封面、分 P 表。

    认不出的链接抛 ValueError，话说成用户能懂的样子 —— 他粘过来的可能是错的链接，
    也可能粘的是 B 站首页，得让他知道该粘什么。

    page：显式指定取第几 P（界面上选了哪一 P 就传哪一 P）。链接自带的 p= 只在没显式
    指定时才生效 —— 用户点的那一 P 比地址栏里可能过期的参数更有权威。
    """
    raw = (url or "").strip()
    if not raw:
        raise ValueError("还没粘链接呢，把 B 站或 YouTube 的视频地址发过来")
    platform, vid, normalized, parsed_page = normalize_url(raw)
    if not platform:
        raise ValueError("这个链接归藏还不认识，现在只收 B 站和 YouTube 的视频。"
                         "B 站粘 BV 号也行（比如 BV1xx411c7mD）。")
    if not vid and "b23.tv" not in normalized:
        raise ValueError("这个链接里没找到视频编号，换视频页上那条完整地址再试")
    want = _as_int(page) or parsed_page
    want = max(1, want) if want else None

    info = probe_meta(normalized)
    entries = [e for e in (info.get("entries") or []) if isinstance(e, dict)]
    title = str(info.get("title") or "").strip()
    uploader = str(info.get("uploader") or info.get("channel") or
                   info.get("artist") or "").strip()
    cover = str(info.get("thumbnail") or "").strip()
    duration = _as_int(info.get("duration"))

    pages = []
    if entries:
        for i, entry in enumerate(entries, start=1):
            pages.append({
                "i": i,
                "title": str(entry.get("title") or ("P%d" % i)).strip(),
                "vid": str(entry.get("id") or "").strip(),
                "url": str(entry.get("webpage_url") or entry.get("url") or "").strip(),
                "duration": _as_int(entry.get("duration")) or 0,
            })
        first = entries[0]
        if str(first.get("webpage_url") or "").strip():
            # 多 P：真正要下的是用户点的那一 P（page 缺省就是第一 P）
            pick = min(max((want or 1) - 1, 0), len(entries) - 1)
            target = entries[pick]
            normalized = _page_url((target.get("webpage_url") or normalized).strip(),
                                   want or 1)
            want = pick + 1
            title = str(target.get("title") or title).strip()
            duration = _as_int(target.get("duration")) or duration
            cover = str(target.get("thumbnail") or cover).strip()
    else:
        # 单 P（或 yt-dlp 没给出 entries）：这一 P 的信息就是整支视频的，地址补 p=
        normalized = _page_url(normalized, want) if want else normalized
        pages.append({"i": want or 1, "title": title, "vid": vid,
                      "url": normalized, "duration": duration or 0})

    if not title:
        title = vid or "未命名视频"
    return {"platform": platform, "vid": vid, "url": normalized, "title": title,
            "uploader": uploader, "duration": duration, "cover": cover,
            "pages": pages, "count": len(pages), "page": want or 1}


def _page_url(url, page):
    """给地址补上 p=N：已有的 p 先摘掉，免得留下两个互相打架的参数。

    为什么绕这一圈而不是让 yt-dlp 自己按 p 取：多 P 时选中的那一 P 的地址本来就在
    entries 里（已带 p=），但单 P 的链接（裸 BV 号）没有 —— 不补这一步，「取第 3 P」
    这个参数在最常见的粘贴场景下会静默失效，用户拿到的永远是第一 P。
    """
    if not page:
        return url
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query)
             if k.lower() != "p"]
    query.append(("p", str(int(page))))
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path,
                                    urllib.parse.urlencode(query), parts.fragment))



def _as_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


# ─────────────────────────── 转写 ───────────────────────────

def _is_apple_silicon():
    if not pc.IS_MAC:
        return False
    try:
        return os.uname().machine in ("arm64", "aarch64")
    except Exception:
        return False


def pick_engine(want, asr_cfg):
    """选转写引擎。

    auto 的偏好顺序：Apple 芯片且 mlx-whisper 能导入 → mlx（最快，且吃 m4a 不用
    ffmpeg）；否则 faster-whisper（CPU int8，哪台机器都能跑）；再否则云转写（用户
    自己在 opts 里填了地址才算数）。用户点名要的那个装不上时直接报人话，不偷偷换 ——
    「我要用大模型」被静默降级成小模型，比报错更让人火大。
    """
    want = (want or "auto").strip().lower()
    cloud_ok = bool(((asr_cfg or {}).get("cloud") or {}).get("url"))
    if want == "auto":
        if _is_apple_silicon() and _importable("mlx_whisper"):
            return "mlx"
        if _importable("faster_whisper"):
            return "faster"
        if cloud_ok:
            return "cloud"
        raise ValueError("这台机器上没找到能用的转写引擎。装一个 mlx-whisper 或 "
                         "faster-whisper，或者把云转写的地址填上再试。")
    if want == "mlx":
        if not _importable("mlx_whisper"):
            raise ValueError("点名要用 mlx-whisper，但它还没装（pip install mlx-whisper），"
                             "或者把引擎改成 auto / faster。")
        if not _is_apple_silicon():
            raise ValueError("mlx-whisper 只在 Apple 芯片上跑得起来，这台机器用不了，"
                             "把引擎改成 faster 或 auto。")
        return "mlx"
    if want == "faster":
        if not _importable("faster_whisper"):
            raise ValueError("点名要用 faster-whisper，但它还没装（pip install faster-whisper）。")
        return "faster"
    if want == "cloud":
        if not cloud_ok:
            raise ValueError("要用云转写，先在设置里把地址（和 Key）填上。")
        return "cloud"
    raise ValueError("转写引擎只认 auto / mlx / faster / cloud 这几种")


def _asr_model_error(err, model):
    """模型拉不下来是最常见的一次性错误，光甩一句 HTTP 报错用户没法自救。"""
    text = str(err)
    low = text.lower()
    looks_download = any(k in low for k in
                         ("max retries", "connection", "timed out", "ssl", "huggingface",
                          "can't load", "cannot load", "failed to download", "resolve",
                          "offline", "404", "connect"))
    if looks_download and not (os.environ.get("HF_ENDPOINT") or "").strip():
        return ValueError(
            "转写模型没能下下来（%s）。模型是第一次用时从 Hugging Face 取的："
            "挂个代理再试，或者设 HF_ENDPOINT=https://hf-mirror.com 走国内镜像，"
            "也可以先在别处下好、用 asr.model 指到本地目录。"
            % _clip(text, 120))
    return ValueError("转写出错了：%s" % _clip(text, 160))


def _pack_result(result):
    """把 mlx/faster/云三家的返回收敛成 {"text", "segments":[{start,end,text}]}。"""
    segments = []
    for item in (result or {}).get("segments") or []:
        text = str((item or {}).get("text") or "").strip()
        segments.append({"start": float((item or {}).get("start") or 0.0),
                         "end": float((item or {}).get("end") or 0.0),
                         "text": text})
    text = str((result or {}).get("text") or "").strip()
    if not text:
        text = "".join(s["text"] for s in segments).strip()
    if len(text) > MAX_TRANSCRIPT_CHARS:
        text = text[:MAX_TRANSCRIPT_CHARS]
    return {"text": text, "segments": segments}


def transcribe(audio_path, asr_cfg, progress=None, should_stop=None, resume=False):
    """音频 → 文字。返回 {"text", "segments", "engine", "language"}。

    这里只是分发：真正干活的是下面三个函数，自测套件把 transcribe 整个换掉就行。

    resume=True 时，云转写的分片结果会落盘复用（见 _part_cache），重跑不再传已经转
    好的那些段；本地引擎（mlx / faster）一次调用吃整段音频，没有分片可跳，续跑靠
    run() 那份转写暂存。
    """
    asr_cfg = asr_cfg or {}
    engine = pick_engine(asr_cfg.get("engine"), asr_cfg)
    model = (asr_cfg.get("model") or "").strip()
    lang = (asr_cfg.get("language") or "").strip() or DEFAULT_LANGUAGE
    # mlx-whisper 抽音轨时 shell 的是裸 'ffmpeg'，而归藏下的那份在 cache/tools/ 里、
    # 不在 PATH 上 —— 不补这一步，组件装好了也照样报 No such file or directory。
    ffmpeg_tool.ensure_on_path()
    _say(progress, "asr", 47, "开始转写（%s）" % engine)
    if engine == "mlx":
        out = _transcribe_mlx(audio_path, model, lang, progress)
    elif engine == "faster":
        out = _transcribe_faster(audio_path, model, lang, progress)
    else:
        out = _transcribe_cloud(audio_path, asr_cfg.get("cloud") or {}, model, lang,
                                progress, should_stop, resume=resume)
    out["engine"] = engine
    out["language"] = lang
    if not out.get("text"):
        raise ValueError("这段音频里没转出文字，换一集或者换个人声清楚的视频再试")
    _say(progress, "asr", 75, "转写完成，共 %d 字" % len(out["text"]))
    return out


def _transcribe_mlx(audio_path, model, lang, progress):
    tool = model or DEFAULT_MLX_MODEL
    _say(progress, "asr", 48, "正在加载转写模型（首次会自动下 %s）" % tool)
    try:
        import mlx_whisper
    except Exception as e:
        raise ValueError("没装上 mlx-whisper（pip install mlx-whisper），"
                         "或者把引擎改成 faster。") from e
    try:
        result = mlx_whisper.transcribe(audio_path, path_or_hf_repo=tool, language=lang)
    except Exception as e:
        raise _asr_model_error(e, tool) from e
    _say(progress, "asr", 72, "转写完成")
    return _pack_result(result)


def _transcribe_faster(audio_path, model, lang, progress):
    name = model or DEFAULT_FASTER_MODEL
    _say(progress, "asr", 48, "正在加载转写模型 %s（CPU int8，慢慢来）" % name)
    try:
        from faster_whisper import WhisperModel
    except Exception as e:
        raise ValueError("没装上 faster-whisper（pip install faster-whisper）。") from e
    try:
        engine = WhisperModel(name, device="cpu", compute_type="int8")
        raw, _info = engine.transcribe(audio_path, language=lang, vad_filter=True)
        # 这是个生成器：必须在这里面把它跑完，出了 try 再取会丢掉异常上下文。
        segments = [{"start": float(getattr(s, "start", 0.0) or 0.0),
                     "end": float(getattr(s, "end", 0.0) or 0.0),
                     "text": (getattr(s, "text", "") or "").strip()} for s in raw]
    except Exception as e:
        raise _asr_model_error(e, name) from e
    _say(progress, "asr", 72, "转写完成")
    return _pack_result({"segments": segments, "text": ""})


def _transcriptions_url(raw):
    """把用户填的地址补成 /audio/transcriptions 端点（跟 ui_server 补
    /chat/completions 是同一套路数：允许只填到域名或 /v1）。"""
    url = (raw or "").strip().rstrip("/")
    if not re.match(r"^https?://", url):
        return ""
    if url.endswith("/audio/transcriptions"):
        return url
    if url.endswith("/audio"):
        return url + "/transcriptions"
    if re.search(r"/v\d+$", url):
        return url + "/audio/transcriptions"
    return url + "/v1/audio/transcriptions"


def _multipart(fields, file_field, file_name, blob):
    """拼一份 multipart/form-data（标准库就够，不用 curl 也不用 requests）。"""
    boundary = "----guizang" + uuid.uuid4().hex
    out = []
    for key, value in fields.items():
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                    % (boundary, key, value)).encode("utf-8"))
    out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n"
                % (boundary, file_field, file_name)).encode("utf-8"))
    out.append(blob)
    out.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    return b"".join(out), "multipart/form-data; boundary=%s" % boundary


def _post_audio(endpoint, key, model, lang, audio_path, timeout=600):
    """把一段音频 POST 给 OpenAI 兼容的 /audio/transcriptions，失败重试三次。"""
    with open(audio_path, "rb") as f:
        blob = f.read()
    body, ctype = _multipart(
        {"model": model, "language": lang, "response_format": "verbose_json"},
        "file", os.path.basename(audio_path), blob)
    last = None
    for attempt in range(3):
        req = urllib.request.Request(
            endpoint, data=body, method="POST",
            headers={"Content-Type": ctype, "Accept": "application/json",
                     "Authorization": "Bearer " + (key or "-"),
                     "User-Agent": "guizang/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            payload = json.loads(raw)
            if isinstance(payload, dict):
                return payload
            last = ValueError("云转写回的格式不对")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            if e.code in (401, 403):
                raise ValueError("云转写不给进（HTTP %s）：Key 不对或者这个模型没权限。%s"
                                 % (e.code, detail))
            last = ValueError("云转写回了 %s：%s" % (e.code, detail))
        except Exception as e:
            last = ValueError("连不上云转写地址：%s" % _clip(e, 160))
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))
    raise last or ValueError("云转写没成")


def _silence_split(audio_path, work_dir, seconds=600):
    """音频太大时按段切开（云接口单次有体积上限）。利用 ffmpeg 的分段器。"""
    try:
        exe = ffmpeg_tool.ffmpeg_path()
    except ValueError as e:
        raise ValueError("音频太大，云转写得分段上传，切段要用 ffmpeg —— "
                         "点「装组件」让归藏自己下一份，或者先装一个放进 PATH。") from e
    os.makedirs(work_dir, exist_ok=True)
    pattern = os.path.join(work_dir, "part-%03d.mp3")
    argv = [exe, "-y", "-hide_banner", "-loglevel", "error", "-i", audio_path,
            "-f", "segment", "-segment_time", str(seconds), "-c:a", "libmp3lame",
            "-b:a", "96k", pattern]
    done = subprocess.run(argv, capture_output=True, text=True, errors="replace",
                          timeout=3600)
    if done.returncode != 0:
        raise ValueError("音频切段没成：%s" % _clip((done.stderr or "").strip(), 140))
    return sorted(os.path.join(work_dir, n) for n in os.listdir(work_dir)
                  if n.startswith("part-") and n.endswith(".mp3"))


def _part_cache_path(part_path):
    """分片转写结果的暂存文件名：贴在分片旁边，删临时目录时一起删干净。"""
    return part_path + ".asr.json"


def _part_cached(part_path, model, lang):
    """这一片上次转过了吗。模型或语言换了就不算 —— 拿旧结果续跑会把两种口径混在一起。"""
    doc = _read_json(_part_cache_path(part_path))
    if not isinstance(doc, dict) or not str(doc.get("text") or "").strip():
        return None
    if str(doc.get("model") or "") != str(model or ""):
        return None
    if str(doc.get("language") or "") != str(lang or ""):
        return None
    return {"text": str(doc.get("text") or ""),
            "segments": doc.get("segments") if isinstance(doc.get("segments"), list) else []}


def _transcribe_cloud(audio_path, cloud, model, lang, progress, should_stop,
                      resume=False):
    endpoint = _transcriptions_url(cloud.get("url"))
    if not endpoint:
        raise ValueError("云转写的地址要写成 http:// 或 https:// 开头")
    name = model or (cloud.get("model") or "").strip() or DEFAULT_CLOUD_MODEL
    key = (cloud.get("key") or "").strip()
    parts = [audio_path]
    if os.path.getsize(audio_path) > CLOUD_CHUNK_BYTES:
        _say(progress, "asr", 50, "音频较大，正在切段")
        parts = _silence_split(audio_path, os.path.join(os.path.dirname(audio_path),
                                                        "chunks"))
    segments, pieces, offset = [], [], 0.0
    for i, part in enumerate(parts, start=1):
        if should_stop and callable(should_stop) and should_stop():
            raise Aborted("已经停下来了")
        cached = _part_cached(part, name, lang) if resume else None
        if cached is not None:
            # 断点续跑：这一片上次已经转好了，直接复用 —— 一段一段传一次要几十秒，
            # 重跑不该把已经付过的钱再付一遍、等的时间再等一遍。
            _say(progress, "asr", 50 + int(20.0 * i / max(len(parts), 1)),
                 "第 %d/%d 段沿用上次的转写" % (i, len(parts)))
            payload = cached
        else:
            _say(progress, "asr", 50 + int(20.0 * (i - 1) / max(len(parts), 1)),
                 "正在上传第 %d/%d 段" % (i, len(parts)))
            payload = _post_audio(endpoint, key, name, lang, part)
            _write_json(_part_cache_path(part),
                        {"model": name, "language": lang,
                         "text": str(payload.get("text") or "").strip(),
                         "segments": _pack_result(payload)["segments"],
                         "saved_at": _now()})
        pieces.append(str(payload.get("text") or "").strip())
        for seg in (payload.get("segments") or []):
            if not isinstance(seg, dict):
                continue
            segments.append({"start": float(seg.get("start") or 0.0) + offset,
                             "end": float(seg.get("end") or 0.0) + offset,
                             "text": str(seg.get("text") or "").strip()})
        offset += _audio_seconds(part)
    _say(progress, "asr", 72, "转写完成")
    return _pack_result({"segments": segments, "text": "\n".join(p for p in pieces if p)})


def _audio_seconds(path):
    seconds = ffmpeg_tool.probe_duration(path)
    return float(seconds) if seconds else 0.0


# ─────────────────────────── LLM ───────────────────────────

# 三段提示词从 bilisum 移植（packages/infra/.../config.py 的 DEFAULT_SUMMARY_*、
# DEFAULT_KNOWLEDGE_NOTE_*、DEFAULT_MINDMAP_*，以及 real.py 里 mindmap 那段
# 「至少 3 个一级分支」的硬性验收）。按归藏这边的落地方式做了三处裁剪：
#   · 摘要只留 title / overview / bulletPoints / chapters，去掉它的 chapterGroups
#     （归藏的目录就是 chapters 这一层，多一级没有落点）；
#   · 思维导图 schema 收敛成它的 {version,title,root,nodes[]}，节点字段就是我们
#     要的那六个，去掉了它的 source_chapter_* / 图文截图 / 知识库那一路；
#   · 加了「时间点用秒数、要能回到原文」的现实约束（转写来自音频，没有字幕时间轴
#     时 time_anchor 允许为 0）。
SUMMARY_SYSTEM = (
    "你是一名严谨、克制、信息密度优先的中文视频内容编辑。"
    "你的任务不是泛泛总结，而是基于转写和分段信息，产出可以直接用于「知识卡片」"
    "页面的结构化内容。所有内容都必须忠实原文，不得编造，不得补充外部资料，"
    "不得输出 JSON 以外的任何文字。You must return valid json only."
)

SUMMARY_USER = """请阅读下面的视频资料，并输出一个 JSON 对象。
注意：你必须返回合法的 json 对象，且只返回 json。

目标：
生成一个适合阅读页展示的结构化摘要，让用户在不看完整视频的情况下，也能快速理解：
1. 这支视频核心在讲什么；
2. 有哪些关键观点、论据、案例、争议和结论；
3. 内容是如何逐步展开的。

强约束：
1. 顶层只允许包含 title、overview、bulletPoints、chapters 四个字段。
2. title 必须是简洁、准确的中文标题，避免口号式空话。
3. overview 写成 3 到 5 句中文，整体形成一段完整概述：第 1 句交代主题或讨论对象，
   中间句交代关键论点、论据、背景、冲突或方法，最后 1 句交代结论、判断、影响或落点。
4. bulletPoints 必须是 5 到 8 条中文要点，每条 28 到 88 个字，每条都要能单独成为
   一张知识卡片；优先提炼事实、观点、因果、对比、条件、风险、建议、争议；
   不要写「作者认为」「视频提到」这类低信息密度前缀，直接写结论。
5. chapters 必须按内容自然分布生成，每项包含 title、start、summary：
   - title 像小标题，短而具体，能体现这一段的主题推进；
   - start 用视频里真实出现的时间点，单位为秒，按升序排列；拿不到时间轴就估一个；
   - summary 写成 2 到 3 个短句或 40 到 120 个字，说明这一段讲了什么、举了什么例子、
     得出了什么判断；
   - 章节数量随内容自适应，不要机械平均切分，也不要为了凑数量硬拆。
6. 不要写「视频主要讲了」「本视频介绍了」这类模板化空话，直接进入信息本体。
7. 不要引用不存在的数据，不要补充外部背景，不要猜测说话者未明确表达的动机。

输出格式示例：
{"title":"","overview":"","bulletPoints":["", "", "", "", ""],"chapters":[{"title":"","start":0,"summary":""}]}

视频标题：
{title}

转写节选：
{transcript}

分段数据节选：
{segments_json}"""

NOTE_SYSTEM = (
    "你是一名严谨、擅长整理学习型内容的中文知识编辑。"
    "你的任务是基于转写、分段和现有结构化摘要，单独产出一篇适合阅读的知识笔记。"
    "知识笔记必须比知识卡片更完整，能够承担学习、回顾和查阅任务。"
    "所有内容都必须忠实原文，不得编造，不得补充外部资料，不得输出 JSON 以外的任何文字。"
    "You must return valid json only."
)

NOTE_USER = """请阅读下面的视频资料，并输出一个 JSON 对象。
注意：你必须返回合法的 json 对象，且只返回 json。

目标：
基于原始转写和结构化摘要，生成一篇适合「知识笔记」阅读视图的 Markdown 笔记。

强约束：
1. 顶层只允许包含 knowledgeNoteMarkdown 一个字段。
2. knowledgeNoteMarkdown 必须是一篇完整 Markdown 笔记，不要用代码围栏包住整篇内容。
3. 笔记必须明显区别于知识卡片：要有连续叙述、上下文解释、章节展开和重点串联，
   允许引用已有结构化摘要，但必须重新组织为适合阅读的笔记。
4. 知识类内容优先组织为：核心结论、关键概念、推理/方法、章节展开、易错点/限制。
5. 教程、评论、新闻类内容退化为通用深度笔记：主题概览、关键信息、内容推进、结论/影响。
6. 只有在原文确实涉及公式、符号时才用 LaTeX：行内用 $...$，独立公式用 $$...$$。
7. 不要照抄转写全文，不要把原始 transcript 直接拼进笔记主体。
8. 不要补充外部背景，不要编造例子，不要猜测说话者未表达的动机。

输出格式示例：
{"knowledgeNoteMarkdown":"# 标题\\n\\n## 核心结论\\n\\n..."}

视频标题：
{title}

已有结构化摘要：
{summary_json}

转写节选：
{transcript_excerpt}"""

MINDMAP_SYSTEM = (
    "你是一名擅长把学习内容重新组织为知识导图的中文内容编辑。"
    "你的任务是基于已有结构化摘要和知识笔记，输出一个适合思维导图展示、信息密度充足、"
    "覆盖完整的 JSON 树。所有内容都必须忠实原文，不得编造，不得补充外部资料，"
    "不得输出 JSON 以外的任何文字。You must return valid json only.\n"
    "硬性验收：顶层 root 节点的 children 必须至少生成 3 个彼此有区分的一级分支，"
    "只返回根节点或少于 3 个一级分支都视为不合格；一级分支必须来自内容本身的归纳，"
    "不能为了凑数拆碎章节或要点。"
)

MINDMAP_USER = """请阅读下面的视频资料，并输出一个 JSON 对象。
注意：你必须返回合法的 json 对象，且只返回 json。

目标：
把当前视频内容组织成一棵真正「像思维导图」的知识树。它必须以概念、主题、方法、结论
之间的关系为核心，而不是把章节标题换个层级重新排列。最末层节点仍然要能回到原视频片段。

强约束：
1. 顶层只允许包含 title、root、nodes 三个字段。
2. root 必须是整棵导图的根节点 id，且 nodes 里有且只有一个该 id 的节点。
3. 每个节点必须包含这六个字段：id、label、type、summary、children、time_anchor。
4. type 只能是 root、theme、topic、leaf 之一。
5. 整体结构必须是树，最大深度为 root -> theme -> topic/leaf -> leaf。
6. 一级分支（type=theme）至少 3 个，通常控制在 3 到 5 个；只有内容确实存在更多
   互相独立的知识域时才增加。后续层级的数量与深度以内容结构为准，不要硬拆。
7. 一级分支必须有明确语义、彼此区分且能覆盖主要内容；不得用「其他」「更多内容」这类
   空泛占位词凑数，也不得按每个章节或每个要点机械铺开。
8. leaf 节点要具体、短促、点开就能看懂，不要写成长句，也不要只是「第 X 部分」；
   time_anchor 用该内容在原视频里的秒数，拿不准就填 0。
9. label 必须是有内容的主题名，禁止「主题1」「Part 1」「Section 1」等占位标题。
10. summary 要适合学习复盘，直接写信息本体，不要重复整段知识笔记；theme/topic 的
    summary 尽量写成 2 到 4 句，leaf 的 summary 至少交代「结论 / 方法 / 条件 / 例子」
    中的两项。
11. 只允许输出 JSON；JSON 字符串内部允许包含少量 Markdown 与 $...$ 数学公式。

写作要求：
- 优先按「概念定义 / 推导方法 / 典型例子 / 易错点 / 结论判断 / 应用条件」这类知识结构重组。
- 根节点应该是整支视频真正的学习主题，不要只是视频标题原样重复。
- 如果多个章节都在讲同一个概念、同一种方法、同一类例子，应该先合并为一个主题。
- 最终观感要像学习者自己整理出来的脑图，而不是讲稿目录。

输出格式示例：
{"version":1,"title":"","root":"root","nodes":[{"id":"root","label":"","type":"root","summary":"","children":[{"id":"theme-1","label":"","type":"theme","summary":"","children":[],"time_anchor":0}],"time_anchor":null}]}

视频标题：
{title}

已有结构化摘要：
{summary_json}

知识笔记：
{knowledge_note_markdown}"""

MINDMAP_REPAIR = (
    "上一版导图未通过结构验收。请重新输出完整 JSON，不要解释原因。"
    "硬性要求：root 节点的 children 至少包含 3 个有明确语义的一级 theme 分支；"
    "后续层级不设固定数量，不要为了凑数拆分或制造空泛节点。"
    "请先合并同类章节，再根据摘要和知识笔记中的概念、方法、例子、条件或结论组织架构，"
    "严禁编造外部信息。只输出 JSON。"
)


def _fill(tpl, **kw):
    """按 {key} 替换。模板里大量 JSON 花括号，所以不用 str.format，逐键替换最稳。"""
    out = tpl
    for key, value in kw.items():
        out = out.replace("{%s}" % key, "" if value is None else str(value))
    return out


def _chat_url(raw):
    """补成 /chat/completions 端点（跟 ui_server.agent_url_full 同一套路数）。

    配置里存的本来就是补全过的地址，但 opts 也可能从别处来（MCP、命令行），
    这里再兜一次，省得用户填了 …/v1 就报「连不上」。
    """
    url = (raw or "").strip().rstrip("/")
    if not re.match(r"^https?://", url):
        return ""
    if url.endswith("/chat/completions"):
        return url
    if re.search(r"/v\d+$", url):
        return url + "/chat/completions"
    return url + "/v1/chat/completions"


def _llm_content(payload):
    """从 chat/completions 的响应里取正文。个别网关即使没要流式也回 SSE，一并兼容。"""
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = (choices[0] or {}).get("message") or {}
        text = message.get("content")
        if isinstance(text, list):        # 有些网关把 content 拆成块数组
            text = "".join(str((c or {}).get("text") or "") for c in text)
        if text:
            return str(text)
    return ""


def llm_chat(cfg, messages, timeout=180):
    """一次非流式 /chat/completions，返回正文文本。失败抛人话。

    三件套由调用方传进来（见模块开头）——这里不认识 ui_server，也不读界面配置。
    """
    endpoint = _chat_url((cfg or {}).get("url"))
    model = str((cfg or {}).get("model") or "").strip()
    if not endpoint or not model:
        raise ValueError("还没配 LLM：把地址和模型名填上再跑一次")
    body = json.dumps({"model": model, "messages": messages, "stream": False},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "Authorization": "Bearer " + ((cfg or {}).get("key") or "-"),
                 "User-Agent": "guizang/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        raise ValueError("LLM 接口回了 %s：%s" % (e.code, detail)) from e
    except Exception as e:
        raise ValueError("连不上 LLM 地址：%s" % _clip(e, 160)) from e
    try:
        payload = json.loads(raw)
    except ValueError:
        # SSE 兜底：把 data: 行里的 delta.content 拼起来
        pieces = []
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                item = json.loads(chunk)
            except ValueError:
                continue
            for ch in (item.get("choices") or []):
                pieces.append(str(((ch or {}).get("delta") or {}).get("content") or ""))
        if pieces:
            return "".join(pieces)
        raise ValueError("LLM 回的既不是 JSON 也不是 SSE，认不出来")
    text = _llm_content(payload)
    if not text.strip():
        raise ValueError("LLM 这次没说话（返回空内容），可能是模型名不对或余额用完了")
    return text


def _extract_json(text):
    """从模型输出里抠出 JSON：先去 think 标签、去 ``` 围栏，再取最外层花括号。"""
    raw = str(text or "")
    raw = re.sub(r"<think[^>]*>.*?</think\s*>", "", raw, flags=re.DOTALL | re.IGNORECASE)
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        raw = raw[start:end + 1]
    for candidate in (raw, raw.replace("\r", "")):
        if not candidate.strip():
            continue
        try:
            return json.loads(candidate)
        except ValueError:
            try:
                return json.loads(candidate, strict=False)
            except ValueError:
                continue
    raise ValueError("模型没按约定回 JSON")


def _segments_excerpt(segments, limit=120):
    rows = []
    for seg in (segments or [])[:limit]:
        rows.append({"start": round(float(seg.get("start") or 0.0), 1),
                     "text": _clip(seg.get("text"), 200)})
    return json.dumps(rows, ensure_ascii=False)[:8000]


def _transcript_excerpt(text, limit=LLM_INPUT_LIMIT):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n……（中段省略）……\n" + text[-half:]


def _summary_messages(title, transcript, segments):
    return [{"role": "system", "content": SUMMARY_SYSTEM},
            {"role": "user", "content": _fill(
                SUMMARY_USER, title=title,
                transcript=_transcript_excerpt(transcript),
                segments_json=_segments_excerpt(segments))}]


def _note_messages(title, transcript, segments, summary):
    return [{"role": "system", "content": NOTE_SYSTEM},
            {"role": "user", "content": _fill(
                NOTE_USER, title=title,
                transcript_excerpt=_transcript_excerpt(transcript, 16000),
                summary_json=_clip(json.dumps(summary, ensure_ascii=False), 4000))}]


def _mindmap_messages(title, summary, note_markdown):
    return [{"role": "system", "content": MINDMAP_SYSTEM},
            {"role": "user", "content": _fill(
                MINDMAP_USER, title=title,
                summary_json=_clip(json.dumps(summary, ensure_ascii=False), 6000),
                knowledge_note_markdown=_clip(note_markdown, 12000))}]


def _clean_summary(payload, title):
    """把 LLM 的摘要收敛成固定形状 —— 少字段补空、类型不对就丢，别让它带病落盘。"""
    payload = payload if isinstance(payload, dict) else {}
    bullets = [str(x).strip() for x in (payload.get("bulletPoints") or [])
               if str(x or "").strip()]
    chapters = []
    for item in (payload.get("chapters") or []):
        if not isinstance(item, dict):
            continue
        ctitle = str(item.get("title") or "").strip()
        if not ctitle:
            continue
        chapters.append({"title": ctitle[:80],
                         "start": _as_int(item.get("start")) or 0,
                         "summary": str(item.get("summary") or "").strip()[:1200]})
    return {"title": str(payload.get("title") or "").strip() or title,
            "overview": str(payload.get("overview") or "").strip(),
            "bulletPoints": bullets[:12],
            "chapters": chapters[:24]}


def _normalize_nodes(nodes):
    """节点白名单化：只留约定的六个字段，type 不在枚举里就按位置猜一个。"""
    out = []
    for item in (nodes or []):
        if not isinstance(item, dict):
            continue
        nid = str(item.get("id") or "").strip()
        label = str(item.get("label") or "").strip()
        if not nid or not label:
            continue
        kind = str(item.get("type") or "").strip().lower()
        if kind not in MINDMAP_TYPES:
            kind = "topic"
        anchor = item.get("time_anchor")
        try:
            anchor = float(anchor) if anchor is not None else None
        except (TypeError, ValueError):
            anchor = None
        out.append({"id": nid[:60], "label": label[:120], "type": kind,
                    "summary": str(item.get("summary") or "").strip()[:800],
                    "children": [str(c) for c in (item.get("children") or [])
                                 if str(c or "").strip()][:40],
                    "time_anchor": anchor})
    return out


def _mindmap_top_branches(mindmap):
    """这棵树有几个合格的一级分支（按 children 里的 id 去 nodes 里数类型）。"""
    nodes = {n["id"]: n for n in (mindmap or {}).get("nodes") or []}
    root = nodes.get(str((mindmap or {}).get("root") or "").strip())
    if root is None and nodes:
        root = next((n for n in nodes.values() if n["type"] == "root"), None)
    if root is None:
        return 0
    return len([c for c in root["children"] if c in nodes])


def _normalize_mindmap(payload, title, summary):
    """LLM 的树 → 我们认的 {version,title,root,nodes[]}；节点收成扁平表（children 存 id）。"""
    payload = payload if isinstance(payload, dict) else {}
    raw_nodes = payload.get("nodes")
    if isinstance(raw_nodes, list) and any(isinstance(n, dict) and n.get("children")
                                           for n in raw_nodes):
        nodes = _normalize_nodes(raw_nodes)          # 已经是扁平 + children id 的写法
    else:
        nodes = _flatten_tree(raw_nodes or payload.get("root"), title)
    root_id = str(payload.get("root") or "").strip()
    if not root_id or root_id not in {n["id"] for n in nodes}:
        root_id = next((n["id"] for n in nodes if n["type"] == "root"), "")
    if not root_id and nodes:
        nodes[0]["type"] = "root"
        root_id = nodes[0]["id"]
    return {"version": 1,
            "title": str(payload.get("title") or "").strip() or title,
            "root": root_id,
            "nodes": nodes,
            "summary": _clip(summary.get("overview"), 400)}


def _flatten_tree(blob, title):
    """模型要是直接回了嵌套 children（对象数组），就自己摊平并接上 id。"""
    flat = []
    counter = {"n": 0}

    def walk(item, depth):
        if not isinstance(item, dict):
            return ""
        counter["n"] += 1
        nid = str(item.get("id") or "").strip() or "n%d" % counter["n"]
        kind = str(item.get("type") or "").strip().lower()
        if kind not in MINDMAP_TYPES:
            kind = "root" if depth == 0 else ("theme" if depth == 1 else "leaf")
        kids = []
        for child in (item.get("children") or []):
            cid = walk(child, depth + 1)
            if cid:
                kids.append(cid)
        try:
            anchor = float(item.get("time_anchor")) if item.get("time_anchor") is not None else None
        except (TypeError, ValueError):
            anchor = None
        flat.append({"id": nid[:60], "label": str(item.get("label") or title)[:120],
                     "type": kind,
                     "summary": str(item.get("summary") or "").strip()[:800],
                     "children": kids[:40], "time_anchor": anchor})
        return nid

    if isinstance(blob, list):
        for one in blob:
            walk(one, 0)
    else:
        walk(blob, 0)
    return flat


def _mindmap_fallback(title, summary):
    """模型彻底不配合时的本地兜底树：摘要的几个落点各撑一个分支，保证图能用。

    导图这种东西「有」比「没有」重要得多 —— 转写都拿到了，不能因为一次 JSON 坏掉
    就让用户整本书看不到东西。
    """
    nodes = [{"id": "root", "label": title, "type": "root",
              "summary": _clip(summary.get("overview"), 400), "children": [],
              "time_anchor": None}]
    branches = []
    chapters = summary.get("chapters") or []
    for i, chapter in enumerate(chapters[:5], start=1):
        cid = "theme-%d" % i
        nodes.append({"id": cid, "label": chapter["title"], "type": "theme",
                      "summary": _clip(chapter.get("summary") or summary.get("overview"),
                                       600),
                      "children": [], "time_anchor": chapter.get("start") or 0})
        branches.append(cid)
    if len(branches) < MIN_MINDMAP_BRANCHES:
        for i, point in enumerate((summary.get("bulletPoints") or [])[:6], start=1):
            if len(branches) >= MIN_MINDMAP_BRANCHES:
                break
            cid = "theme-%d" % (len(branches) + 1)
            nodes.append({"id": cid, "label": _clip(point, 40), "type": "theme",
                          "summary": point, "children": [], "time_anchor": 0})
            branches.append(cid)
    for i in range(len(branches) + 1, MIN_MINDMAP_BRANCHES + 1):
        cid = "theme-%d" % i
        nodes.append({"id": cid, "label": "知识分支 %d" % i, "type": "theme",
                      "summary": _clip(summary.get("overview"), 400), "children": [],
                      "time_anchor": 0})
        branches.append(cid)
    nodes[0]["children"] = branches
    return {"version": 1, "title": title, "root": "root", "nodes": nodes,
            "summary": _clip(summary.get("overview"), 400)}


def build_mindmap(cfg, title, summary, note_markdown, progress=None):
    """生成思维导图：一次生成 + 不合格时一次修复重试 + 本地兜底（绝不抛）。"""
    try:
        payload = _extract_json(llm_chat(cfg, _mindmap_messages(title, summary,
                                                               note_markdown)))
        mindmap = _normalize_mindmap(payload, title, summary)
        if _mindmap_top_branches(mindmap) >= MIN_MINDMAP_BRANCHES:
            _say(progress, "mindmap", 93, "导图生成完成")
            return mindmap, ""
        _say(progress, "mindmap", 92, "一级分支不够，让模型重排一次")
        repair = _mindmap_messages(title, summary, note_markdown)
        repair.append({"role": "user", "content": MINDMAP_REPAIR})
        again = _extract_json(llm_chat(cfg, repair))
        mindmap = _normalize_mindmap(again, title, summary)
        if _mindmap_top_branches(mindmap) >= MIN_MINDMAP_BRANCHES:
            _say(progress, "mindmap", 93, "导图生成完成")
            return mindmap, ""
        return _mindmap_fallback(title, summary), "模型两次给的导图一级分支都不够，已用摘要兜了一张"
    except Exception as e:
        # 导图失败不上升为整条线的失败：本地兜底树照落盘。
        return _mindmap_fallback(title, summary), "导图没生成成（%s），已用摘要兜了一张" % _clip(e, 120)


def mindmap_to_tree(mindmap):
    """导图 → book_notes 认得的那种 {label, kids, color} 树，好复用现成的 SVG 画法。

    颜色按层级给：一级分支上色，越往下越淡 —— 和笔记导图那套「看图等于复习」一致。
    """
    nodes = {n["id"]: n for n in (mindmap or {}).get("nodes") or []}
    palette = ("#b9c8d8", "#8fb7a6", "#d8c9a6", "#c9b1ff", "#9cc6ff", "#ff9d8f")
    seen = set()

    def make(nid, depth):
        node = nodes.get(nid)
        if node is None or nid in seen:
            return None
        seen.add(nid)
        kids = [k for k in (make(c, depth + 1) for c in node.get("children") or [])
                if k is not None]
        label = node["label"]
        if node.get("summary") and depth >= 1:
            label = "%s　%s" % (label, _clip(node["summary"], 40))
        color = palette[min(depth - 1, len(palette) - 1)] if depth >= 1 else None
        return {"label": label, "kids": kids, "color": color}

    root = make(mindmap.get("root") or "", 0)
    return root or {"label": (mindmap or {}).get("title") or "视频", "kids": [], "color": None}


# ─────────────────────────── 落盘 ───────────────────────────

PLATFORM_LABEL = {"bilibili": "B 站", "youtube": "YouTube"}


def _mmss(seconds):
    return _stamp(seconds)


def _provenance(plan_info, asr_engine):
    when = _mmss(plan_info.get("duration"))
    line = "> 视频：%s（%s，%s）" % (plan_info.get("title") or "未命名",
                                    PLATFORM_LABEL.get(plan_info.get("platform"),
                                                       plan_info.get("platform") or "视频"),
                                    when)
    if plan_info.get("uploader"):
        line += "　·　%s" % plan_info["uploader"]
    link = plan_info.get("url") or ""
    shown = (link[:60] + "…") if len(link) > 60 else link
    out = [line, "> 原文：[%s](%s)" % (shown, link)]
    if asr_engine:
        out.append("> 转写：%s　·　归藏整理于 %s" % (asr_engine, _now()))
    return "\n".join(out)


def _summary_block(summary):
    """概览那一段：摘要是给人「不点开转写也知道讲了什么」用的，放在第一章开头。"""
    out = []
    if summary.get("overview"):
        out += ["## 概览", "", summary["overview"].strip(), ""]
    if summary.get("bulletPoints"):
        out += ["## 要点", ""]
        out += ["- %s" % str(p).strip() for p in summary["bulletPoints"]]
        out.append("")
    return out


def _chapter_bodies(transcript, segments, chapters):
    """把转写按章节切段。

    有分段（mlx / faster 都会给）就照时间切；云转写只回整段文字时按字数均分 ——
    章节标题是 LLM 按内容给的，正文对不齐也不至于错位到看不了。
    """
    text = (transcript or "").strip()
    if not chapters:
        return [("全文转写", text)]
    if segments:
        pieces = [""] * len(chapters)
        for seg in segments:
            start = float(seg.get("start") or 0.0)
            index = 0
            for i, chapter in enumerate(chapters):
                if start >= float(chapter.get("start") or 0):
                    index = i
            pieces[index] += (seg.get("text") or "") + " "
        return [(chapters[i]["title"], pieces[i].strip()) for i in range(len(chapters))]
    # 没有分段：按章节条数均分字数，并给每章标上它自己的标题
    body, step = [], max(1, len(text) // max(len(chapters), 1))
    for i, chapter in enumerate(chapters):
        chunk = text[i * step:(i + 1) * step] if i < len(chapters) - 1 else text[i * step:]
        body.append((chapter["title"], chunk.strip()))
    return body


def _chapter_docs(bodies, extras):
    """[(章名, 正文)] → 每章一份完整 Markdown（来源那几行只进第一章）。

    落盘和重建共用这一份拼法：合并稿和分章文件必须长得一样，两处各写一套的话，
    重建出来的书会和当初落盘的那本悄悄分叉。
    """
    out = []
    for index, (chapter_title, body) in enumerate(bodies):
        lines = ["# %s" % (chapter_title or ("第 %d 章" % (index + 1))), ""]
        if index == 0 and extras:
            lines.append("\n".join(extras).strip())
            lines.append("")
        if body:
            lines.append(body)
            lines.append("")
        out.append("\n".join(lines).strip() + "\n")
    return out


def _norm_seg(seg):
    """一段转写收敛成能安全落盘的样子：时间收成浮秒或空，文字裁长，认不出的字段留着。"""
    seg = seg if isinstance(seg, dict) else {}
    rest = {k: v for k, v in seg.items() if k not in ("start", "end", "text", "speaker")}
    ordered = {"start": _maybe_sec(seg.get("start")),
               "end": _maybe_sec(seg.get("end")),
               "text": _clip(seg.get("text"), MAX_SEG_CHARS).strip()}
    speaker = str(seg.get("speaker") or "").strip()
    if speaker:
        ordered["speaker"] = speaker[:40]
    ordered.update(rest)      # 剩下的字段排在后面，前四个位置稳定
    return ordered


def _transcript_doc(spoken, asr_cfg, plan_info):
    """转写结果 → transcript.json 那份文档。

    为什么要单开这个文件：摘要、笔记、导图都是派生物，只有这份带时间戳的段落是
    「用户会去改的那一份」—— 点时间轴跳到那一句、编辑器改完存回去，都得有个稳定的
    落点；transcript.txt 那份是纯文本视图，没地方放时间。
    """
    segments = [_norm_seg(s) for s in (spoken.get("segments") or [])][:MAX_SEGMENTS]
    duration = _maybe_sec(spoken.get("duration"))
    if duration is None:
        duration = _maybe_sec(plan_info.get("duration"))
    if duration is None:
        duration = _maybe_sec(segments[-1].get("end") if segments else None)
    return {"schema": TRANS_SCHEMA,
            "engine": str(spoken.get("engine") or
                          (asr_cfg or {}).get("engine") or "auto"),
            "language": str(spoken.get("language") or
                            (asr_cfg or {}).get("language") or DEFAULT_LANGUAGE),
            "duration": duration if duration is not None else 0.0,
            "title": str(plan_info.get("title") or ""),
            "vid": str(plan_info.get("vid") or ""),
            "page": _as_int(plan_info.get("page")) or 1,
            "segments": segments,
            "generated_at": _now(),
            "updated_at": int(time.time())}


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_json(path):
    """读一份 JSON：缺文件或读坏了都回 None，怎么兜底由调用方决定。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_text(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _save_notes(book_dir, note_markdown, title, summary, meta):
    """按 book_notes 的路数写 notes.json / notes.md：AI 那份笔记是一条「笔记条目」。

    为什么要绕这一圈而不是自己写个 notes.json：阅读器右栏、笔记编辑器、导出 Anki
    都只认 book_notes 的 schema，自己造一份字段就会「书能打开、笔记面板是空的」。
    """
    doc = book_notes.load_notes(book_dir)
    body = (note_markdown or "").strip()
    if not body:
        body = "\n".join(_summary_block(summary)).strip()
    if not body:
        body = "（这一集的 AI 笔记没生成出来，转写还在，见「全文转写」那一章。）"
    doc["entries"] = [{
        "id": "n_" + uuid.uuid4().hex[:10],
        "title": "视频笔记" if not summary.get("title") else summary["title"],
        "body": body[:book_notes.MAX_ENTRY_BODY],
        "tag": "idea",
        "refs": [],
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }]
    doc["note"] = _clip(summary.get("overview"), 2000)
    book_notes.save_notes(book_dir, doc)
    saved = book_notes.load_notes(book_dir)
    out = book_notes.export_notes(book_dir, saved, meta=meta,
                                  titles=book_notes.chapter_titles(book_dir),
                                  book_label=title)
    return saved, out


def _save_mindmap(book_dir, mindmap, title, meta):
    """导图 JSON 落盘 + 用 book_notes 那套 SVG 画法出一张图（看图等于复习）。"""
    mp = os.path.join(book_dir, "mindmap.json")
    _write_json(mp, mindmap)
    svg_path = ""
    try:
        svg = book_notes.build_mindmap_svg(mindmap_to_tree(mindmap), title)
        svg_path = os.path.join(book_dir, "mindmap.svg")
        _write_text(svg_path, svg)
    except Exception:
        svg_path = ""                     # 画不出来不影响 JSON 那份
    return mp, svg_path


def _ai_bundle(cfg, title, transcript, segments, progress):
    """三次 LLM 调用。任何一次失败都只记原因，不往上抛。"""
    import_result = {"summary": {}, "note": "", "warnings": []}
    if not (cfg or {}).get("url") or not (cfg or {}).get("model"):
        import_result["warnings"].append("还没配 LLM（地址和模型名），这次只存了转写")
        return import_result
    _say(progress, "summary", 76, "正在整理摘要")
    try:
        raw = llm_chat(cfg, _summary_messages(title, transcript, segments))
        import_result["summary"] = _clean_summary(_extract_json(raw), title)
    except Exception as e:
        import_result["warnings"].append("结构化摘要没成：%s" % _clip(e, 160))
        return import_result                # 摘要没了，笔记和导图就无从谈起了
    _say(progress, "summary", 82, "摘要完成")
    try:
        _say(progress, "note", 83, "正在写知识笔记")
        raw = llm_chat(cfg, _note_messages(title, transcript, segments,
                                           import_result["summary"]))
        import_result["note"] = str(_extract_json(raw).get("knowledgeNoteMarkdown")
                                   or "").strip()
        if not import_result["note"]:
            import_result["warnings"].append("知识笔记回来了但是空的")
    except Exception as e:
        import_result["warnings"].append("知识笔记没成：%s" % _clip(e, 160))
    try:
        _say(progress, "mindmap", 88, "正在画思维导图")
        mindmap, warn = build_mindmap(cfg, title, import_result["summary"],
                                      import_result["note"], progress)
        import_result["mindmap"] = mindmap
        if warn:
            import_result["warnings"].append(warn)
    except Exception as e:                  # build_mindmap 自己兜底，这里只是双保险
        import_result["warnings"].append("导图没成：%s" % _clip(e, 160))
    return import_result


def _task_key(plan_info, page=None):
    """一次视频一个临时目录，而且目录名稳定 —— 断点续跑靠的就是「重跑时还找得到上次那些产物」。

    以前用随机名：跑完固然干净，中途挂了也一并删干净，等于下好的音频和转好的分片
    全扔了，重跑从头再来。名字认 vid + 分 P：同一集重跑撞上同一个目录，不同集不串。
    """
    vid = str(plan_info.get("vid") or "").strip()
    platform = str(plan_info.get("platform") or "").strip()
    if not vid:      # b23.tv 这类短链在认链接阶段还没有编号，拿地址哈希顶上
        vid = hashlib.md5(str(plan_info.get("url") or "").encode("utf-8")).hexdigest()[:12]
    tail = "_p%s" % int(page) if page else ""
    return re.sub(r"[^0-9A-Za-z_\-]", "", "%s_%s" % (platform, vid))[:60] + tail


def _existing_audio(task_dir):
    """临时目录里已经下好的音频（没有就空串）。认 audio.* 那份，.part 不算数。"""
    if not os.path.isdir(task_dir):
        return ""
    hits = [os.path.join(task_dir, n) for n in sorted(os.listdir(task_dir))
            if n.startswith("audio.") and not n.endswith((".part", ".tmp"))]
    return hits[0] if hits else ""


def _stage_transcript(task_dir, spoken, asr_cfg):
    """转写一成功就暂存一份，别等落盘。

    为什么这么早存：AI 那三步是最容易挂的一环（没配、超时、回烂 JSON），挂在这儿时
    已经花掉的时间最贵 —— 转写先落进临时目录，后面怎么失败都丢不了，重跑也不用再转一遍。
    """
    _write_json(os.path.join(task_dir, "transcript.json"),
                {"text": spoken.get("text") or "",
                 "segments": spoken.get("segments") or [],
                 "engine": spoken.get("engine") or "",
                 "language": _asr_lang(spoken, asr_cfg),
                 "duration": spoken.get("duration"),
                 "model": _asr_model(asr_cfg),
                 "saved_at": _now()})


def _asr_lang(spoken, asr_cfg):
    """这次转写用的语言：转写结果没带就认配置，配置也没有就认默认值。"""
    return str(spoken.get("language") or (asr_cfg or {}).get("language")
               or DEFAULT_LANGUAGE)


def _asr_model(asr_cfg):
    return str((asr_cfg or {}).get("model") or "")


def _staged_transcript(task_dir, asr_cfg):
    """上次转好的那份能不能直接用：模型、语言对不上就不算（拿旧结果续会把两种口径混一起）。"""
    doc = _read_json(os.path.join(task_dir, "transcript.json"))
    if not isinstance(doc, dict) or not str(doc.get("text") or "").strip():
        return None
    if str(doc.get("model") or "") != _asr_model(asr_cfg):
        return None
    if str(doc.get("language") or "") != _asr_lang({}, asr_cfg):
        return None
    if not isinstance(doc.get("segments"), list):
        return None
    return {"text": str(doc.get("text") or ""), "segments": doc["segments"],
            "engine": str(doc.get("engine") or ""),
            "language": str(doc.get("language") or ""),
            "duration": _maybe_sec(doc.get("duration"))}


def run(url, out_dir, opts, progress):
    """整条流水线：认链接 → 下音频 → 转写 → 三次 LLM → 落成一本书。

    opts 认这些键：
      asr   {"engine": "auto"|"mlx"|"faster"|"cloud", "model": str,
             "cloud": {"url", "key", "model"}, "language": "zh"}
      llm   {"url", "key", "model"}      —— 由调用方从 agent_cfg() 取
      page  取第几 P（B 站多 P 用；不给就认链接里的 p=，再不给就是第一 P）
      resume  默认 True：沿用上次已经下好的音频、已经转好的分片与转写；
              给 False 就从头重做（换了引擎、想重转一遍时用）
      should_stop  可调用对象，阶段之间查一次；要求停就抛 Aborted

    progress(stage, pct, note) 的取值表见模块开头。返回值：
      {book, transcript_path, transcript_json, merged_path, note_path, mindmap_path,
       chapters, words, segments, duration, language, asr_engine, elapsed, ai_error,
       resume_dir}
    ai_error 非空表示 AI 那部分缺席（原因在里头），书本身照常可用。
    中途挂了（含中止）时临时目录不删，下次重跑同一条链接会接着用；跑成功了才清干净
    （用户要的是这本书，不是那段音轨），这时 resume_dir 给空串。
    """
    opts = opts or {}
    started = time.time()
    asr_cfg = dict(opts.get("asr") or {})
    asr_cfg.setdefault("language", (opts.get("language") or "").strip() or DEFAULT_LANGUAGE)
    llm_cfg = dict(opts.get("llm") or {})
    resume = opts.get("resume")
    resume = True if resume is None else bool(resume)

    plan_info = plan(url, opts.get("page"))
    _say(progress, "plan", 5, "认出来了：%s" % _clip(plan_info["title"], 40))
    _check_stop(opts)

    task_dir = os.path.join(TMP_ROOT, _task_key(plan_info, plan_info.get("page")))
    if not resume:
        stale = os.path.join(task_dir, "transcript.json")
        if os.path.isfile(stale):
            os.remove(stale)          # 明确要求重转：暂存那份就别再拿出来了
    finished = False
    try:
        audio = _existing_audio(task_dir) if resume else ""
        if audio:
            _say(progress, "download", 45, "沿用上回下好的音频，不用再下一遍")
        else:
            _say(progress, "download", 5, "正在取音频")
            audio = download_audio(plan_info["url"], task_dir, progress,
                                   should_stop=opts.get("should_stop"))
        _check_stop(opts)

        spoken = _staged_transcript(task_dir, asr_cfg) if resume else None
        if spoken:
            _say(progress, "asr", 75, "沿用上回转好的 %d 段，不用再转一遍"
                 % len(spoken["segments"]))
        else:
            spoken = transcribe(audio, asr_cfg, progress,
                                should_stop=opts.get("should_stop"), resume=resume)
            _stage_transcript(task_dir, spoken, asr_cfg)
        _check_stop(opts)

        bundle = _ai_bundle(llm_cfg, plan_info["title"], spoken["text"],
                            spoken["segments"], progress)
        _check_stop(opts)

        summary = bundle.get("summary") or {}
        mindmap = bundle.get("mindmap")
        ai_error = "；".join(bundle.get("warnings") or [])
        _say(progress, "save", 95, "正在落成一本书")
        book = _save_book(out_dir, plan_info, spoken, summary, bundle.get("note") or "",
                         mindmap, asr_cfg, ai_error)
        finished = True
    finally:
        if finished:
            shutil.rmtree(task_dir, ignore_errors=True)

    _say(progress, "save", 100, "《%s》进书架了" % _clip(book.get("title"), 30))
    _say(progress, "done", 100, "完成")
    return {"book": book["meta"], "transcript_path": book["transcript_path"],
            "transcript_json": book["transcript_json"],
            "merged_path": book["merged_path"],
            "note_path": book["note_path"], "mindmap_path": book["mindmap_path"],
            "chapters": book["meta"].get("chapters") or 0,
            "words": book["meta"].get("words") or 0,
            "segments": book["meta"].get("segments") or 0,
            "duration": _sec(book["meta"].get("duration")),
            "language": book["meta"].get("language") or "",
            "asr_engine": spoken["engine"], "elapsed": round(time.time() - started, 1),
            "ai_error": ai_error, "resume_dir": ""}


def _save_book(out_dir, plan_info, spoken, summary, note_markdown, mindmap, asr_cfg,
               ai_error):
    """转写 + 摘要 + 笔记 + 导图 → 书库里的一本书（结构和 book_import 落的一模一样）。

    正文用「章节」这一个层级：LLM 给的每章正好是一个 `#` 标题，book_import 切出来
    的 _catalog.json 就是那批章节名，阅读器目录一次到位。视频来源那几行放进第一章
    开头 —— 放在第一个标题之前的话，切章时会多出一章「无题」把整本目录顶偏。
    """
    title = plan_info["title"] or "视频笔记"
    chapters = summary.get("chapters") or []
    bodies = _chapter_bodies(spoken["text"], spoken["segments"], chapters)
    extras = [_provenance(plan_info, spoken["engine"])]
    if summary.get("overview"):
        extras += [""] + _summary_block(summary)
    if ai_error:
        extras += ["", "> AI 那部分没生成全：%s" % ai_error]

    body_md = "\n".join(_chapter_docs(bodies, extras)).strip() + "\n"

    name = re.sub(r'[\\/:*?"<>|]', "", title)[:60] or "video"
    info = book_import.import_book(
        out_dir, "%s.md" % name, body_md.encode("utf-8"),
        title=title, author=plan_info.get("uploader") or PLATFORM_LABEL.get(
            plan_info.get("platform"), plan_info.get("platform") or ""),
        book_id_prefix="video", source="video")

    book_dir = info["dir"]
    _write_text(os.path.join(book_dir, TRANS_TXT), spoken["text"] + "\n")
    # 合并稿也落一份：一本书一个文件的整本 Markdown，导出、比对、重建都从这里走
    _write_text(os.path.join(book_dir, "merged.md"), body_md)
    # 带时间戳的那份段落单独落一个文件：编辑器改转写、点时间轴跳到那一句，都读它
    _write_json(os.path.join(book_dir, TRANS_FILE),
                _transcript_doc(spoken, asr_cfg, plan_info))
    _write_json(os.path.join(book_dir, SUMMARY_FILE),
                {"summary": summary, "segments": spoken["segments"],
                 "asr_engine": spoken["engine"], "asr": {
                     "engine": asr_cfg.get("engine") or "auto",
                     "model": asr_cfg.get("model") or ""},
                 "ai_error": ai_error, "generated_at": _now()})

    words = len(re.sub(r"\s", "", spoken["text"]))
    picked = _picked_page(plan_info)
    meta = _read_meta(book_dir)
    meta.update({
        "source": "video", "format": "video", "url": plan_info.get("url") or "",
        "site": PLATFORM_LABEL.get(plan_info.get("platform"),
                                   plan_info.get("platform") or ""),
        "platform": plan_info.get("platform") or "", "vid": plan_info.get("vid") or "",
        "uploader": plan_info.get("uploader") or "",
        "duration": plan_info.get("duration") or 0,
        "pages": len(plan_info.get("pages") or []) or 1,
        # 选中那一 P 自己的名字和时长：多 P 的书在书架上得说清这是第几 P
        "page": picked["i"], "page_title": picked["title"],
        "page_duration": picked["duration"],
        "cover": plan_info.get("cover") or "",
        "asr_engine": spoken["engine"], "words": words, "chars": words,
        "language": spoken.get("language") or asr_cfg.get("language") or DEFAULT_LANGUAGE,
        "segments": len(spoken.get("segments") or []), "transcript_edited": False,
        "ai_error": ai_error, "video_at": _now(),
    })
    _write_json(os.path.join(book_dir, "meta.json"), meta)

    saved, exported = _save_notes(book_dir, note_markdown, title, summary, meta)
    mindmap_path, svg_path = "", ""
    if mindmap:
        mindmap_path, svg_path = _save_mindmap(book_dir, mindmap, title, meta)
    meta.update({"notes_marks": len(saved.get("marks") or []),
                 "notes_entries": len(saved.get("entries") or []),
                 "chapters": info.get("chapters") or 0,
                 "mindmap": os.path.basename(mindmap_path) if mindmap_path else "",
                 "mindmap_svg": os.path.basename(svg_path) if svg_path else ""})
    _write_json(os.path.join(book_dir, "meta.json"), meta)

    pub = dict(info)
    pub.update({"title": title, "author": meta.get("author") or "",
                "meta": {**meta, "id": info.get("id"), "dir": book_dir,
                         "label": info.get("label")},
                "transcript_path": os.path.join(book_dir, TRANS_TXT),
                "transcript_json": os.path.join(book_dir, TRANS_FILE),
                "merged_path": os.path.join(book_dir, "merged.md"),
                "segments": len(spoken.get("segments") or []),
                "duration": _sec(meta.get("duration")),
                "language": meta.get("language") or "",
                "book_dir": book_dir,
                "note_path": exported.get("path") or "",
                "mindmap_path": mindmap_path, "mindmap_svg": svg_path})
    return pub


def _picked_page(plan_info):
    """这一本书取的是第几 P，以及那一 P 的标题与时长。

    单 P 视频也走这条路：pages 里就一条，返回的正是整支视频的标题和时长，
    所以调用方不用分两种情况处理。
    """
    pages = [p for p in (plan_info.get("pages") or []) if isinstance(p, dict)]
    want = _as_int(plan_info.get("page")) or 1
    hit = next((p for p in pages if _as_int(p.get("i")) == want), None)
    if hit is None:
        hit = pages[0] if pages else {}
    return {"i": _as_int(hit.get("i")) or want,
            "title": str(hit.get("title") or plan_info.get("title") or ""),
            "duration": _as_int(hit.get("duration")) or _as_int(plan_info.get("duration")) or 0}



def _read_meta(book_dir):
    path = os.path.join(book_dir, "meta.json")
    try:
        with open(path, encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        return {}
    return meta if isinstance(meta, dict) else {}


# ───────────────────────── 转写：读 / 改 / 重建 / 导出 ─────────────────────────

def _safe_file(title):
    """书名 → 能安全落盘的文件名（跟 _save_book 裁标题的规矩一致）。"""
    return re.sub(r'[\\/:*?"<>|]', "", str(title or "").strip())[:60] or "transcript"


def _txt_segments(path):
    """只有 transcript.txt 的老书：一行一段，时间戳给空。读不到返回 None。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError:
        return None
    return [{"start": None, "end": None, "text": line.strip()}
            for line in raw.splitlines() if line.strip()]


def load_transcript(book_dir, keyword="", start=None, end=None):
    """读一本书的转写，收成同一种形状（带 source 说明是从哪份文件来的）。

    transcript.json 在就读它（source="json"，有时间戳）；不在就从 transcript.txt 兜底
    （source="txt"，时间戳是空，不抛）—— 升级之前转的那批书也得打得开、改得动。

    keyword / start / end 是筛段：关键词不分大小写，也认说话人；时间按「这段和这个区间
    有交集」算。没时间戳的段落在带时间条件时会被滤掉 —— 它没法证明自己落在区间里，
    留着比假装它算数更诚实。
    """
    book_dir = book_dir or ""
    meta = _read_meta(book_dir)
    doc = _read_json(os.path.join(book_dir, TRANS_FILE))
    if isinstance(doc, dict) and isinstance(doc.get("segments"), list):
        source = "json"
        segments = [_norm_seg(s) for s in doc["segments"]
                    if isinstance(s, dict) and str(s.get("text") or "").strip()]
        engine = str(doc.get("engine") or meta.get("asr_engine") or "")
        language = str(doc.get("language") or meta.get("language") or "")
        duration = _maybe_sec(doc.get("duration"))
        if duration is None:
            duration = _maybe_sec(meta.get("duration"))
        edited = bool(doc.get("edited"))
        path = os.path.join(book_dir, TRANS_FILE)
    else:
        source = "txt"
        txt = _txt_segments(os.path.join(book_dir, TRANS_TXT))
        if txt is None:
            raise ValueError("这本书里没有转写文件（%s 和 %s 都不在），"
                             "编辑器打不开一份不存在的转写" % (TRANS_FILE, TRANS_TXT))
        segments = txt
        engine = str(meta.get("asr_engine") or "")
        language = str(meta.get("language") or "")
        duration = _maybe_sec(meta.get("duration"))
        edited = False
        path = os.path.join(book_dir, TRANS_TXT)

    # 每段都得有 id，编辑器才知道「我改的是哪一段」：老书写盘时没记 id，这里按位置现场
    # 补一个（用户一保存就固定进 transcript.json）。不补的话前端只能按下标对位，而删一段、
    # 插一段之后下标全会挪 —— 挪了还对不上，就是用户手打的字静默跑到别的段上去了。
    for i, seg in enumerate(segments):
        if not str(seg.get("id") or "").strip():
            seg["id"] = "s%05d" % i

    rows = segments
    key = str(keyword or "").strip().lower()
    if key:
        rows = [s for s in rows if key in str(s.get("text") or "").lower()
                or key in str(s.get("speaker") or "").lower()]
    if start is not None or end is not None:
        low = _sec(start)
        high = _sec(end) if end not in (None, "") else float("inf")
        hits = []
        for s in rows:
            if s.get("start") is None:
                continue
            begin = _sec(s.get("start"))
            finish = max(begin, _sec(s.get("end")))
            if begin <= high and finish >= low:
                hits.append(s)
        rows = hits

    return {"book_dir": book_dir, "path": path, "source": source, "engine": engine,
            "language": language, "duration": duration, "edited": edited,
            "timed": any(s.get("start") is not None for s in segments),
            "title": str((doc if isinstance(doc, dict) else {}).get("title")
                         or meta.get("title") or ""),
            "total": len(segments), "matched": len(rows),
            "segments": rows, "text": _join_text(segments)}


def save_transcript(book_dir, segments, engine=None, language=None, duration=None,
                    sync_txt=True):
    """编辑器保存转写：原子写、认不出的字段原样留着、纯文本那份跟着一起同步。

    为什么原子写：这是用户在编辑器里手打的内容，写到一半断电留下半截 JSON，
    下一次读就是坏档 —— 比丢一次保存严重得多。
    为什么同步 transcript.txt：两份是同一个转写的两个视图，只更新一份的话，
    重建章节时用的文字和用户在纯文本里看到的就对不上了。
    """
    if not os.path.isdir(book_dir or ""):
        raise ValueError("没有这本书的目录，转写没处存")
    if not isinstance(segments, (list, tuple)):
        raise ValueError("转写段落得是一个列表")
    if not len(segments):
        raise ValueError("一段都没给，这次不写盘：这份接口是整本覆盖，"
                         "接个空列表就等于把用户的转写清空了")
    path = os.path.join(book_dir, TRANS_FILE)
    doc = _read_json(path)
    doc = doc if isinstance(doc, dict) else {}
    # 旧段落按 id 找得着就认 id，找不到就按位置对：拆段合段之后位置是会挪，
    # 所以只在来段没写 id 时才退回位置对齐 —— 前端给每段带上 id 就不会串。
    old = [s for s in (doc.get("segments") or []) if isinstance(s, dict)]
    by_id = {}
    for item in old:
        oid = str(item.get("id") or "")
        if oid:
            by_id[oid] = item
    kept = []
    for index, seg in enumerate(segments[:MAX_SEGMENTS]):
        if not isinstance(seg, dict):
            continue
        sid = str(seg.get("id") or "")
        base = by_id.get(sid) or (old[index] if index < len(old) else {})
        merged = {**base, **seg}
        # after 是编辑器「这一新段插在哪一段后面」的内部锚点，插完就没用了。它得吃掉：
        # 别处都原样留字段是为了不丢用户的字，而这个字段留在 transcript.json 里，
        # 下一次读还会带出来，导出的 JSON 里就多出一串谁也不认的编号。
        merged.pop("after", None)
        kept.append(_norm_seg(merged))

    doc.update({"schema": TRANS_SCHEMA, "segments": kept, "edited": True,
                "edited_at": _now(), "updated_at": int(time.time())})
    if engine is not None:
        doc["engine"] = str(engine)
    if language is not None:
        doc["language"] = str(language)
    if duration is not None:
        doc["duration"] = _sec(duration)
    _write_json(path, doc)
    if sync_txt:
        _write_text(os.path.join(book_dir, TRANS_TXT), _join_text(kept) + "\n")
    # 「手改」那盏灯看的是 meta.json 里的 transcript_edited —— 列一本书不该为了它把几十兆的
    # transcript.json 整份读进来。改动已经落盘了，meta 不跟着记一笔，界面会一直说「还是语音
    # 识别的原样」，用户明明改过字。
    meta = _read_meta(book_dir)
    if meta:
        meta.update({"transcript_edited": True, "edited_at": _now(),
                     "updated_at": int(time.time())})
        _write_json(os.path.join(book_dir, "meta.json"), meta)
    text = _join_text(kept)
    return {"path": path, "segments": len(kept),
            "words": len(re.sub(r"\s", "", text)), "timed": bool(
                any(s.get("start") is not None for s in kept)),
            "rebuild_needed": True, "msg": "转写存好了，章节要跟着改就再调一次重建"}


def _backup_dir(book_dir):
    """给重建腾一个带时间戳的备份目录（同一秒里连着重建两次也不撞车）。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(book_dir, ".bak-" + stamp)
    n = 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(book_dir, ".bak-%s-%d" % (stamp, n))
    os.makedirs(path)
    return path


def _restore_backup(book_dir, backup, moved):
    """重建写挂了就原样挪回去，别留一本半新半旧的书。"""
    for name in reversed(moved):
        src = os.path.join(backup, name)
        dst = os.path.join(book_dir, name)
        try:
            if os.path.isdir(dst):
                shutil.rmtree(dst, ignore_errors=True)
            elif os.path.exists(dst):
                os.remove(dst)
            shutil.move(src, dst)
        except Exception:
            pass                      # 兜底也救不了就让它留着，至少备份是完整的


def rebuild_book(book_dir):
    """按改过的转写重建这本书：分章文件、合并稿、notes.md 都跟着新转写走。

    为什么要备份到 .bak-时间戳/：章节文件是用户读过、也可能自己动过手的东西，
    静默覆盖等于把他那些改动丢了。所以旧文件是整个「挪」进备份目录（不是覆盖掉），
    要回退把里面的东西挪回来就行。notes.json 里用户的划线和笔记条目一个字不改，
    只是拿它重新导出 notes.md。

    只认摘要里那批章节表（summary.json）：重建不叫 LLM，章节名沿用上一版，
    改的是正文归属 —— 想重新分章就再跑一次 AI（这条线本来就允许 AI 缺席）。
    """
    if not os.path.isdir(book_dir or ""):
        raise ValueError("没有这本书的目录，重建不了")
    trans = load_transcript(book_dir)
    segments = trans["segments"]
    text = _join_text(segments)
    if not text.strip():
        raise ValueError("转写是空的，重建出来只会是一本空书")

    doc = _read_json(os.path.join(book_dir, SUMMARY_FILE))
    summary = (doc or {}).get("summary") if isinstance(doc, dict) else None
    summary = summary if isinstance(summary, dict) else {}
    chapters = summary.get("chapters") or []
    meta = _read_meta(book_dir)
    # 没时间戳的老书按字数均分（拿 None 时间戳去切章会让每一段都落进第一章）
    bodies = _chapter_bodies(text, segments if trans["timed"] else None, chapters)
    source = {"title": meta.get("page_title") or meta.get("title") or "视频笔记",
              "platform": meta.get("platform") or "", "url": meta.get("url") or "",
              "uploader": meta.get("uploader") or "",
              "duration": trans.get("duration") if trans.get("duration") is not None
              else meta.get("duration")}
    extras = [_provenance(source, trans.get("engine") or meta.get("asr_engine") or "")]
    if summary.get("overview"):
        extras += [""] + _summary_block(summary)
    if meta.get("ai_error"):
        extras += ["", "> AI 那部分没生成全：%s" % meta["ai_error"]]
    extras += ["", "> 转写已于 %s 手工修订，全文与各章按修订后的转写重建" % _now()]

    parts = _chapter_docs(bodies, extras)
    merged = "\n".join(parts).strip() + "\n"
    names = [bodies[i][0] or ("第 %d 章" % (i + 1)) for i in range(len(bodies))]

    # 先把要写的东西全算出来，再动旧文件：中间出错的话书还是原来那本
    backup, moved = _backup_dir(book_dir), []
    ch_dir = os.path.join(book_dir, "chapters")
    for name in ("chapters", TRANS_TXT, "_catalog.json", "meta.json", "notes.md"):
        src = os.path.join(book_dir, name)
        if os.path.exists(src):
            shutil.move(src, os.path.join(backup, name))
            moved.append(name)
    for name in (TRANS_FILE, SUMMARY_FILE, "notes.json"):
        src = os.path.join(book_dir, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(backup, name))    # 这三份不重写，留个参照
    try:
        os.makedirs(ch_dir, exist_ok=True)
        for index, part in enumerate(parts):
            _write_text(os.path.join(ch_dir, "%04d.md" % index), part)
        _write_json(os.path.join(book_dir, "_catalog.json"), names)
        _write_text(os.path.join(book_dir, TRANS_TXT), text + "\n")
        _write_text(os.path.join(book_dir, "merged.md"), merged)

        total = len(parts)
        prog = _read_json(os.path.join(book_dir, "_progress.json"))
        prog = prog if isinstance(prog, dict) else {}
        _write_json(os.path.join(book_dir, "_progress.json"),
                    {"at": min(_as_int(prog.get("at")) or total, total), "max": total})

        words = len(re.sub(r"\s", "", text))
        # 这里不碰 transcript_edited：重建章节和「用户改过字」是两件事，刚转完直接重建的书
        # 也走这条路，替它立一盏「手改」灯就是撒谎。
        meta.update({"words": words, "chars": words, "segments": len(segments),
                     "rebuilt_at": _now(),
                     "chapters": total, "updated_at": int(time.time())})
        if trans.get("engine"):
            meta["asr_engine"] = trans["engine"]
        if trans.get("language"):
            meta["language"] = trans["language"]
        if trans.get("duration") is not None:
            meta["duration"] = trans["duration"]
        _write_json(os.path.join(book_dir, "meta.json"), meta)

        saved = book_notes.load_notes(book_dir)
        out = book_notes.export_notes(book_dir, saved, meta=meta,
                                      titles=book_notes.chapter_titles(book_dir),
                                      book_label=source["title"])
    except Exception:
        _restore_backup(book_dir, backup, moved)
        raise
    return {"ok": True, "book_dir": book_dir, "chapters": total, "words": words,
            "backup": backup, "moved": moved, "merged_path": os.path.join(book_dir, "merged.md"),
            "transcript_path": os.path.join(book_dir, TRANS_TXT),
            "notes_path": out.get("path") or "", "msg": "重建好了，旧文件在备份目录里"}


def _cue_doc(segments, sep):
    """段落 → 字幕正文：空段跳过、序号连续，时间码用 sep 分隔秒与毫秒。

    序号只在真要写出一条时才自增 —— 空段占号会让字幕编号跳着走，播放器能忍，
    但对时间轴的人忍不了。
    """
    out, index = [], 0
    for seg in segments:
        text = " ".join(str(seg.get("text") or "").split())      # 段内换行压成空格
        if not text:
            continue
        index += 1
        begin = _sec(seg.get("start"))
        finish = max(begin, _sec(seg.get("end")))
        speaker = str(seg.get("speaker") or "").strip()
        if speaker:
            # 说话人：VTT 用它认得的语音标记，SRT 没标准就用方括号摆在前头
            text = ("<v %s>%s</v>" % (speaker, text)) if sep == "." else "[%s] %s" % (
                speaker, text)
        out.append("%d\n%s --> %s\n%s\n" % (index, _cue_time(begin, sep),
                                           _cue_time(finish, sep), text))
    return "\n".join(out)


def export_transcript(book_dir, fmt="srt", out_path=""):
    """转写导出成字幕 / 纯文本 / Markdown / JSON，落盘并返回能直接下载的信息。

    默认落在书目录下的 exports/ 里（跟书走，拷走这本书就拷走了它的导出物），
    而不是 transcript.txt 那种有正用的文件 —— 那些名字另有职责，覆盖了就出事。
    """
    fmt = str(fmt or "srt").strip().lower().lstrip(".")
    if fmt not in EXPORT_EXT:
        raise ValueError("导出格式只认 %s，别的还不会" % " / ".join(sorted(EXPORT_EXT)))
    trans = load_transcript(book_dir)
    meta = _read_meta(book_dir)
    title = (meta.get("page_title") or meta.get("title") or trans.get("title")
             or "转写")
    segments = [s for s in trans["segments"] if str(s.get("text") or "").strip()]
    timed = bool(trans["timed"])

    if fmt == "srt":
        body = _cue_doc(segments, ",")
    elif fmt == "vtt":
        body = "WEBVTT\n\n" + _cue_doc(segments, ".")
    elif fmt == "txt":
        body = _join_text(segments) + "\n"
    elif fmt == "md":
        lines = ["# %s　·　转写" % title,
                 "> %d 段　·　%s　·　导出于 %s" % (
                     len(segments), trans.get("engine") or "转写引擎未记", _now()), ""]
        for seg in segments:
            stamp = "**[%s]** " % _stamp(seg["start"]) if (
                timed and seg.get("start") is not None) else ""
            speaker = "%s：" % seg["speaker"] if str(seg.get("speaker") or "").strip() else ""
            lines.append("%s%s%s" % (stamp, speaker, str(seg.get("text") or "").strip()))
            lines.append("")
        body = "\n".join(lines).rstrip() + "\n"
    else:
        body = json.dumps({"schema": TRANS_SCHEMA, "source": trans["source"],
                           "engine": trans.get("engine") or "",
                           "language": trans.get("language") or "",
                           "duration": trans.get("duration"),
                           "title": title, "timed": timed,
                           "segments": segments, "text": trans["text"]},
                          ensure_ascii=False, indent=2) + "\n"

    if not out_path:
        target = os.path.join(book_dir, EXPORT_DIR,
                              "%s.%s" % (_safe_file(title), EXPORT_EXT[fmt]))
    else:
        target = out_path
    holder = os.path.dirname(os.path.abspath(target))
    os.makedirs(holder, exist_ok=True)
    path = os.path.abspath(target)
    _write_text(path, body)
    return {"ok": True, "format": fmt, "path": path, "name": os.path.basename(path),
            "bytes": len(body.encode("utf-8")),
            "chars": len(re.sub(r"\s", "", body)), "segments": len(segments),
            "timed": timed, "source": trans["source"], "book_dir": book_dir}



if __name__ == "__main__":
    # 手动冒烟：.venv/bin/python video_note.py <视频链接> [输出目录]
    # 界面拉起：.venv/bin/python video_note.py --task <链接>（选项走 env）
    import signal
    import sys

    args = [a for a in sys.argv[1:] if a]

    if "--task" in args:
        # 界面那条正规路：选项用 env 传（命令行会把 JSON 里的引号再折腾一遍），
        # 结果用 RESULT_MARK 一行交回 ui_server，日志照旧打给「进展」抽屉看。
        i = args.index("--task")
        target = (args[i + 1] if i + 1 < len(args) else "").strip()
        try:
            opts = json.loads(os.environ.get("GUIZANG_VIDEO_OPTS") or "{}")
        except Exception:
            opts = {}
        if not isinstance(opts, dict):
            opts = {}

        # 中止：ui_server 的 stop_task 发的是 SIGTERM。这里把它接住转成「请求停止」，
        # 让流水线在阶段之间自己收尾（下好的音频和转好的分片留在临时目录里，
        # 下次重跑同一条链接会接着用），而不是被就地打死、留下一本半截的书。
        stop = {"v": False}

        def _ask_stop(*_):
            stop.update(v=True)
            print("--- 收到停止请求，正在收尾 ---", flush=True)

        try:
            signal.signal(signal.SIGTERM, _ask_stop)
            signal.signal(signal.SIGINT, _ask_stop)
        except Exception:
            pass          # Windows 上没有 SIGTERM，退化成硬杀，不为此报错

        def echo(stage, pct, note):
            print("[%-8s %3s%%] %s" % (stage, "" if pct is None else pct, note), flush=True)

        url_cfg, key_cfg, model_cfg = _agent_cfg_from_disk()
        root = (os.environ.get("GUIZANG_OUTPUT") or "").strip() or pc.books_dir(REPO)
        try:
            res = run(target, root, {
                "asr": opts.get("asr") or {},
                "language": opts.get("language") or "",
                "page": opts.get("page"),
                "resume": opts.get("resume"),
                "llm": {"url": url_cfg, "key": key_cfg, "model": model_cfg},
                "should_stop": lambda: stop["v"],
            }, echo)
            book = res.get("book") or {}
            # 前端做转写编辑器要的东西都在这：段数、时长、语言、三份文件的路径 ——
            # 拿到就能直接开编辑器和下载按钮，不用再回读盘。
            payload = {"ok": True, "book_id": book.get("id", ""),
                       "title": book.get("title", ""), "words": res.get("words", 0),
                       "chapters": res.get("chapters", 0),
                       "asr_engine": res.get("asr_engine", ""),
                       "mindmap": bool(res.get("mindmap_path")),
                       "ai_error": res.get("ai_error", ""),
                       "elapsed": res.get("elapsed", 0),
                       "segments": res.get("segments", 0),
                       "duration": res.get("duration", 0),
                       "language": res.get("language", ""),
                       "page": book.get("page") or 1,
                       "pages": book.get("pages") or 1,
                       "book_dir": book.get("dir", ""),
                       "transcript_path": res.get("transcript_path", ""),
                       "transcript_json": res.get("transcript_json", ""),
                       "merged_path": res.get("merged_path", "")}
        except Aborted:
            payload = {"ok": False, "aborted": True, "msg": "已中止"}
        except Exception as e:
            print("--- 视频转笔记失败：%s: %s ---" % (type(e).__name__, e), flush=True)
            for line in traceback.format_exc().strip().splitlines()[-4:]:
                print("    " + line, flush=True)
            payload = {"ok": False, "msg": str(e)[:200] or "这条视频没能转成笔记"}
        print(RESULT_MARK + json.dumps(payload, ensure_ascii=False), flush=True)
        sys.exit(0 if payload.get("ok") else 1)

    if not args:
        print("用法: video_note.py <视频链接> [输出目录]  |  video_note.py --task <链接>")
        sys.exit(1)
    target = args[0]
    root = args[1] if len(args) > 1 else os.path.join(DATA_DIR, "output")

    def echo(stage, pct, note):
        print("[%-8s %3s%%] %s" % (stage, "" if pct is None else pct, note), flush=True)

    url_cfg, key_cfg, model_cfg = _agent_cfg_from_disk()
    print(json.dumps(plan(target), ensure_ascii=False, indent=2))
    print(json.dumps(run(target, root, {"llm": {"url": url_cfg, "key": key_cfg,
                                                "model": model_cfg}},
                         echo), ensure_ascii=False, indent=2))