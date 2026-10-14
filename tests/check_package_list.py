# -*- coding: utf-8 -*-
"""进包清单不许漏模块：根目录每个 .py 都必须在 APP_FILES 里。

为什么这条值得单独一个离线门禁
    打包脚本（mac .app / Windows zip / 源码 zip）共用 `shell/build_macos.sh` 里的
    `APP_FILES` 这一处清单，三个打包器里的反向护栏也都齐了 —— 但它们全都要等到
    **打包那一刻**才跑。而「漏一个运行期模块」的症状正是本仓库唯一没法在开发机上
    复现的那条路径：源码直接跑一切正常，装成 app 一开就哑。book_ids.py 就是这么漏的：
    它在 714132a 抽出来给 ui_server / shelf_add 用，清单没跟着加；此后任何一次打包
    都会在护栏上停下，可 --fast 一路绿，谁也不知道。

所以这里做两件事
    1. 直接调打包器读清单的那把尺子（tools/package_source_zip.manifest），把它的
       SystemExit 变成一条会失败的断言 —— 护栏没删、清单没漏，这里就绿；
    2. 再自己数一遍「根目录的 .py vs 清单」，并在断言里点名漏了谁，方便一眼看出。

纯文件走查，不起服务、不联网、不打包，归 --fast。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import package_source_zip                                         # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:300]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ── A. 打包器那把尺子自己说没问题（漏文件 / 清单里的文件不存在都会 exit）──
try:
    app_files, app_dirs = package_source_zip.manifest()
    broke = None
except SystemExit as e:                                            # pragma: no cover
    app_files, app_dirs, broke = [], [], str(e)
chk("打包器读得动清单，且没在护栏上停下（漏模块 / 列了不存在的文件都算）",
    broke is None, broke)

# ── B. 自己再数一遍，失败时点名 ─────────────────────────────
root_py = sorted(p.name for p in package_source_zip.ROOT.glob("*.py"))
drift = [n for n in root_py if n not in set(app_files)]
chk("根目录每个 .py 都进了 APP_FILES（漏掉就是「装起来一开就哑」）",
    not drift, drift)
chk("清单里没有重复项",
    len(app_files) == len(set(app_files)),
    sorted({f for f in app_files if app_files.count(f) > 1}))
chk("清单条目确实都在仓库里",
    all(os.path.exists(os.path.join(ROOT, f)) for f in app_files),
    [f for f in app_files if not os.path.exists(os.path.join(ROOT, f))])
chk("APP_DIRS 都在（mcp / skills / vendor 少一个，装起来一样哑）",
    all(os.path.isdir(os.path.join(ROOT, d)) for d in app_dirs), app_dirs)

# book_ids.py 是这条门禁抓到的第一例，单独钉一条：它被删掉时这条会直接说出名字。
chk("book_ids.py 在清单里（714132a 抽出来时漏过的那一个）",
    "book_ids.py" in app_files, app_files)

# 解析别退化成空表：空表在上面的 drift 里会全红，但先挡一道，错误信息更直白。
chk("清单解析没退化成空表（>25 项且含入口模块）",
    len(app_files) > 25 and "ui_server.py" in app_files, (len(app_files), app_files[:5]))

# ── C. 护栏本身还在，且清单仍然只有一处真相 ─────────────────
for rel in ("shell/build_macos.sh", "tools/package_source_zip.py"):
    chk("%s 里那条「没进 APP_FILES」的反向护栏还在" % rel,
        "没进 APP_FILES" in read(rel), rel)

defs = []
for rel in ("shell/build_macos.sh", "shell/build_windows.sh",
            "tools/package_windows.py", "tools/package_source_zip.py"):
    if re.search(r"^\s*APP_FILES\s*=", read(rel), re.M):
        defs.append(rel)
chk("APP_FILES 只有一处定义（shell/build_macos.sh）",
    defs == ["shell/build_macos.sh"], defs)

print()
if FAIL:
    print("进包清单：失败 %d 项" % len(FAIL))
    for n in FAIL:
        print("  ✗ " + n)
    sys.exit(1)
print("进包清单：通过 %d 项，失败 0 项" % PASSED)
