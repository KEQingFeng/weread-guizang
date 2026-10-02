#!/usr/bin/env python3
"""按需给归藏在 macOS 上备一份 ffmpeg / ffprobe。

为什么单独一个文件：「视频转笔记」这条线（抽音轨、转码、量时长）离了 ffmpeg
一步都走不动，而这台机器上没有它、也没有 Homebrew。让用户自己去官网下、或者先
装一个 brew 再 brew install，都不是一个「双击即用」的 app 该提的要求。于是照
bootstrap.py 那套思路：下载的东西全落在应用自己的数据目录里（源码直接跑时就是
项目目录，装成 app 时由 GUIZANG_DATA 指到用户目录），不写系统目录、不碰应用包、
不依赖 Homebrew。

下载源按顺序试，前一个失败才轮到下一个：

  1. martin-riedl.de —— 静态 macOS arm64 构建。它首页自己写明
     /redirect/latest/macos/arm64/snapshot/{ffmpeg,ffprobe}.zip 总指向最新一份
     快照。这个端点是实测出来的，不是猜的（猜一个地址写死在代码里，一旦它过期
     就是用户那边「装组件」点了没反应）。
  2. evermeet.cx —— 兜底。只有 x86_64（Intel）构建，Apple 芯片上要靠 Rosetta
     才跑得起来；好处是它有个 JSON 索引能问到确切版本号。

「装上了没有」只认一个证据：把二进制真的跑一次，从它嘴里听到「ffmpeg version」
这一行。HTTP 回了 200、zip 解开没报错，都不算数 —— 半截下载、被运营商插进来的
提示页、错架构的包，都能骗过前者，而用户看到的会是「装成功之后又说找不到」。
"""
import json
import os
import re
import shutil
import subprocess
import threading
import urllib.request
import zipfile

import platform_compat as pc

REPO = os.path.dirname(os.path.abspath(__file__))

# 这两个是一起用的：ffmpeg 干活，ffprobe 量时长。
BINARIES = ("ffmpeg", "ffprobe")

# PATH 里没有 ffmpeg 时，再往这几个 macOS 上常见的装法处找一眼。
# 用模块级常量而不是写死在函数里，是因为测试要能把它清空来验「真的没有」这条分支。
COMMON_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")

# 下载用的 UA。跟 clip_article.py 那份一致：一部分站点会对非浏览器 UA 收紧策略。
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
TIMEOUT = 60

# 同一时刻只允许一份下载在跑，不然两次请求会各自下半个 zip、互相覆盖。
_dl_lock = threading.Lock()
_downloading = False


def tools_dir():
    """下载下来的二进制放哪。

    放在数据目录的 cache/ 下，跟 .gitignore 挡掉的那一片对齐。这样有两个好处：
    装成 app 时它落在用户目录、应用包之外（包体自始至终只读）；源码直接跑时是
    <项目>/cache/tools，不会跟仓库根那个 tools/（打包、出图标这类开发脚本）撞名。
    """
    return os.path.join(pc.data_dir(REPO), "cache", "tools")


def _is_exe(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _resolve(name):
    """找一个能用的 <name>，返回 (路径, 来源)。来源只有 bundled / system 两种，没有就是空串。

    顺序是有讲究的：先看归藏自己下的那份（那是「装组件」装进来的，最该用），
    再看 PATH，最后看几个常见安装位置。任何一步都可能什么都没有，所以结尾返回
    (None, "")，由调用方决定是报错还是当作没找到。
    """
    local = os.path.join(tools_dir(), name)
    if _is_exe(local):
        return local, "bundled"
    hit = shutil.which(name)
    if hit:
        return hit, "system"
    for base in COMMON_DIRS:
        p = os.path.join(base, name)
        if _is_exe(p):
            return p, "system"
    return None, ""


def _version_text(path):
    """跑一次 -version，把它的输出原样拿回来。跑不起来返回空串，不抛。"""
    try:
        out = subprocess.run([path, "-version"], capture_output=True, text=True,
                             errors="replace", timeout=15)
    except Exception:
        return ""
    return (out.stdout or "") + (out.stderr or "")


def _version_of(path):
    """从 -version 的输出里抠出版本号（ffmpeg version 后面那个词）。抠不到返回空串。"""
    m = re.search(r"version\s+(\S+)", _version_text(path))
    return m.group(1) if m else ""


def status():
    """ffmpeg 现在能不能用。永远不抛，找不到就是 found=False。

    界面「设置」那一栏拿它显示状态；这里一旦抛异常，接口会 500，用户看到的是
    「点了没反应」而不是「还没装」—— 那正是这个项目反复栽的那种静默失败。
    """
    out = {"found": False, "path": None, "ffprobe": None, "source": "", "version": ""}
    try:
        path, source = _resolve("ffmpeg")
        if path:
            out["found"] = True
            out["path"] = path
            out["source"] = source
            out["version"] = _version_of(path)
        probe, _ = _resolve("ffprobe")
        out["ffprobe"] = probe
    except Exception:
        pass
    return out


def ffmpeg_path():
    """ffmpeg 的路径；没有就抛人话，告诉用户怎么把这东西补上。"""
    path, _ = _resolve("ffmpeg")
    if not path:
        raise ValueError("还没找到 ffmpeg。点一下「装组件」让归藏自己下一份（约 30MB），"
                         "或者自己装一个放进 PATH（brew install ffmpeg）。")
    return path


def ffprobe_path():
    """ffprobe 的路径；它跟 ffmpeg 通常一起装，所以错话也一起说。"""
    path, _ = _resolve("ffprobe")
    if not path:
        raise ValueError("还没找到 ffprobe。它一般跟 ffmpeg 一起装，"
                         "点「装组件」让归藏自己下一份，或者 brew install ffmpeg。")
    return path


def ensure_on_path():
    """把归藏自己下那份 ffmpeg 的目录塞进 PATH，返回有没有真动过。

    为什么得有这一步：归藏把 ffmpeg 下在 cache/tools/ 里，这个目录天生不在 PATH
    上。归藏自己调 ffmpeg 时都是走 ffmpeg_path() 拿绝对路径，所以一直没露馅；可
    mlx-whisper 内部是直接 shell 出 'ffmpeg' 三个字去抽音轨的 —— 于是就成了
    「组件明明装好了，转写却报 No such file or directory: 'ffmpeg'」。
    修法不是去改第三方库（改了就跟着它一起烂），而是开跑 ASR 之前把它要的那个
    目录放进 PATH，子进程自然也找得到。faster-whisper 走 PyAV 解码，不依赖它，
    这一步对它只是无害。

    只放归藏自己下的那份：系统里本来就有 ffmpeg 的话，PATH 上早就有了，轮不到
    我们插手，硬塞反而可能把用户自己选的版本挤掉。
    """
    path, source = _resolve("ffmpeg")
    if not path or source != "bundled":
        return False
    folder = os.path.dirname(path)
    parts = (os.environ.get("PATH") or "").split(os.pathsep)
    if folder in parts:
        return False
    os.environ["PATH"] = folder + os.pathsep + (os.environ.get("PATH") or "")
    return True


def downloading():
    """现在是不是正在下一份。

    界面用这个把「装组件」按钮按住；后端起下载前也拿它挡一下，省得两次请求
    各下半个 zip 互相覆盖，最后留一个跑不起来的半成品在那儿冒充装好了。
    """
    return _downloading


def probe_duration(path):
    """量一个媒体文件的时长（秒）。量不出来返回 None —— 它不该拖垮主流程。

    ffprobe 只管把时长念出来，任何一步不对劲（没装、文件是坏的、读超时）都
    当作「不知道」，让上层自己决定要不要继续，而不是把异常甩上去。
    """
    try:
        exe = ffprobe_path()
    except ValueError:
        return None
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, errors="replace", timeout=60)
    except Exception:
        return None
    try:
        return float((out.stdout or "").strip())
    except ValueError:
        return None


# 常见容器对应的音频编码器。ffmpeg 自己也会按扩展名猜，但显式点出来更稳。
_CODECS = {"mp3": "libmp3lame", "m4a": "aac", "aac": "aac",
           "wav": "pcm_s16le", "flac": "flac", "opus": "libopus"}


def _run(argv):
    """跑 ffmpeg 并等它结束。抽成单独一个函数，测试可以换成只记账、不真跑的桩。"""
    out = subprocess.run(argv, capture_output=True, text=True,
                         errors="replace", timeout=3600)
    if out.returncode != 0:
        lines = ((out.stderr or "") + (out.stdout or "")).strip().splitlines()
        raise ValueError("ffmpeg 没跑成：%s" % (lines[-1] if lines else "退出码 %d" % out.returncode))
    return out


def extract_audio(src, dst, fmt="mp3", bitrate="192k"):
    """从一个媒体文件里把音轨抽出来，返回产物路径。

    二进制路径只从 ffmpeg_path() 来：测试把它和 _run 一起换掉，就能在不真跑
    ffmpeg 的前提下检查拼出来的命令行。
    """
    exe = ffmpeg_path()
    codec = _CODECS.get((fmt or "").lower().lstrip("."), (fmt or "mp3").lower())
    argv = [exe, "-y", "-hide_banner", "-loglevel", "error",
            "-i", src, "-vn", "-c:a", codec, "-b:a", bitrate, dst]
    _run(argv)
    return dst


def _open(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    # 注意：进程内的 urllib 自己就读 HTTPS_PROXY / HTTP_PROXY 和系统代理，
    # 不用像 playwright 那样把代理手动翻成环境变量（见 platform_compat.proxy_env）。
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def _get_json(url):
    with _open(url) as resp:
        return json.loads(resp.read(4 * 1024 * 1024).decode("utf-8", "replace"))


def _download(url, dst, progress=None, stage=""):
    """把 URL 下到 dst。下到一半失败就把残file删掉，不留半个 zip 蒙人。

    边读边回报进度：界面「装组件」是个 30MB 级的等待，没有动静会像卡死。
    Content-Length 拿不到就只报阶段、不报百分比。
    """
    tmp = dst + ".part"
    try:
        with _open(url) as resp:
            try:
                total = int(resp.headers.get("Content-Length") or 0)
            except Exception:
                total = 0
            got = 0
            with open(tmp, "wb") as fh:
                while True:
                    block = resp.read(65536)
                    if not block:
                        break
                    fh.write(block)
                    got += len(block)
                    if progress and total:
                        progress(stage, int(100 * got / total))
        os.replace(tmp, dst)
        return dst
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _install_zip(blob, name):
    """把 zip 里那个 <name> 解到 tools 目录，赋上可执行位，返回它的路径。

    martin-riedl 的 zip 里就一个文件（ffmpeg / ffprobe），evermeet 的也是。
    这里按 basename 找而不是按固定条目名，是为了容一点目录层级上的差异。
    """
    target = os.path.join(tools_dir(), name)
    with zipfile.ZipFile(blob) as zf:
        member = None
        for info in zf.infolist():
            if os.path.basename(info.filename) == name:
                member = info
                break
        if member is None:
            raise ValueError("下回来的压缩包里没有 %s" % name)
        with zf.open(member) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
    os.chmod(target, 0o755)
    return target


def _src_martin_riedl(name):
    """首选源：静态 macOS arm64 构建，它首页写明的 redirect 端点总指向最新快照。"""
    return "https://ffmpeg.martin-riedl.de/redirect/latest/macos/arm64/snapshot/%s.zip" % name


def _src_evermeet(name):
    """兜底源：evermeet 的 JSON 索引能问到确切版本号与 zip 地址，问不到就退回固定重定向。

    它是 Intel(x86_64) 构建，Apple 芯片上要 Rosetta 才跑得动 —— 所以排在最后，
    只有首选源整个下不来时才用它。
    """
    try:
        info = _get_json("https://evermeet.cx/ffmpeg/info/%s/release" % name)
        url = ((info.get("download") or {}).get("zip") or {}).get("url")
        if url:
            return url
    except Exception:
        pass
    if name == "ffmpeg":
        return "https://evermeet.cx/ffmpeg/getrelease/zip"
    return "https://evermeet.cx/ffmpeg/getrelease/%s/zip" % name


SOURCES = (("martin-riedl", _src_martin_riedl), ("evermeet", _src_evermeet))


def _do_download(say, have):
    """按 BINARIES 逐个补齐缺的那些，每个都从首选源往下试。"""
    os.makedirs(tools_dir(), exist_ok=True)
    troubles = []
    for name in BINARIES:
        if have.get(name):
            continue
        blob = os.path.join(tools_dir(), "." + name + ".zip")
        last = None
        for label, resolver in SOURCES:
            try:
                url = resolver(name)
                say("正在取 %s" % name, None)
                _download(url, blob, progress=say, stage="正在取 %s" % name)
                path = _install_zip(blob, name)
                # 唯一的成功判据：真的能跑，而且自报家门。
                if ("%s version" % name) not in _version_text(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    raise ValueError("下回来的 %s 跑不起来（没有版本行）" % name)
                say("%s 就绪" % name, None)
                have[name] = path
                last = None
                break
            except Exception as e:
                last = e
                say("%s 这个源没成" % name, None)
            finally:
                if os.path.exists(blob):
                    try:
                        os.remove(blob)
                    except OSError:
                        pass
        if last is not None:
            troubles.append("%s：%s" % (name, str(last)[:120]))
    if troubles:
        raise ValueError("ffmpeg 没装成。%s。"
                         "挂上代理再试一次，或者自己装一个放进 PATH（brew install ffmpeg）。"
                         % "；".join(troubles))
    say("完成", 100)
    return have["ffmpeg"]


def ensure(progress=None):
    """确保 ffmpeg 与 ffprobe 各有一份能用的，返回 ffmpeg 的路径。

    已经装好就直接返回（第二次点是一秒过）；正在下的时候再点会直接报错，不让
    两份下载互相覆盖。progress(stage, pct) 是可选回调，给界面显示动静用。
    非 macOS 平台直接拒绝 —— 这个下载器只服务 macOS，别的系统请自己装。
    """
    global _downloading

    def say(stage, pct=None):
        if progress:
            try:
                progress(stage, pct)
            except Exception:
                pass

    if not pc.IS_MAC:
        raise ValueError("归藏只在 macOS 上自动下载 ffmpeg。"
                         "Windows / Linux 请自己装一个，并把它的目录加进 PATH。")
    if _downloading:
        raise ValueError("正在下一份 ffmpeg，等它下完再点。")

    have = {}
    for name in BINARIES:
        path, source = _resolve(name)
        if path and source == "bundled":
            have[name] = path
    if len(have) == len(BINARIES):
        say("完成", 100)
        return have["ffmpeg"]

    with _dl_lock:
        if _downloading:
            raise ValueError("正在下一份 ffmpeg，等它下完再点。")
        _downloading = True
    try:
        return _do_download(say, have)
    finally:
        _downloading = False


def remove():
    """清掉归藏自己下的那份（界面「清掉重下」用）。只删自己下的，系统里那份不碰。

    返回有没有真删掉东西：没下过就是 False，界面据此决定要不要提示「本来就没有」。
    """
    gone = False
    for name in BINARIES:
        path = os.path.join(tools_dir(), name)
        if os.path.isfile(path):
            try:
                os.remove(path)
                gone = True
            except OSError:
                pass
    return gone


if __name__ == "__main__":
    # 手动 / 界面拉起：ffmpeg_tool.py --ensure（下）| --status（看）
    # 只用退出码说话：下没下成，界面自己用 status() 复核（不信「装成功」四个字，
    # 只信二进制真的跑起来报了版本号 —— 见模块开头）。
    import sys

    if "--status" in sys.argv[1:]:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        sys.exit(0)

    def say(stage, pct, note=""):
        print("[%-8s %3s%%] %s" % (stage, "" if pct is None else pct, note), flush=True)

    try:
        path = ensure(say)
        print("ffmpeg 备好了：%s" % path, flush=True)
        sys.exit(0)
    except Exception as e:
        print("--- ffmpeg 没下成：%s: %s ---" % (type(e).__name__, e), flush=True)
        print("    可以稍后重试，或者自己装一份 ffmpeg 放进 PATH。", flush=True)
        sys.exit(1)
