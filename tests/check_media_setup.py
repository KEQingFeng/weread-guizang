#!/usr/bin/env python3
"""转写组件（引擎 + 模型）的准备逻辑与接线，离线自测。

这条线最容易出的错是「装的时候按 A 下、跑的时候按 B 找」，以及「没下完就报就绪」。
所以这里钉三件事：
  1. 引擎挑谁、模型缓存在哪个目录（短名要翻成 Systran/faster-whisper-xxx 那种）
  2. 直连 Hugging Face 失败时会不会自动换镜像；用户自己设过端点就别乱动
  3. 接线：bootstrap 里那两步不拦门、打包清单里有 media_setup.py、壳里开了自动准备

全程不联网、不下任何东西 —— 下载那一步用桩替掉。
"""
import os
import pathlib
import shutil
import sys
import tempfile
import time
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-media-"))
# 缓存目录指进沙盒：绝不去动用户真实的 ~/.cache/huggingface
os.environ["HF_HUB_CACHE"] = str(SB / "hub")
os.environ.pop("HF_ENDPOINT", None)
os.environ.pop("GUIZANG_ASR_ENGINE", None)
os.environ.pop("GUIZANG_AUTO_MEDIA", None)

import media_setup as ms  # noqa: E402

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


def main():
    # ── 1. 挑引擎 / 模型路径 ────────────────────────────────────
    want = "mlx" if ms.is_apple_silicon() else "faster"
    chk("挑引擎：Apple 芯片用 mlx，其余用 faster", ms.engine() == want, ms.engine())
    os.environ["GUIZANG_ASR_ENGINE"] = "faster"
    chk("挑引擎：设置里点过名就听点名的", ms.engine() == "faster", ms.engine())
    os.environ.pop("GUIZANG_ASR_ENGINE", None)

    chk("模型短名翻成正确的缓存仓库名（faster 那套）",
        ms.repo_id("faster") == "Systran/faster-whisper-small", ms.repo_id("faster"))
    chk("模型 HF 仓库名原样保留（mlx 那套）",
        ms.repo_id("mlx") == "mlx-community/whisper-large-v3-turbo", ms.repo_id("mlx"))
    chk("缓存目录落在 HF_HUB_CACHE 下（没往用户家目录写）",
        str(SB) in ms.hf_hub_dir(), ms.hf_hub_dir())
    chk("模型目录名按 Hugging Face 的约定拼",
        ms.model_dir("faster").endswith("models--Systran--faster-whisper-small"),
        ms.model_dir("faster"))

    chk("没下过就是没就绪", ms.model_ready("faster") is False)
    # 空壳（下了一半）不许算就绪 —— 这正是「假装就绪」的坑
    shell = pathlib.Path(ms.model_dir("faster")) / "snapshots" / "abc"
    shell.mkdir(parents=True, exist_ok=True)
    chk("只有个空壳目录不算就绪", ms.model_ready("faster") is False)
    (shell / "model.bin").write_text("x")
    chk("真有权重文件才算就绪", ms.model_ready("faster") is True)
    shutil.rmtree(ms.model_dir("faster"))     # 清干净，后面还要用这个目录

    # ── 2. 直连失败 → 自动换镜像 ────────────────────────────────
    calls = []
    fake = types.ModuleType("huggingface_hub")

    def snapshot_download(repo_id=None, **kw):
        calls.append(kw.get("endpoint"))
        if len(calls) == 1:
            raise RuntimeError("max retries exceeded with url")
        d = ms.model_dir("faster")      # 桩按被测的那个引擎落权重
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "model.safetensors"), "w") as f:
            f.write("x")
        return d

    fake.snapshot_download = snapshot_download
    real_mod = sys.modules.get("huggingface_hub")
    sys.modules["huggingface_hub"] = fake
    try:
        os.environ.pop("HF_ENDPOINT", None)
        ok, msg = ms.download_model("faster")
        chk("直连失败会自己换镜像再试一遍", ok and len(calls) == 2, (ok, msg, calls))
        chk("换镜像走的是 HF_ENDPOINT（就是那个国内镜像）",
            os.environ.get("HF_ENDPOINT") == ms.MIRROR, os.environ.get("HF_ENDPOINT"))
        chk("下完确实报了就绪，且缓存里有权重", ms.model_ready("faster") and "就绪" in msg, msg)

        # 用户自己设过端点：不许被我们改写
        shutil.rmtree(ms.model_dir("faster"), ignore_errors=True)
        calls.clear()
        os.environ["HF_ENDPOINT"] = "https://hf.example.cn"

        def boom(repo_id=None, **kw):
            calls.append(kw.get("endpoint"))
            raise RuntimeError("boom")

        fake.snapshot_download = boom
        ok, msg = ms.download_model("faster")
        chk("用户自己配过端点就不动它，也不偷偷换镜像",
            os.environ.get("HF_ENDPOINT") == "https://hf.example.cn" and len(calls) == 1,
            (os.environ.get("HF_ENDPOINT"), calls, msg))
    finally:
        sys.modules.pop("huggingface_hub", None)
        if real_mod is not None:
            sys.modules["huggingface_hub"] = real_mod
        os.environ.pop("HF_ENDPOINT", None)
        shutil.rmtree(ms.model_dir("faster"), ignore_errors=True)

    # ── 3. 不联网时的自动准备：默认不干 ──────────────────────────
    os.environ.pop("GUIZANG_AUTO_MEDIA", None)
    chk("源码运行时不开自动准备（不会因为起个服务就拉 GB 级权重）",
        ms.auto_start_enabled() is False)
    chk("开关关着时补/不补都不动手", ms.maybe_auto_start()[0] is False, ms.maybe_auto_start())
    os.environ["GUIZANG_AUTO_MEDIA"] = "1"
    chk("壳里开了开关才算数", ms.auto_start_enabled() is True)

    # 引擎已经在了（本机装了）→ 只剩模型；把下模型换成桩，线程跑一遍看状态机
    real_dl = ms.download_model
    ms.download_model = lambda **kw: (True, "stub")
    try:
        ok, _ = ms.start("model")
        chk("能给后台线程派活", ok is True)
        for _ in range(60):
            if not ms.status()["busy"]:
                break
            time.sleep(0.05)
        st = ms.status()
        chk("干完把 busy 收掉、记上「都准备好了」",
            st["busy"] == "" and st["err"] == "" and st["pct"] == 100, st)
    finally:
        ms.download_model = real_dl
        os.environ.pop("GUIZANG_AUTO_MEDIA", None)

    chk("status 的形状够前端画那盏灯",
        all(k in ms.status() for k in ("engine", "engine_ready", "model", "model_ready",
                                       "busy", "pct", "got", "total", "note", "err", "hub")),
        sorted(ms.status()))

    # ── 4. 接线：不拦门、进包、壳里开自动 ────────────────────────
    bs = (ROOT / "bootstrap.py").read_text(encoding="utf-8")
    chk("bootstrap：多了 ffmpeg 与转写引擎两步",
        "[4/5]" in bs and "[5/5]" in bs and "media_setup" in bs)
    # 这两步必须在 Chromium 那道 return 1 之后，且自己不许 return 1 —— 否则一次
    # 组件下载失败就把人堵在配置页上，连取书都进不去。
    tail = bs.split("── 4. ffmpeg")[-1]
    chk("bootstrap：视频组件这两步不拦门（段里没有 return 1）",
        "return 1" not in tail and "return 0" in tail)

    build = (ROOT / "shell" / "build_macos.sh").read_text(encoding="utf-8")
    chk("打包清单里有 media_setup.py（运行期模块必须进包）",
        "\n  media_setup.py\n" in build)

    swift = (ROOT / "shell" / "main.swift").read_text(encoding="utf-8")
    chk("壳里给后端开了自动准备", 'env["GUIZANG_AUTO_MEDIA"] = "1"' in swift)
    chk("首启页多了一行「视频转写组件」", "视频转写组件" in
        (ROOT / "shell" / "onboarding.html").read_text(encoding="utf-8"))
    chk("壳里会问一句引擎装没装", "mediaEngineReady" in swift)

    # ── 5. 后端把那盏灯的原料给出来了 ────────────────────────────
    srv = (ROOT / "ui_server.py").read_text(encoding="utf-8")
    chk("后端：available 里带上 setup", 'v["setup"] = media_setup.status()' in srv)
    chk("后端：有 media_engine / media_model / media_all 三个动作",
        all(('"%s"' % k) in srv for k in ("media_status", "media_engine", "media_model", "media_all")))
    chk("后端：服务起来时会问一次要不要自动补", "maybe_auto_start" in srv)

    html = (ROOT / "ui.html").read_text(encoding="utf-8")
    chk("前端：视频页有「转写模型」这盏灯与补件按钮",
        "转写模型" in html and "vidPrep" in html and "#vModelFix" in html)

    shutil.rmtree(SB, ignore_errors=True)
    bad = [c for c in checks if not c[0]]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} 通过")
    for _ok, name, extra in bad:
        print("  ✗ " + name + "  <- " + extra)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
