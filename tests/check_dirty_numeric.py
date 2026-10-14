#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏门禁：账本里的**越界数字**不许从数字收口缝里穿出去。

一句话：`json` 认 `Infinity` / `-Infinity` / `NaN`，也认 `1e400` 与一长串数字
（前者解析成 inf，后者解析成一个 Python 大整数）；这些值是**合法的 JSON**，会
原样落进本机账本，再被读回来。而各处的数字收口一直只写

    except (TypeError, ValueError)

漏了 `OverflowError` —— 于是：

  · `flomo/notes.json` 里一条 `"ts": Infinity` 让 `_clean_memo` 抛穿 `load()`，
    便签那一屏从「一条坏」变成「一条都看不到」；
  · `/api/notes_csv` 收到越界 `at`（前端原样把账里的时间戳传回来）让整份导出
    写不出来，还留下一个空文件；
  · `notes.json` 的 `"created_at": 1e400` 让 `/api/mynotes` 存盘回「存不下」；
  · `anna.json` 的计数越界让 `read_state()` 整份退回空账，界面上看就是记录全没了；
  · 视频书 `meta.json` 的越界时长/页数让 `video_books()` 整栏 500；
  · 取书续传锚点档、转写时间轴、外站 JSON 的时间戳，同一处漏掉同一个异常。

这条把同一组越界值喂进每一个收口函数与每一条产品级读路径，要求：不抛、坏值回
安全默认、**能认的值一点不许改口径**、坏了一条不许带走整份。全程在临时目录里跑，
不起服务、不联网、不碰真实 cache/ 与书库。
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))   # 被测模块在仓库根
import anna_state as ans  # noqa: E402
import book_notes as bn  # noqa: E402
import export_precise as ep  # noqa: E402
import flomo_notes as fn  # noqa: E402
import ui_server as us  # noqa: E402
import video_note as vn  # noqa: E402
import web_parse as wp  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


ROOT = tempfile.mkdtemp(prefix="guizang-numeric-")
try:
    # ── 0. 前提：这些越界值真的是 json 解析得出来的 ────────────────
    # 先把「为什么会有这种值」钉住；解析不出来就说明这组用例站不住脚。
    chk("J1 Infinity 是合法 JSON，解析成 inf",
        json.loads('{"v": Infinity}')["v"] == float("inf"))
    chk("J2 -Infinity / NaN 同理",
        json.loads('{"v": -Infinity}')["v"] == float("-inf")
        and json.loads('{"v": NaN}')["v"] != json.loads('{"v": NaN}')["v"])
    chk("J3 1e400 解析成 inf", json.loads("1e400") == float("inf"))
    big = json.loads("1" + "0" * 400)
    chk("J4 一长串数字解析成大整数（转 float 时抛 OverflowError）",
        isinstance(big, int) and big > 10 ** 300)

    OVER = [float("inf"), float("-inf"), float("nan"), 10 ** 400,
            "1e400", "inf", "nan"]

    # ── 1. 每个收口函数：越界值不抛，回安全默认 ────────────────────
    RAISED = object()          # 收口函数真抛了就拿它当结果，于是断言出 FAIL 而不是 traceback

    def val(f):
        try:
            return f()
        except Exception:      # noqa: BLE001 —— 门禁就是要逮住任何异常
            return RAISED

    OVER_FNS = (
        ("flomo._as_int", lambda v: fn._as_int(v)),
        ("book_notes._int", lambda v: bn._int(v)),
        ("anna_state._as_int", lambda v: ans._as_int(v)),
        ("web_parse._fmt_epoch", lambda v: wp._fmt_epoch(v)),
        ("video_note._sec", lambda v: vn._sec(v)),
        ("video_note._maybe_sec", lambda v: vn._maybe_sec(v)),
        ("video_note._as_int", lambda v: vn._as_int(v)),
        ("ui_server._vnum", lambda v: us._vnum(v)),
        ("flomo.csv_time", lambda v: fn.csv_time(v)),
    )
    leaks = ["%s(%r) 抛" % (nm, v)
             for v in OVER for nm, f in OVER_FNS if val(lambda f=f, v=v: f(v)) is RAISED]
    chk("N1 九个收口函数对 inf / -inf / nan / 10**400 / 字符串越界值都不抛",
        not leaks, leaks)

    inf = float("inf")
    chk("N2 越界值回的是安全默认（0 / None / 空串），不是 inf 或天文数字",
        val(lambda: fn._as_int(inf)) == 0 and val(lambda: bn._int(inf)) is None
        and val(lambda: ans._as_int(inf)) == 0 and val(lambda: vn._sec(inf)) == 0.0
        and val(lambda: vn._maybe_sec(inf)) == 0.0
        and val(lambda: vn._as_int(inf)) is None
        and val(lambda: us._vnum(inf)) == 0 and val(lambda: fn.csv_time(inf)) == ""
        and val(lambda: wp._fmt_epoch(inf)) == "",
        (val(lambda: fn._as_int(inf)), val(lambda: bn._int(inf)),
         val(lambda: ans._as_int(inf)), val(lambda: vn._sec(inf)),
         val(lambda: vn._maybe_sec(inf)), val(lambda: us._vnum(inf))))

    # 超大整数是**认得出的整数**：int() 装得下就该照认，只有转 float 的那几处才回默认。
    chk("N3 超大整数的口径分得清：整数收口照认，转 float 的收口回默认",
        val(lambda: us._vnum(10 ** 400)) == 0 and val(lambda: fn.csv_time(10 ** 400)) == ""
        and val(lambda: wp._fmt_epoch(10 ** 400)) == ""
        and val(lambda: bn._int(10 ** 400)) == 10 ** 400)

    # ── 2. 反向：能认的值口径一点不许改 ─────────────────────────────
    chk("R1 csv_time 正常时间照排（本机时区）",
        val(lambda: fn.csv_time(1756864800)) == "2025-09-03 10:00:00",
        val(lambda: fn.csv_time(1756864800)))
    chk("R2 数字串照认：_as_int(\"42\") / _int(\" 1756864800 \") / _vnum(\"12\")",
        val(lambda: fn._as_int("42")) == 42
        and val(lambda: bn._int(" 1756864800 ")) == 1756864800
        and val(lambda: us._vnum("12")) == 12)
    chk("R3 小数照旧：_sec(90.5)=90.5 / _vnum(3.9)=3",
        val(lambda: vn._sec(90.5)) == 90.5 and val(lambda: us._vnum(3.9)) == 3)
    chk("R4 本来就不认的照旧不认：_as_int(\"x\")=0 / csv_time(\"x\")=\"\"",
        val(lambda: fn._as_int("x")) == 0 and val(lambda: fn.csv_time("x")) == "")

    # ── 3. 便签账本：一条 Infinity 不许把整份 load 带走 ──────────────
    FD = os.path.join(ROOT, "flomo")
    os.makedirs(FD, exist_ok=True)
    with open(fn.path_in(FD), "w", encoding="utf-8") as f:
        f.write('{"memos":[{"id":"m1","md":"好的一条","ts":1756864800},'
                '{"id":"m2","md":"坏时间的一条","ts":Infinity}],'
                '"at":Infinity,"src":"t"}')
    try:
        loaded = fn.load(FD)
        ok = True
    except Exception as e:              # noqa: BLE001
        loaded, ok = {}, False
        print("      load 抛了：" + type(e).__name__)
    chk("F1 一条 ts 是 Infinity，load 不抛、两条条目都还在",
        ok and len(loaded.get("memos") or []) == 2, loaded)
    chk("F2 坏的那条 ts 归 0，好的那条时间照认",
        ok and [m["ts"] for m in loaded["memos"]] == [1756864800, 0],
        [m.get("ts") for m in (loaded.get("memos") or [])])
    chk("F3 顶层 at 是 Infinity 也归 0，src 照留",
        ok and loaded.get("at") == 0 and loaded.get("src") == "t",
        (loaded.get("at"), loaded.get("src")))

    # ── 4. 划线笔记导出 CSV：坏一条只让那一列留空 ───────────────────
    rows = [("好的一条", 1756864800), ("越界的一条", float("inf")), ("没时间", 0)]
    raw = val(lambda: fn.build_import_csv(rows))
    back = []
    if isinstance(raw, (bytes, bytearray)):
        back = [r for r in raw.decode("utf-8-sig").split("\r\n") if r]
    chk("C1 三条一条不少（表头 + 3 行），坏时间不带走整份",
        len(back) == 4, back)
    chk("C2 好的那行时间写对，越界 / 空的那两行留空",
        len(back) == 4 and "2025-09-03 10:00:00" in back[1]
        and back[2].endswith(",") and back[3].endswith(","), back)

    # ── 5. 安娜账本：计数越界不许把整本账吞成空 ──────────────────────
    AD = os.path.join(ROOT, "anna")
    os.makedirs(os.path.join(AD, "cache"), exist_ok=True)
    with open(os.path.join(AD, ans.STATE_FILE), "w", encoding="utf-8") as f:
        f.write('{"domain":"annas-archive.org","caught":Infinity,'
                '"imported":3,"seen":["a","b"]}')
    st = ans.read_state(AD)
    chk("A1 计数是 Infinity 时 domain 照在（不再整份退回空账）",
        st.get("domain") == "annas-archive.org", st)
    chk("A2 越界计数归 0，能认的 counted 照认、seen 不丢",
        st.get("caught") == 0 and st.get("imported") == 3 and st.get("seen") == ["a", "b"], st)

    # ── 6. 书本地笔记：越界时间戳不许让存盘回「存不下」 ──────────────
    BD = os.path.join(ROOT, "book")
    os.makedirs(BD, exist_ok=True)
    doc = {"created_at": float("nan"),
           "marks": [{"id": "m1", "text": "划线", "created_at": float("inf"),
                      "updated_at": 1e400}],
           "entries": [{"id": "n1", "title": "条", "body": "正文",
                        "created_at": 10 ** 400, "updated_at": float("-inf")}]}
    try:
        bn.save_notes(BD, doc)
        saved = bn.load_notes(BD)
        bok = True
    except Exception as e:              # noqa: BLE001
        saved, bok = {}, False
        print("      save_notes 抛了：" + type(e).__name__)
    chk("B1 越界时间戳存得下、读得回", bok and bn.notes_path(BD) and saved)
    times = []
    if bok:
        times = [saved["marks"][0]["created_at"], saved["marks"][0]["updated_at"],
                 saved["entries"][0]["created_at"], saved["entries"][0]["updated_at"],
                 saved["created_at"]]
    chk("B2 落盘的时间戳全是能用的整数（不是 inf / nan）",
        bok and len(times) == 5 and all(isinstance(t, int) for t in times), times)

    # ── 7. 视频书架：meta 里越界数字不许让整栏 500 ───────────────────
    BOOKS = os.path.join(ROOT, "books")
    vdir = os.path.join(BOOKS, "视频", "video_dirty01")
    os.makedirs(vdir, exist_ok=True)
    with open(os.path.join(vdir, "meta.json"), "w", encoding="utf-8") as f:
        f.write('{"source":"video","title":"坏数字的视频","duration":Infinity,'
                '"words":1e400,"chapters":10,"segments":NaN,"page":Infinity,'
                '"pages":1000000000000000000000000000000,"updated_at":1e400}')
    old_out = us.OUT_DIR
    try:
        us.OUT_DIR = BOOKS
        listed = us.video_books()
        vok = True
    except Exception as e:              # noqa: BLE001
        listed, vok = [], False
        print("      video_books 抛了：" + type(e).__name__)
    finally:
        us.OUT_DIR = old_out
    chk("V1 meta 里全是越界数字，video_books() 照样列得出这本书",
        vok and len(listed) == 1, listed)
    one = listed[0] if listed else {}
    chk("V2 越界项归 0，能认的 chapters 照认、页数是能用的整数",
        one.get("duration") == 0 and one.get("words") == 0
        and one.get("segments") == 0 and one.get("page") == 1
        and one.get("chapters") == 10
        and isinstance(one.get("pages"), int) and one["pages"] > 0, one)

    # ── 8. 取书续传锚点档：坏一条只跳那一条 ─────────────────────────
    SP = os.path.join(ROOT, "stuck.json")
    with open(SP, "w", encoding="utf-8") as f:
        f.write('{"catalog":12,"strikes":{"0":2,"1":Infinity,"2":"x","3":1}}')
    got = val(lambda: ep.load_stuck(SP, 12))
    chk("S1 strikes 里越界 / 认不出的只跳那一条，其余照认",
        got == {0: 2, 3: 1}, got)
    chk("S2 目录对不上时整份作废（越界值也不许让这步抛）",
        val(lambda: ep.load_stuck(SP, 99)) == {}, val(lambda: ep.load_stuck(SP, 99)))

    # ── 9. 外站 JSON 的时间戳：越界只丢时间，不丢整篇 ────────────────
    chk("W1 外站给 400 位整数当时间戳，_fmt_epoch 回空串而不是抛",
        val(lambda: wp._fmt_epoch(10 ** 400)) == "",
        val(lambda: wp._fmt_epoch(10 ** 400)))
    chk("W2 正常秒 / 毫秒照排",
        str(val(lambda: wp._fmt_epoch(1756864800))).startswith("2025-")
        and str(val(lambda: wp._fmt_epoch(1756864800000))).startswith("2025-"),
        (val(lambda: wp._fmt_epoch(1756864800)),
         val(lambda: wp._fmt_epoch(1756864800000))))
finally:
    shutil.rmtree(ROOT, ignore_errors=True)

print()
print("越界数字体检：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  Infinity / 1e400 / 超长整数不再从数字收口缝里穿出去")
