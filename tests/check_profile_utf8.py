# -*- coding: utf-8 -*-
"""个人资料里的坏字符（person.py）：半个代理码点不许把「保存」打成失败。

从浏览器来的名字 / 简介是 JSON 字符串：JS 把字符串拦腰截断时会留下一个落单的
代理码点（U+D800–U+DFFF），`json.loads` 收得下（形如 U+D83D 那个转义），于是它会
一路走到 config.json 落盘那一步 —— 而那份文件是 ensure_ascii=False 写的，
`json.dump` 碰到它就抛 UnicodeEncodeError：用户看到的是「改个名字没保存上」，
cache/ 里还多一个半截 .tmp。控制字符、零宽字符那条路 tests/check_profile.py 已经
钉住了，这一份专门补「代理码点」这一类 —— 它与那些不是一回事：`ord(ch) >= 32`
拦不住，控制器那套也拦不住。

只碰 person.py 的纯函数与一份临时文件，不 import ui_server、不起服务、不联网，
所以归静态段，--fast 也要带。三条硬要求：
  · 落单的代理码点被摘掉（高位、低位、以及「只剩它」）；
  · 真 emoji 与 ZWJ 连字一个字不动（洗过头就把 🧑‍💻 拆成两个人）；
  · 洗完的值真能按 config.json 那条路落盘，且手改脏的 config 读出来也干净。
"""
import json
import pathlib
import shutil
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import person  # noqa: E402

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


SANDBOX = tempfile.mkdtemp(prefix="guizang-profile-utf8-")


def writes_config_file(obj):
    """照 ui_server._write_json 那条路真写一份：ensure_ascii=False + utf-8。"""
    path = pathlib.Path(SANDBOX) / "config.json"
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        return True
    except UnicodeEncodeError:
        return False


HI, LO = "\ud83d", "\ude00"          # 落单的高位 / 低位代理码点


# ── 一、clean_text：摘掉落单的代理码点 ───────────────────────────
chk("落单的高位代理码点被摘掉", person.clean_text("a" + HI + "b", 40) == "ab")
chk("落单的低位代理码点被摘掉", person.clean_text("a" + LO + "b", 40) == "ab")
chk("只剩一个落单代理码点时给空串（不留写不进文件的坏字符）",
    person.clean_text(HI, 40) == "" and person.clean_text(LO, 40) == "")
chk("坏字符夹在正常内容中间：正常的都留下",
    person.clean_text("名" + HI + "字" + LO, 40) == "名字")
chk("摘除之后仍然按「字」夹长度", len(person.clean_text(HI + "归" * 100, 40)) == 40)
chk("清洗是幂等的（洗两遍与一遍相同）",
    person.clean_text(person.clean_text("x" + HI + "y", 40), 40)
    == person.clean_text("x" + HI + "y", 40))

# 反向保护：别洗过头。emoji 本身是一个真字符（不是代理对），ZWJ 是连字笔画。
chk("真 emoji（含肤色修饰符）一个字不动",
    person.clean_text("👋\U0001f3fd 归藏", 40) == "👋\U0001f3fd 归藏")
chk("ZWJ 连字（🧑‍💻）原样保留", person.clean_text("🧑\u200d💻", 40) == "🧑\u200d💻")


# ── 二、保存那条路：洗过的值必须真能落盘 ───────────────────────
cfg = {"profile_name": "旧", "profile_bio": "旧"}
changed, cfg = person.set_profile(cfg, "名" + HI + "字", "简介" + LO)
chk("set_profile 落盘的就是洗过的值",
    cfg["profile_name"] == "名字" and cfg["profile_bio"] == "简介"
    and changed == ["名字", "简介"], repr(cfg))
chk("洗过的 cfg 能按 config.json 的方式真写一份（ensure_ascii=False）",
    writes_config_file(cfg), repr(cfg))
# 对照：未清洗的落单代理码点确实过不了同一条落盘路 —— 证明这条门禁不是空转。
chk("对照：未清洗的落单代理码点确实写不进 utf-8",
    not writes_config_file({"profile_name": "名" + HI + "字"}))

# ── 三、读取兜底：手改脏的 config 也洗 ─────────────────────────
chk("normalize 也摘落单代理码点（config 被手改脏了也照洗）",
    person.normalize({"profile_name": "a" + HI + "b",
                      "profile_bio": LO + "x"}) == {"name": "ab", "bio": "x"})

shutil.rmtree(SANDBOX, ignore_errors=True)

bad = [c for c in checks if not c[0]]
print("\n个人资料坏字符体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  x " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
