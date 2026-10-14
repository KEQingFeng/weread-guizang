#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏门禁：cache/config.json 的**内层字段**写脏，不许让整屏翻车。

一句话：`load_cfg()` 原先只保证顶层是对象，内层值什么样就什么样地交给调用方。
可配置是界面自己写、用户也会手工编辑、外部 Agent 也代写过，字段写脏完全可能：

    {"export_dir": 5}     → `.strip()` 抛 AttributeError
    {"nav_order": 5}      → `for x in 5` 抛 TypeError
    {"flomo_url": 5}      → `[-8:]` 抛 TypeError
    {"weread_key": 5}     → 同上；sync / agent_* / ai_* / asr_* / *_mime 同一类

这些读取点里最重的是 `/api/state` —— 界面每 2.6s 轮它一次，构建响应时就会摸
`export_dir()`、`weread_key()`、`flomo_url` 的尾巴、`nav_view()`。一个坏字段让
响应抛穿连接，用户看到的不是「某一格报错」，而是**整页失联**：书架、任务、设置
一起没了。别的账本（readstat / activity / readplan / library / feed / meta /
login / notes_index）都已收过形状，配置这里原先漏了。

这条把每个已知键逐一写脏、再全写脏一起，要求：`load_cfg()` 把形状不对的键当没设、
每个读取者都不抛、缺键回默认；同时**干净配置一个字不许改**（能不能读出值、列表
顺序、sidebar 模式、sync 是否开着）。全程只碰临时目录里的假 config.json，
不 import 服务进程、不起服务、不联网、不碰真实 cache/。
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))   # 被测模块在仓库根
import ui_server as us  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


ROOT = tempfile.mkdtemp(prefix="guizang-cfg-")
CFG = os.path.join(ROOT, "config.json")
OLD_CFG, OLD_AVATAR = us.CFG_PATH, us.AVATAR_PATH
us.CFG_PATH = CFG
us.AVATAR_PATH = os.path.join(ROOT, "avatar.bin")     # 不存在 = 没有头像


def put(obj):
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump(obj, f)


# 每个读配置的地方各挑一个代表：这些函数就是 /api/state 那条链上会走到的。
READERS = (
    ("export_dir()", lambda: us.export_dir()),
    ("weread_key()", lambda: us.weread_key()),
    ("nav_view()", lambda: us.nav_view()),
    ("profile_view()", lambda: us.profile_view()),
    ("agent_state()", lambda: us.agent_state()),
    ("ai_state()", lambda: us.ai_state()),
    ("sync_state()", lambda: us.sync_state()),
    ("avatar_file()", lambda: us.avatar_file()),
)

RAISED = object()


def val(f):
    try:
        return f()
    except Exception:      # noqa: BLE001 —— 门禁要出 FAIL，不要出 traceback
        return RAISED


try:
    # ── 0. 前提：键名表与代码里真正读的那些键对得上 ─────────────────
    # 手工列的表漏一个键，这条门禁就守不住那一个 —— 所以逐个键回读源码。
    src = pathlib.Path(HERE.parent / "ui_server.py").read_text(encoding="utf-8")
    missing = [k for k in (us.CFG_STR_KEYS + us.CFG_LIST_KEYS + us.CFG_DICT_KEYS)
               if ('"%s"' % k) not in src]
    chk("K0 收口表里的每个键都在 ui_server.py 里出现（不是凭空列的表）",
        not missing, missing)
    tops = []
    for v in ([1, 2], "x", None, 5):
        put(v)
        tops.append(us.load_cfg())
    chk("K1 顶层不是对象一律回空表（原先那条兜底还在）",
        all(t == {} for t in tops), tops)

    # ── 1. 逐个键写脏：load_cfg 当没设、向量里每个读取者都不抛 ───────
    leaks = []
    for keys, badv in ((us.CFG_STR_KEYS, 5), (us.CFG_LIST_KEYS, 5), (us.CFG_DICT_KEYS, 5)):
        for key in keys:
            put({key: badv})
            if key in us.load_cfg():
                leaks.append("load_cfg 没把脏键 %s 当没设" % key)
                continue
            for nm, f in READERS:
                if val(f) is RAISED:
                    leaks.append("%s=%r 时 %s 抛" % (key, badv, nm))
    chk("W1 每个键写脏后 load_cfg 都当没设、八个读取者都不抛", not leaks, leaks)

    # ── 2. 所有键一起写脏：/api/state 那条链上没有一个读取者抛 ────────
    allbad = {k: 5 for k in us.CFG_STR_KEYS + us.CFG_LIST_KEYS + us.CFG_DICT_KEYS}
    allbad["nav_order"] = 7
    allbad["nav_hidden"] = {"x": 1}
    put(allbad)
    dead = [nm for nm, f in READERS if val(f) is RAISED]
    chk("W2 所有配置键一起写脏，八个读取者仍全部回话",
        not dead, dead)
    nv = val(lambda: us.nav_view())
    chk("W3 脏 nav 退回默认十三格（不是空名单、也不是报错）",
        isinstance(nv, dict) and len(nv.get("items") or []) == 13
        and nv.get("sidebar") in us.SIDEBAR_MODES, nv)

    # ── 3. 反向：干净配置一个字不许改口径 ───────────────────────────
    clean = {"weread_key": "wrk-abcdefgh", "export_dir": "/tmp/out",
             "flomo_url": "https://flomoapp.com/api/v1/abc12345",
             "nav_order": ["feed", "shelf"], "nav_hidden": ["anna"],
             "sidebar_mode": "edge", "sync": {"enabled": True, "provider": "webdav"},
             "agent_url": "http://127.0.0.1:9/v1", "ai_model": "m-1"}
    put(clean)
    chk("R1 干净配置原样读回（没被收口误伤）",
        us.load_cfg() == clean, us.load_cfg())
    chk("R2 weread_key / export_dir 照读，key_tail 仍是末四位",
        us.weread_key() == "wrk-abcdefgh" and us.export_dir() == "/tmp/out"
        and us.weread_key()[-4:] == "efgh")
    nv2 = us.nav_view()
    chk("R3 nav_order / nav_hidden / sidebar_mode 照认",
        nv2["items"][0]["id"] == "feed" and nv2["sidebar"] == "edge"
        and [i["id"] for i in nv2["items"] if i["hidden"]] == ["anna"], nv2)
    chk("R4 sync 那块照读回来（enabled / provider 没丢）",
        us.sync_state().get("enabled") is True
        and us.sync_state().get("provider") == "webdav", us.sync_state())

    # ── 4. 缺键 = 默认，不许因为「不在表里」被当成脏值丢掉 ────────────
    put({"export_dir": "/tmp/keep"})
    chk("M1 只设一个键时，其余键缺省照旧（export_dir 留着）",
        us.export_dir() == "/tmp/keep" and us.weread_key() == ""
        and len(us.nav_view()["items"]) == 13)
finally:
    us.CFG_PATH, us.AVATAR_PATH = OLD_CFG, OLD_AVATAR
    shutil.rmtree(ROOT, ignore_errors=True)

print()
print("配置脏字段体检：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  config.json 内层写脏不再让 /api/state 整页失联")
