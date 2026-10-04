# -*- coding: utf-8 -*-
"""tests/ 里各套件共用的环境常量。

只干一件事：把「跑在哪个仓库、连哪个地址、读写哪个临时书库」这三件事从写死的字符串
变成可推导、可覆盖的东西。写死 `/Users/xxx/…` 这种路径既不能进仓库（那是别人的家目录），
也会让套件在换机器时静默失效。

优先级：命令行参数 > 环境变量 > 由本文件位置推导。
"""
import os
import pathlib
import sys

# 仓库根 = tests/ 的上一级。套件用父目录推导，就不需要任何绝对路径。
REPO = pathlib.Path(__file__).resolve().parent.parent

# 被测服务地址。run_all.sh 会起一个一次性沙盒并把地址写进来；
# 想挂到自己正在跑的那个实例上，export GUIZANG_TEST_URL=http://127.0.0.1:8770 即可 ——
# 这是唯一的口子，need_base() 什么都不给时会退出，不再拿这个缺省值当默认目标。
BASE = os.environ.get("GUIZANG_TEST_URL", "http://127.0.0.1:8770").rstrip("/")

# 沙盒书库与数据目录：默认落在系统临时目录下的 guizang-selftest，
# 与真实用户的 cache/ 和 ~/Documents/归藏 完全隔离。
SANDBOX = pathlib.Path(os.environ.get("GUIZANG_SELFTEST_DIR",
                                      pathlib.Path(__file__).resolve().anchor + "tmp/guizang-selftest"))
BOOKS = SANDBOX / "books"
CACHE = SANDBOX / "cache"

# 截图输出目录：诊断用的图，别和套件混在仓库里。
SHOTS = pathlib.Path(os.environ.get("GUIZANG_SHOT_DIR", str(SANDBOX / "shots")))


def use_real_repo():
    """少数套件（比如 privacy 扫描）默认扫仓库本身，这里给个统一的入口。"""
    return REPO


def need_base(argv_index=1):
    """让套件能吃命令行里的地址当第一参数，其余参数照旧。

    没给参数也没 export 环境变量时**直接退出**，不许悄悄退回 8770。理由不是洁癖：
    8770 是用户自己那台归藏正在听的端口，而界面从磁盘现读、路由表却是进程起来那一刻
    装进内存的 —— 手动跑一套件（少打一个参数是常事）就等于拿测试夹具去戳用户的真数据。
    2026-10-04 真栽过一次：没带地址跑 check_mcp_tools.py，它把端口上那个闲着的旧后端
    按本轮新加的「接管」逻辑停掉、换成了新进程，顺带往用户的 cache/flomo 写了一本空账。
    接管本身没错（那是设计好的），错在套件不该有「不打招呼就挂到用户实例上」这条路。
    """
    if len(sys.argv) > argv_index:
        return sys.argv[argv_index].rstrip("/")
    if os.environ.get("GUIZANG_TEST_URL"):
        return BASE
    sys.exit("真机套件得指着一个沙盒服务跑：要么把地址当第一个参数传进来"
             "（bash tests/run_all.sh 会起一个并传），要么自己 export GUIZANG_TEST_URL=…。"
             "不带地址不再缺省挂 8770 —— 那个端口上是用户正在用的归藏，不是测试沙盒。")


def export_sandbox_env():
    """给进程内 import ui_server 的套件用：必须在 import 之前调用。"""
    os.environ["GUIZANG_DATA"] = str(CACHE)
    os.environ["GUIZANG_BOOKS"] = str(BOOKS)
    return str(BOOKS)
