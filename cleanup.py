#!/usr/bin/env python3
"""维护：把归藏装过的东西卸掉、把自己产生的数据清掉。

两件事分开，因为它们是两种意图：

  卸载组件   —— 为了让这软件跑起来而装的东西：虚拟环境（依赖在里面）、
                自己下的那份 ffmpeg、取书用的 Chromium。
  清除数据   —— 它自己攒下来的东西：登录态、书架清单、设置、阅读时长、
                笔记索引、订阅条目、剪藏、封面、下载中间件、临时音频，
                以及各本账写到一半留下的 `.tmp` / `.part` 临时件（半截
                `config.json` 里就带着接口 Key，半截 `readstat.json` 是
                同步取回断在改名之前留下的）。

软件本体（那个 .app）不归这里管 —— 用户自己拖进废纸篓。**书也一概不动**：
书在「文档/归藏」里，是用户的东西，不是这软件的缓存。

为什么每一步都要先 dry-run：这个模块删的是几百 MB 到 1.6GB 的东西，而且删错了
没有回收站。所以 plan() 只算不删、把「要删什么、多大、什么后果」摆出来，run()
才真动手；动每个路径之前再过一道 _safe_to_remove 的白名单与红线检查。

这个模块只用标准库（外加平台差异模块），导入时不碰网络。
"""
import os
import re
import shutil
import sys

import platform_compat as pc

REPO = os.path.dirname(os.path.abspath(__file__))

# 数据目录里、归藏自己产生的那一片（cache/ 与它下面的各样）。列成白名单而不是
# 「把 cache 整个删掉」：将来往 cache 里放别的东西时，会在这里被显式想一遍。
CACHE_ITEMS = (
    ("browser_profile", "取书用的浏览器档案（含微信读书登录态）"),
    ("anna_profile", "在安娜的档案里搜书下载时用的浏览器档案"),
    ("downloads", "取书 / 下载留下的中间文件（含安娜的档案接住的原文件）"),
    ("covers", "书架封面缓存"),
    ("apkg", "导出的 Anki 卡包"),
    ("video", "视频转笔记的临时音频与产物"),
    ("flomo", "导入进来的 flomo 便签（笔记账、图片、记忆画像）"),
    ("_bak-before-clean", "迁移时留下的备份"),
)
CACHE_FILES = (
    ("login_state.json", "登录态标记"),
    ("library.json", "书架清单"),
    ("config.json", "设置（含接口 Key 与导出位置）"),
    ("readstat.json", "阅读时长记录"),
    ("readplan.json", "每本书的阅读计划账（进度高水位、每日账、读完的档案）"),
    ("activity.json", "学习时长与写作时长（个人主界面那两张热力图）"),
    ("avatar.bin", "个人主界面的头像"),
    ("wallpaper.bin", "自定义壁纸"),
    ("notes_index.json", "划线与笔记索引"),
    ("note_templates.json", "自建的划线笔记模板"),
    ("feed.json", "订阅源与抓下来的条目"),
    ("clips.json", "剪藏下来的文章"),
    ("anna.json", "安娜的档案这一栏的状态账（窗口状态与入库记录，没有账号信息）"),
    ("anna_cmd.json", "界面递给下载窗口的搜书口令"),
    ("server.out", "上次运行的日志"),
)
ROOT_FILES = (
    ("runtime.json", "服务当前端口的回执"),
    ("feed.json", "订阅源与抓下来的条目"),
    ("订阅.opml", "导出的订阅清单"),
)


def _residue_re(name):
    """账本 name 写到一半留下的临时件名：`<name>.tmp` / `<name>.<pid>.<随机>.tmp` / `<name>.part`。

    前两种：固定 `<name>.tmp` 是 login 那一处至今还在写的固定名（`feed` /
    `platform_compat` 在第十七轮之前也还是这一档，改名之前留下的残件同样长得这样），
    `<name>.<pid>.<随机>.tmp` 是 `ui_server._write_json` / `anna_state` /
    `book_notes` 这一族现版的尺子
    （`"%s.%d.%s.tmp" % (path, os.getpid(), uuid.uuid4().hex[:8])`）。

    第三种 `<name>.part` 是**同步取回**那一处的落盘形状（`sync._write_atomic`
    先写 `path + ".part"` 再改名）：`sync._collect` 会把 `cache/` 里的
    `readstat.json` / `library.json` / `notes_index.json` 也列进同步对，同步中途被
    强杀（断网、断电、活动监视器里杀进程）就在 `cache/` 留下一份半截账，而这一刀
    原先只认 `.tmp` 两族、`drop_stray_tmps` 又只扫书目录 —— 两处都够不到它，用户点
    「清除本地数据」也带不走。书目录那一头（`ui_server.drop_stray_tmps`）早已是
    `.tmp` / `.part` 同一把尺子，这里对齐。

    名字从账本名派生、前后都锚死，所以认到的永远只是白名单里那些账本的兄弟文件。
    """
    return re.compile(r"^%s(?:\.tmp|\.part|\.\d+\.[0-9a-f]{8}\.tmp)$" % re.escape(name))


def _residue_in(directory, name):
    """directory 里属于账本 name 的 `.tmp` / `.part` 残件；只认普通文件。

    同名目录、符号链接（哪怕以 `.tmp` / `.part` 收尾）一概不碰 —— 与清残稿那一刀
    （`ui_server.drop_stray_tmps`）同一条护栏。目录不存在就当没有。
    """
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return []
    want = _residue_re(name)
    out = []
    for entry in entries:
        if not want.match(entry):
            continue
        path = os.path.join(directory, entry)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        out.append(path)
    return out


def _residue_items():
    """白名单账本留下的 `.tmp` / `.part` 残件，按同样那两张表逐名去找。

    为什么要单列这一刀：`run()` 是按**精确路径**删的，而这些残件的名字不定
    （带进程号与随机串、或同步那一处的 `.part` 尾巴）、数量不定，一个都认不到。
    而它们不是空的 —— `cache/config.json` 的半截件里就是用户的接口 Key，
    `library.json` 那份是他整个书架。用户点「清除本地数据」（换机、把机器送人
    之前最常点的那一下）本该把它们一起带走；少收这一刀，残件就留在盘上，而且
    谁都收不走：`drop_stray_tmps` 只扫书目录，够不到 `cache/` 与数据根。

    边界仍然按白名单来：只有上面那两张表里点过名的账本，才去收它的 `.tmp` /
    `.part` 兄弟；不认识的临时件一律不动（用户真往里放了别的东西，那是他的东西）。
    """
    out = []
    for name, label in CACHE_FILES:
        for path in _residue_in(cache_dir(), name):
            out.append(("data-cache-" + os.path.basename(path),
                        label + "（写到一半的临时件）", path))
    for name, label in ROOT_FILES:
        for path in _residue_in(data_dir(), name):
            out.append(("data-root-" + os.path.basename(path),
                        label + "（写到一半的临时件）", path))
    return out


def data_dir():
    return pc.data_dir(REPO)


def cache_dir():
    return os.path.join(data_dir(), "cache")


def _size(path):
    """这一样占多少字节。目录递归，文件直接取；不存在算 0。"""
    if not os.path.exists(path):
        return 0
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _safe_to_remove(path):
    """红线检查：这几样一律不动。

      · 数据目录本身 / 仓库根 —— 源码直接跑时它们就是同一个目录，手一抖就把仓库删了
      · 书库（默认「文档/归藏」，或 GUIZANG_BOOKS 指的地方）—— 那是用户的书
      · 空路径

    过了这道检查也不代表能删：每个路径还得先出现在上面的白名单里。
    """
    if not path:
        return False
    p = os.path.realpath(path)
    if p in (os.path.realpath(data_dir()), os.path.realpath(REPO)):
        return False
    books = os.path.realpath(pc.books_dir(REPO))
    if p == books or p.startswith(books + os.sep):
        return False
    return True


# ─────────────────────────── 卸载组件 ───────────────────────────

def component_items():
    """为了让这软件跑起来而装的东西。每一项都是 (key, 标签, 路径或动作)。"""
    items = [
        ("venv", "虚拟环境（依赖都在里面）", pc.venv_dir(REPO)),
        ("ffmpeg", "自己下的那份 ffmpeg / ffprobe", os.path.join(cache_dir(), "tools")),
    ]
    # 取书用的 Chromium：只挑归藏会用的那几个目录名，不动这个共享缓存里的别的东西
    try:
        base = pc.ms_playwright_dir()
        names = sorted(os.listdir(base)) if os.path.isdir(base) else []
        for name in names:
            if name.startswith("chromium-") or name.startswith("chromium_headless_shell-"):
                items.append(("chromium-" + name, "取书浏览器 %s" % name,
                              os.path.join(base, name)))
    except Exception:
        pass
    return items


def _data_items():
    out = []
    for name, label in CACHE_ITEMS:
        out.append(("data-cache-" + name, label, os.path.join(cache_dir(), name)))
    for name, label in CACHE_FILES:
        out.append(("data-cache-" + name, label, os.path.join(cache_dir(), name)))
    for name, label in ROOT_FILES:
        out.append(("data-root-" + name, label, os.path.join(data_dir(), name)))
    # 各本账写到一半留下的临时件也归「清除本地数据」管（见 _residue_items）。
    out.extend(_residue_items())
    return out


GROUP_ITEMS = {"components": component_items, "data": _data_items}


def plan(what="all"):
    """要删什么、多大 —— 只看不删。

    返回 {"groups": [{key, label, items: [{label, size, path, exist}], size}],
          "total": 字节, "books": 书库路径（只说一句「这里不动」）}
    """
    want = ("components", "data") if what in ("all", "", None) else (what,)
    groups = []
    for key in want:
        fn = GROUP_ITEMS.get(key)
        if not fn:
            continue
        items = []
        for ikey, label, path in fn():
            size = _size(path)
            items.append({"key": ikey, "label": label, "path": path,
                          "size": size, "exist": os.path.exists(path),
                          "safe": _safe_to_remove(path)})
        groups.append({"key": key, "label": GROUP_LABEL[key], "items": items,
                       "size": sum(i["size"] for i in items)})
    return {"groups": groups,
            "total": sum(g["size"] for g in groups),
            # 说清楚哪块地不碰。用户点「清除数据」时最怕的就是把书一起清了。
            "books": pc.books_dir(REPO),
            "data": data_dir()}


GROUP_LABEL = {"components": "组件（装上去才能用的那些）",
               "data": "本地数据（它自己攒下来的）"}


def run(what="all"):
    """真删。返回每一步的结果，好让界面如实说「删了什么、还剩什么、哪里没删成」。"""
    p = plan(what)
    removed, kept, errors = [], [], []
    freed = 0
    for group in p["groups"]:
        for it in group["items"]:
            if not it["exist"]:
                continue
            path = it["path"]
            if not it["safe"]:
                kept.append(it["label"])
                continue
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
                freed += it["size"]
                removed.append({"label": it["label"], "size": it["size"]})
            except Exception as e:
                errors.append("%s：%s: %s" % (it["label"], type(e).__name__, e))
    return {"ok": not errors, "freed": freed, "removed": removed,
            "kept": kept, "errors": errors}


def human(n):
    """给人看的体积。"""
    try:
        n = float(n)
    except Exception:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024
    return "%.1f GB" % n


if __name__ == "__main__":
    # 手动用：python cleanup.py [--what components|data|all] [--run]
    import json
    args = sys.argv[1:]
    what = "all"
    if "--what" in args:
        what = args[args.index("--what") + 1]
    if "--run" in args:
        print(json.dumps(run(what), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(plan(what), ensure_ascii=False, indent=2))
