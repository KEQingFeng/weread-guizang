#!/usr/bin/env python3
"""视频转写「只剩云端」这条线的离线自测：挑选引擎 / 配置存取 / 接线，全程不联网。

1.0.8 把本地 Whisper（mlx-whisper / faster-whisper）整套砍了，转写只剩云端一路
（OpenAI 兼容的 /audio/transcriptions）。这一条守的就是「砍干净了、且没砍漏」：

  1. 挑引擎：auto / cloud 都归云端；mlx / faster 这类老字面量必须当面报错，
     不许静默改道 —— 悄悄改道会让用户以为本地那条路还在。
  2. 没填地址就报人话（「点了没反应」是这一行最常见的病）。
  3. 配置存取：引擎恒为云端；Key 只报「有没有 / 末四位」，明文既不上界面也不进日志。
  4. 接线：video_note 不再 import media_setup；bootstrap 回到四步且不空口提本地模型；
     打包清单里没有 media_setup.py；壳里那个 GUIZANG_AUTO_MEDIA 开关已摘掉；
     界面里有接口地址 / Key / 模型三格。

为什么必须有这一条：整块模块被删掉时，最危险的不是「删多了崩」，而是「删漏了 ——
某处还留着对已删模块的引用」，那种错在开发机上可能一路静默到最后一次打包才炸。

配置那一节全在临时沙盒里读写（先把 GUIZANG_DATA 指过去再 import ui_server），
绝不碰用户真实的 cache/config.json。
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 先把数据目录指进临时沙盒，再 import 任何会读它的模块 —— 配置都是「导入时算一次」，
# 顺序反了就写到用户真实的 config.json 里去了。
SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-asr-"))
os.environ["GUIZANG_DATA"] = str(SB)
os.environ["GUIZANG_BOOKS"] = str(SB / "books")

import video_note as vn   # noqa: E402
import ui_server as us    # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
        return None
    except Exception as e:      # noqa: BLE001 —— 只要报错就算对，具体类型不苛求
        return str(e)


# ── 1. 挑引擎：只有云端一条路 ─────────────────────────────────
cfg_ok = {"cloud": {"url": "https://asr.example.com/v1", "key": "k", "model": "whisper-1"}}
chk("E1 auto 归云端", vn.pick_engine("auto", cfg_ok) == "cloud", vn.pick_engine("auto", cfg_ok))
chk("E2 cloud 就是云端", vn.pick_engine("cloud", cfg_ok) == "cloud")
chk("E2b 空值当 auto（老配置没这一项也一样走云端）", vn.pick_engine("", cfg_ok) == "cloud")
for old in ("mlx", "faster", "MLX", "Faster"):
    msg = raises(vn.pick_engine, old, cfg_ok) or ""
    chk("E3 老引擎 %s 当面报「本地已移除」（不静默改道）" % old, "移除" in msg, msg)
chk("E4 认不出的引擎也报错（不静默吞）", bool(raises(vn.pick_engine, "no-such", cfg_ok)))
msg = raises(vn.pick_engine, "cloud", {"cloud": {"url": ""}}) or ""
chk("E5 云转写没配地址 → 报人话且点明去哪儿填", "地址" in msg and "设置" in msg, msg)
chk("E6 缺 cloud 段也不炸、当没配处理", bool(raises(vn.pick_engine, "cloud", {})))
chk("E7 填了地址就放行", vn.pick_engine("cloud", cfg_ok) == "cloud")

# ── 2. available()：形状里不许再有本地模型 ────────────────────
av = vn.available()
chk("A1 available 不抛且是 dict", isinstance(av, dict), av)
chk("A2 asr 明说只有云端一路", av.get("asr") == {"cloud": True}, av.get("asr"))
chk("A3 engines 只剩 cloud", av.get("engines") == ["cloud"], av.get("engines"))
chk("A4 不再报本地模型的引擎/权重状态",
    all(k not in av for k in ("engine", "engine_ready", "model", "model_ready")), sorted(av))
chk("A5 下载器 / ffmpeg / llm 三盏灯还在（没误删）",
    all(k in av for k in ("ytdlp", "ffmpeg", "llm")), sorted(av))

# ── 3. 转写地址补齐（允许只填域名或 /v1） ──────────────────────
T = vn._transcriptions_url
chk("U1 只填域名 → 补 /v1/audio/transcriptions",
    T("https://api.openai.com") == "https://api.openai.com/v1/audio/transcriptions",
    T("https://api.openai.com"))
chk("U2 填到 /v1 → 补 /audio/transcriptions",
    T("https://x.com/v1") == "https://x.com/v1/audio/transcriptions", T("https://x.com/v1"))
chk("U3 已是完整端点 → 原样",
    T("https://x.com/v1/audio/transcriptions") == "https://x.com/v1/audio/transcriptions")
chk("U4 非 http(s) → 空（界面据此报地址写错）", T("asr.example.com") == "", T("asr.example.com"))

# ── 4. 配置存取：引擎恒云端、Key 只许出尾号 ────────────────────
us.set_media_cfg("cloud", "zh", url="https://asr.example.com/v1",
                 key="sk-secret-abcd1234", model="whisper-1")
v = us.asr_cfg_view()
blob = json.dumps(v, ensure_ascii=False)
chk("C1 配置视图回 engine/url/model/lang",
    v.get("engine") == "cloud" and v.get("url") == "https://asr.example.com/v1"
    and v.get("model") == "whisper-1" and v.get("lang") == "zh", v)
chk("C2 明文 Key 绝不进配置视图（只给 key_set / key_tail）",
    "secret" not in blob and v.get("key_set") is True and v.get("key_tail") == "1234", v)
# 老配置里残留的 mlx / faster：保存时不拦（不报错），一律归正到 cloud
us.set_media_cfg("faster", "zh")
chk("C3 老引擎值保存不报错、归正为 cloud", us.asr_cfg_view()["engine"] == "cloud")
# 不传某项 = 不动它；传空串 = 清掉
us.set_media_cfg("cloud", "zh", model="")
chk("C4 model 传空串 = 清掉这一项", us.asr_cfg_view()["model"] == "", us.asr_cfg_view())
us.set_media_cfg("cloud", "zh")
chk("C5 url/key 默认不传 = 原样留着", us.asr_cfg_view()["url"] == "https://asr.example.com/v1",
    us.asr_cfg_view())
opts = us.asr_opts_for_task({"cloud": {"model": "large-v3"}})
chk("C6 asr_opts_for_task 以本机配置为底、body 覆盖在上面",
    opts["engine"] == "cloud" and opts["cloud"]["url"] == "https://asr.example.com/v1"
    and opts["cloud"]["model"] == "large-v3" and opts["cloud"]["key"] == "sk-secret-abcd1234",
    opts)

# ── 5. 接线：源文件里不许再留本地模型那套 ──────────────────────
src = (ROOT / "video_note.py").read_text(encoding="utf-8")
chk("W1 video_note 不再 import media_setup", "media_setup" not in src)
chk("W2 video_note 里没有 mlx / faster 转写实现残留",
    all(x not in src for x in ("_transcribe_mlx", "_transcribe_faster", "_is_apple_silicon")))

bs = (ROOT / "bootstrap.py").read_text(encoding="utf-8")
chk("W3 bootstrap 回到四步（标签也统一成 /4，不留半截的 /5）",
    "[4/4]" in bs and "/5]" not in bs)
chk("W4 bootstrap 不再 import / 提 media_setup", "media_setup" not in bs)
chk("W5 bootstrap 说明转写走云端", "云端" in bs)

build = (ROOT / "shell" / "build_macos.sh").read_text(encoding="utf-8")
chk("W6 打包清单里没有 media_setup.py（模块已删，留着就是打不出的空引用）",
    "media_setup.py" not in build)

swift = (ROOT / "shell" / "main.swift").read_text(encoding="utf-8")
chk("W7 壳里那个自动准备开关已摘（没有模块读它了）", "GUIZANG_AUTO_MEDIA" not in swift)
chk("W8 壳里探组件改成看 ffmpeg（不再问引擎装没装）",
    "videoToolsReady" in swift and "ffmpeg_tool" in swift and "mediaEngineReady" not in swift)

srv = (ROOT / "ui_server.py").read_text(encoding="utf-8")
chk("W9 后端不再 import media_setup", "import media_setup" not in srv)
chk("W10 后端有 set_media_cfg / asr_cfg_view / asr_opts_for_task 三件",
    all(("def %s(" % n) in srv for n in ("set_media_cfg", "asr_cfg_view", "asr_opts_for_task")))
chk("W11 后端本地模型那几个动作一律回「已移除」", "本地转写模型已移除" in srv)
chk("W12 保存转写配置会作废那 30 秒的可用性缓存（否则灯要再绿不绿半分钟）",
    "video_avail_forget" in srv)

html = (ROOT / "ui.html").read_text(encoding="utf-8")
chk("W13 界面有「接口地址 / API Key / 模型」三格",
    all(('id="%s"' % i) in html for i in ("asrUrl", "asrKey", "asrModel")))
chk("W14 界面上不再有本地引擎那张选单", "VID_ENGINES" not in html)
chk("W15 界面上不再有「转写模型」那盏本地灯与补件钮",
    "转写模型" not in html and "vModelFix" not in html and "vidPrep" not in html)
chk("W16 界面上不再提 mediaEngineReady / 派活装本地模型",
    "mediaEngineReady" not in html and "media_engine" not in html)

mcp = (ROOT / "mcp" / "guizang-mcp.mjs").read_text(encoding="utf-8")
# MCP 那一层也得说真话：a.asr.cloud 是能力位（恒真），拿它当「可用」就是给 Agent 亮假绿灯，
# 于是 Agent 以为能转、发起任务才失败。这一盏灯要看的是配置里地址填没填。
chk("W17 适配器那盏「云端语音识别」看的是地址填没填，不是写死的绿灯",
    "asrOk" in mcp and "asr.key_tail" in mcp)
chk("W18 适配器不再拿 a.asr.cloud 当「可用」（那是能力位，恒真）",
    "(a.asr || {}).cloud" not in mcp)

shutil.rmtree(SB, ignore_errors=True)
print()
print("转写云端化：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  挑引擎、配置存取、地址补齐与四处接线全部验通过")
