#!/usr/bin/env python3
"""清理/卸载这一套的离线自测：**只许删列出来的那些，红线一律不动**。

这个模块删的是几百 MB 到 1.6GB，删错了没有回收站，所以测试的重点不是「删得掉」，
而是「删不掉不该删的」：
  · 书（GUIZANG_BOOKS）—— 用户的东西，一个字都不许动
  · 别人家的模型（HF 缓存里同一个目录下还有别的仓库）
  · 别人家的浏览器（共享缓存里的 firefox）
  · 数据目录本身 / 仓库根（源码直接跑时这俩是同一个目录，删了就是把仓库清了）
全程在临时沙盒里跑，不碰真实的 cache / 书库 / HF 缓存 / playwright 缓存。
"""
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-clean-"))
DATA = SB / "data"
BOOKS = SB / "books"
PW = SB / "ms-playwright"
HF = SB / "hfhub"

os.environ["GUIZANG_DATA"] = str(DATA)
os.environ["GUIZANG_BOOKS"] = str(BOOKS)
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(PW)
os.environ["HF_HUB_CACHE"] = str(HF)

import cleanup as cl  # noqa: E402
import platform_compat as pc  # noqa: E402

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


def seed():
    """铺一份「装过一次、用过一阵」的样子。"""
    for p in (DATA / "cache" / "browser_profile", DATA / "cache" / "tools",
              DATA / "cache" / "covers", DATA / "cache" / "video",
              DATA / ".venv", BOOKS, PW / "chromium-1228", PW / "firefox-1466",
              HF / "models--mlx-community--whisper-large-v3-turbo",
              HF / "models--Mapika--decider-2b"):     # 别人家的模型，不能被带走
        p.mkdir(parents=True, exist_ok=True)
    (DATA / "cache" / "browser_profile" / "Cookies").write_text("x")
    (DATA / "cache" / "tools" / "ffmpeg").write_text("x")
    (DATA / "cache" / "feed.json").write_text('{"subs": []}')
    (DATA / "cache" / "config.json").write_text("{}")
    (DATA / "runtime.json").write_text("{}")
    (DATA / "订阅.opml").write_text("<opml/>")
    (DATA / ".venv" / "pyvenv.cfg").write_text("home = /x")
    (BOOKS / "某本书.md").write_text("这是用户的书")
    (PW / "chromium-1228" / "chrome").write_text("x")
    (PW / "firefox-1466" / "ff").write_text("x")
    (HF / "models--mlx-community--whisper-large-v3-turbo" / "model.safetensors").write_text("x")
    (HF / "models--Mapika--decider-2b" / "m.bin").write_text("x")


def main():
    seed()

    # ── 1. 红线 ────────────────────────────────────────────────
    chk("红线：数据目录本身不许删", cl._safe_to_remove(str(DATA)) is False)
    chk("红线：仓库根不许删", cl._safe_to_remove(str(ROOT)) is False)
    chk("红线：书库不许删", cl._safe_to_remove(str(BOOKS)) is False)
    chk("红线：书库里的文件也不许删", cl._safe_to_remove(str(BOOKS / "某本书.md")) is False)
    chk("白名单内：cache 里那几样可以删", cl._safe_to_remove(str(DATA / "cache" / "tools")))

    # ── 2. 试算只看不删 ─────────────────────────────────────────
    before = cl._size(DATA / "cache")
    p = cl.plan("all")
    groups = {g["key"]: g for g in p["groups"]}
    chk("试算：两组都列出来了（组件 / 数据）", set(groups) == {"components", "data"}, sorted(groups))
    chk("试算：总数大于零", p["total"] > 0, p["total"])
    chk("试算：把书库路径如实报出来（界面要说清「这里不动」）",
        str(BOOKS) in p["books"], p["books"])
    chk("试算：ffmpeg 那份在组件组里",
        any(i["key"] == "ffmpeg" and i["exist"] for i in groups["components"]["items"]),
        [i["key"] for i in groups["components"]["items"]])
    chk("试算：转写模型也在组件组里",
        any(i["key"].startswith("model-") and i["exist"] for i in groups["components"]["items"]),
        [i["key"] for i in groups["components"]["items"]])
    chk("试算：Chromium 在、firefox 不在（共享缓存里只挑自己的）",
        any("chromium-1228" in i["key"] for i in groups["components"]["items"])
        and not any("firefox" in i["key"] for i in groups["components"]["items"]),
        [i["key"] for i in groups["components"]["items"]])
    chk("试算：订阅条目、登录态、运行时回执都在数据组里",
        {"data-cache-feed.json", "data-cache-browser_profile",
         "data-root-runtime.json"} <= {i["key"] for i in groups["data"]["items"]},
        [i["key"] for i in groups["data"]["items"]])
    chk("试算不动任何东西（cache 大小没变）", cl._size(DATA / "cache") == before)
    chk("试算：别家的模型不在清单里",
        not any("Mapika" in i["path"] for g in p["groups"] for i in g["items"]))

    # ── 3. 清数据：只清数据，组件与书都留着 ──────────────────────
    r = cl.run("data")
    chk("清数据：报了删掉几样、释放多少", r["ok"] and r["removed"] and r["freed"] > 0,
        {k: r[k] for k in ("ok", "freed", "errors")})
    chk("清数据：订阅条目真的没了", not (DATA / "cache" / "feed.json").exists())
    chk("清数据：登录态与浏览器档案没了", not (DATA / "cache" / "browser_profile").exists())
    chk("清数据：端口回执也没了", not (DATA / "runtime.json").exists())
    chk("清数据：书一个字没动", (BOOKS / "某本书.md").read_text() == "这是用户的书")
    chk("清数据：虚拟环境还留着（那是「卸载组件」的事）", (DATA / ".venv").is_dir())
    chk("清数据：ffmpeg 还留着", (DATA / "cache" / "tools" / "ffmpeg").exists())
    chk("清数据：转写模型还留着",
        (HF / "models--mlx-community--whisper-large-v3-turbo" / "model.safetensors").exists())
    chk("清数据：别家的模型没被误伤",
        (HF / "models--Mapika--decider-2b" / "m.bin").exists())
    chk("清数据：Chromium 没被误伤", (PW / "chromium-1228" / "chrome").exists())

    # ── 4. 卸组件：只卸组件，书还是不动 ──────────────────────────
    r2 = cl.run("components")
    chk("卸组件：报了删掉几样、释放多少", r2["ok"] and r2["removed"] and r2["freed"] > 0,
        {k: r2[k] for k in ("ok", "freed", "errors")})
    chk("卸组件：虚拟环境没了", not (DATA / ".venv").exists())
    chk("卸组件：ffmpeg 那份没了", not (DATA / "cache" / "tools").exists())
    chk("卸组件：转写模型没了",
        not (HF / "models--mlx-community--whisper-large-v3-turbo").exists())
    chk("卸组件：Chromium 没了", not (PW / "chromium-1228").exists())
    chk("卸组件：别家的模型还在（只删自己下的那几个仓库）",
        (HF / "models--Mapika--decider-2b" / "m.bin").exists())
    chk("卸组件：别家的浏览器还在（共享缓存不是我们的）",
        (PW / "firefox-1466" / "ff").exists())
    chk("卸组件：书依然一个字没动", (BOOKS / "某本书.md").read_text() == "这是用户的书")

    # ── 5. 再来一次：空手跑不许炸 ────────────────────────────────
    r3 = cl.run("all")
    chk("已经清干净了再点一次：不报错，只是没得删", r3["ok"] and r3["freed"] == 0, r3)

    chk("体积能说成人话", cl.human(0) == "0 B" and cl.human(1536) == "1.5 KB"
        and cl.human(3 * 1024 ** 3).endswith("GB"),
        (cl.human(0), cl.human(1536), cl.human(3 * 1024 ** 3)))

    # ── 6. 接线 ────────────────────────────────────────────────
    srv = (ROOT / "ui_server.py").read_text(encoding="utf-8")
    chk("后端：接了 cleanup 这个动作", '"cleanup"' in srv and "cleanup.plan" in srv
        and "cleanup.run" in srv)
    build = (ROOT / "shell" / "build_macos.sh").read_text(encoding="utf-8")
    chk("打包清单里有 cleanup.py", "\n  cleanup.py\n" in build)
    html = (ROOT / "ui.html").read_text(encoding="utf-8")
    chk("设置里有「卸载组件」与「清除本地数据」两个入口",
        "卸载组件" in html and "清除本地数据" in html and "cleanupAsk" in html)

    shutil.rmtree(SB, ignore_errors=True)
    bad = [c for c in checks if not c[0]]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} 通过")
    for _ok, name, extra in bad:
        print("  ✗ " + name + "  <- " + extra)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
