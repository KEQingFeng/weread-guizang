#!/usr/bin/env python3
"""转写引擎与模型：装什么、下什么、下到哪、现在走到哪一步。

「视频转笔记」这条线要三样东西，各自独立，也都不改系统：

  · ffmpeg      —— 见 ffmpeg_tool.py（这里只把它算进总状态）
  · 转写引擎    —— mlx-whisper（Apple 芯片）/ faster-whisper（其它），装进 .venv
  · 转写模型    —— 引擎第一次用得着的权重（几百 MB ~ 1.6GB），落在 Hugging Face 缓存

分工的口径：**引擎随安装流程走（几十 MB，装完就进门），模型放后台下（GB 级，
不该把人拦在门口十几分钟）**。所以 bootstrap 只到「引擎就绪」为止；模型由
ui_server 起一个后台线程拉，视频页那几盏灯显示进度。

这个模块在导入时只用标准库、不碰网络、不 import 任何重型包 —— 装引擎的时候项目里
可能还什么都没有，它得能在裸解释器上被 import 自己。
"""
import importlib.util
import os
import platform
import subprocess
import sys
import threading
import time

import platform_compat as pc

HERE = os.path.dirname(os.path.abspath(__file__))

# 两个引擎各自的：pip 包名、import 名、默认模型。模型那栏对 mlx 是 Hugging Face
# 的仓库名；对 faster-whisper 是它自己的短名（small / medium…），缓存在哪个目录
# 得再翻一次（见 repo_id）。
ENGINES = {
    "mlx": {"pip": "mlx-whisper", "mod": "mlx_whisper",
            "model": "mlx-community/whisper-large-v3-turbo",
            "why": "Apple 芯片本机加速，中文准且快"},
    "faster": {"pip": "faster-whisper", "mod": "faster_whisper",
               "model": "small",
               "why": "CPU 通用，哪台机器都跑得动"},
}
MIRROR = "https://hf-mirror.com"          # 直连不通时的国内镜像


# ─────────────────────────── 是什么、有没有 ───────────────────────────

def is_apple_silicon():
    return sys.platform == "darwin" and platform.machine() == "arm64"


def engine():
    """这台机器该用哪个引擎。用户在设置里点过就听他的（GUIZANG_ASR_ENGINE）。"""
    explicit = (os.environ.get("GUIZANG_ASR_ENGINE") or "").strip().lower()
    if explicit in ENGINES:
        return explicit
    return "mlx" if is_apple_silicon() else "faster"


def engine_name(key=None):
    key = key or engine()
    return "%s（%s）" % (ENGINES[key]["pip"], ENGINES[key]["why"])


def model_id(key=None):
    return ENGINES[key or engine()]["model"]


def repo_id(key=None):
    """模型在 Hugging Face 上的仓库名。

    faster-whisper 认的是「small」这种短名，但缓存目录叫
    models--Systran--faster-whisper-small —— 不翻这一步就找不到自己下到哪了。
    """
    mid = model_id(key)
    return mid if "/" in mid else "Systran/faster-whisper-" + mid


def engine_ready(key=None):
    """引擎装了没。用 find_spec 而不是真 import：mlx_whisper 一 import 就是几百毫秒，
    状态栏是轮询的，耗不起；坏了的话真跑的时候会报出来。"""
    try:
        return importlib.util.find_spec(ENGINES[key or engine()]["mod"]) is not None
    except Exception:
        return False


def hf_hub_dir():
    """Hugging Face 的缓存目录。认官方那三个环境变量，缺省跟系统约定走。"""
    d = (os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE") or "").strip()
    if d:
        return os.path.expanduser(d)
    home = (os.environ.get("HF_HOME") or "").strip()
    base = os.path.expanduser(home) if home else os.path.expanduser("~/.cache/huggingface")
    return os.path.join(base, "hub")


def model_dir(key=None):
    return os.path.join(hf_hub_dir(), "models--" + repo_id(key).replace("/", "--"))


def _weights_in(path):
    """目录里有没有真的权重文件。

    只看「目录在不在」不够：下一半断了也会留个空壳，看着像装好了，真去转写才报错。
    """
    for root, _dirs, files in os.walk(path):
        for f in files:
            if f.endswith((".safetensors", ".bin", ".npz", ".pt", ".gguf")):
                return True
    return False


def model_ready(key=None):
    d = model_dir(key)
    return os.path.isdir(d) and _weights_in(d)


def status():
    """给界面看的一行状态：引擎和模型到没到、有没有正在忙、忙到几成。"""
    key = engine()
    with _ST["lock"]:
        st = {k: _ST[k] for k in ("busy", "pct", "got", "total", "note", "err")}
    st.update({
        "engine": key,
        "engine_pkg": ENGINES[key]["pip"],
        "engine_ready": engine_ready(key),
        "model": model_id(key),
        "model_ready": model_ready(key),
        "hub": hf_hub_dir(),
        "mirror": (os.environ.get("HF_ENDPOINT") or "").strip(),
        "apple": is_apple_silicon(),
    })
    return st


# ─────────────────────────── 装引擎 ───────────────────────────

def install_engine(key=None, run=None, say=None):
    """把转写引擎装进 .venv；已经装了就跳过。返回 (ok, 人话)。

    run 传的是 bootstrap 的 run(argv) —— 复用它的流式输出和代理环境，装包那几分钟
    界面上那条进展栏才不会像卡死。不传就自己 subprocess。
    """
    key = key or engine()
    if engine_ready(key):
        return True, "转写引擎已就绪"
    pkg = ENGINES[key]["pip"]
    if say:
        say("      %s" % engine_name(key))
    argv = [pc.venv_python(HERE), "-m", "pip", "install", pkg]
    code = run(argv) if run else subprocess.call(argv, cwd=HERE)
    if code != 0:
        return False, "转写引擎没装上（%s），挂上代理再点一次通常就好" % pkg
    if engine_ready(key):
        return True, "转写引擎已就绪"
    return False, "装完了，但 %s 仍导入不了 —— 多半是这台机器的 Python 太新，先手动 pip 看看" % pkg


# ─────────────────────────── 下模型 ───────────────────────────

def _set_endpoint(url):
    """把 HF 端点切到镜像。

    只改 os.environ 不够：huggingface_hub 在自己 import 的那一刻就把 HF_ENDPOINT
    读进 constants.ENDPOINT 了，之后再改环境变量它看不见。所以两处一起改。
    """
    os.environ["HF_ENDPOINT"] = url
    try:
        import huggingface_hub.constants as C
        C.ENDPOINT = url
    except Exception:
        pass


def _repo_bytes(repo):
    """仓库总字节数，用来算百分比。问不到就返回 0，界面改成只报「已下多少」。"""
    try:
        from huggingface_hub import HfApi
        info = HfApi().model_info(repo, files_metadata=True)
        return sum(int(getattr(f, "size", 0) or 0) for f in (info.siblings or []))
    except Exception:
        return 0


def _dir_bytes(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _short(err, limit=160):
    return "%s: %s" % (type(err).__name__, str(err)[:limit])


def download_model(key=None, on_step=None):
    """把转写模型的权重拉进 HF 缓存。返回 (ok, 人话)。

    国内直连 Hugging Face 常拉不动：第一次失败、而用户自己没设过 HF_ENDPOINT 时，
    自动换 hf-mirror 再试一遍 —— 这是「装完不用管」里最要紧的一步。
    """
    key = key or engine()
    repo = repo_id(key)
    if model_ready(key):
        return True, "转写模型已就绪"
    try:
        from huggingface_hub import snapshot_download
    except Exception:
        return False, "缺 huggingface_hub —— 先跑一次「补齐组件」把转写引擎装上"

    total = _repo_bytes(repo)
    stop = threading.Event()

    def watch():
        while not stop.wait(1.0):
            got = _dir_bytes(model_dir(key))
            if on_step:
                on_step(got, total, repo)

    if on_step:
        threading.Thread(target=watch, daemon=True).start()

    def attempt(endpoint):
        if endpoint:
            _set_endpoint(endpoint)
        try:
            try:
                snapshot_download(repo_id=repo, **({"endpoint": endpoint} if endpoint else {}))
            except TypeError:
                # 老版本 snapshot_download 不吃 endpoint，靠上面改 constants 兜住
                snapshot_download(repo_id=repo)
            return True, ""
        except Exception as e:
            return False, _short(e)

    user_endpoint = (os.environ.get("HF_ENDPOINT") or "").strip()
    ok, err = attempt("")                 # 空串＝不动端点，用用户自己的（或默认直连）
    tried = "直连"
    if not ok and not user_endpoint:
        ok2, err2 = attempt(MIRROR)
        tried = "直连与镜像"
        if ok2:
            ok, err = True, ""
        else:
            err = err + "；镜像也没成：" + err2
    stop.set()

    if not ok:
        return False, "转写模型没下下来（%s 都试过）。%s" % (tried, err)
    if not model_ready(key):
        return False, "转写模型下完了但在缓存里找不到权重，再点一次试"
    if on_step:
        on_step(_dir_bytes(model_dir(key)), total, repo)
    return True, "转写模型已就绪（%s）" % repo


# ─────────────────────────── 后台一次做完 ───────────────────────────

_ST = {"lock": threading.Lock(), "busy": "", "pct": -1, "got": 0, "total": 0,
       "note": "", "err": ""}


def _set(**kw):
    with _ST["lock"]:
        _ST.update(kw)


def start(kind="auto"):
    """起一个后台线程把引擎/模型补齐。已经在忙就不叠第二个（装包和下模型抢磁盘没好处）。

    kind：auto=引擎没有就先装、再下模型；engine / model=只做那一件。
    返回 (是否起上了, 人话)。
    """
    with _ST["lock"]:
        if _ST["busy"]:
            return False, "已经在准备了"
        _ST.update({"busy": kind, "pct": -1, "got": 0, "total": 0,
                    "note": "正在准备", "err": ""})
    threading.Thread(target=_work, args=(kind,), daemon=True).start()
    return True, "已开始准备"


def _work(kind):
    steps = ("engine", "model") if kind in ("auto", "all") else (kind,)
    try:
        for step in steps:
            if step == "engine":
                if engine_ready():
                    _set(note="转写引擎已就绪")
                    continue
                _set(note="正在装转写引擎")
                ok, msg = install_engine()
                if not ok:
                    _set(busy="", pct=-1, note="", err=msg)
                    return
            else:
                if model_ready():
                    _set(note="转写模型已就绪", pct=100)
                    continue
                _set(note="正在下转写模型", pct=-1)
                ok, msg = download_model(on_step=_on_bytes)
                if not ok:
                    _set(busy="", pct=-1, note="", err=msg)
                    return
        _set(busy="", pct=100, note="都准备好了", err="")
    except Exception as e:                      # 后台线程里的异常没人接，落进状态里
        _set(busy="", pct=-1, note="", err="准备转写组件时出错：%s" % _short(e))


def _on_bytes(got, total, _repo=""):
    _set(got=got, total=total, pct=(int(got * 100 / total) if total else -1))


def auto_start_enabled():
    """要不要在服务起来时自动补。只有壳（安装包）会设这个开关。

    源码直接跑时不自动下 —— 一次 1.6GB 的下载不该因为「起了一下服务」就发生。
    """
    return (os.environ.get("GUIZANG_AUTO_MEDIA") or "").strip() == "1"


def maybe_auto_start():
    """服务启动时调一次：开关开着、且确实缺东西，就补。不缺就什么都不做。"""
    if not auto_start_enabled():
        return False, "没开自动准备（源码运行）"
    if engine_ready() and model_ready():
        return False, "转写组件已经齐了"
    return start("auto")
