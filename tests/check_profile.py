# -*- coding: utf-8 -*-
"""个人资料体检（person.py）：头像魔数、名字 / 简介清洗、以及几处「成对」的口径。

person.py 管的是个人主界面上那三样东西 —— 头像、名字、简介。它此前一条测试都没有，
而它守的正好是两件容易悄悄坏掉的事：

  ① 头像是「按魔数认类型」而不是信前端声明的 MIME，认不出来就拒收。sniff_image
     返回的那几个字符串会被 ui_server 拿去和 person_mime_ok() 对表，一旦两边
     说的不是同一套话，用户就会看到一张打不开的破图。
  ② 名字与简介在存的时候就洗干净：控制字符去掉、尖括号换掉、长度夹住。从别处
     粘过来的文本常混着零宽空格、BOM、双向文本控制符 —— 它们不是空白，
     `ord(ch) >= 32` 拦不住，界面上会表现成「名字像是空的」或整行字倒着排。
     反过来，ZWNJ / ZWJ 是 emoji 与印度系文字的组成笔画，**必须留着**，
     删掉会把 🧑‍💻 拆成两个人，属于越洗越坏。

第三件是「一处真相」：AVATAR_MAX 在 person.py 与 ui_server.py 各写了一份，
两份对不上时，界面提示的上限会与后端真正拦下的那个数不一致。这里把它们钉成对。

纯函数 + 源码走查，不起服务、不碰真实 cache、不联网，所以归静态段，--fast 也要带。
"""
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import person  # noqa: E402

SRV = (REPO / "ui_server.py").read_text(encoding="utf-8")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


# ── 一、头像魔数：认得出、也拒得掉 ─────────────────────────────
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 8
GIF = b"GIF89a" + b"\x00" * 8
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 4

chk("PNG 头认成 image/png", person.sniff_image(PNG) == "image/png")
chk("JPEG 头认成 image/jpeg", person.sniff_image(JPG) == "image/jpeg")
chk("GIF 头认成 image/gif", person.sniff_image(GIF) == "image/gif")
chk("WEBP 头（RIFF + WEBP）认成 image/webp", person.sniff_image(WEBP) == "image/webp")
# 只读头 16 个字节的调用点（ui_server.avatar_file）也得认得出 WEBP：WEBP 标记在第 12 字节
chk("截到 16 字节仍认得出 WEBP（avatar_file 只读头 16 字节）",
    person.sniff_image(WEBP[:16]) == "image/webp")
chk("RIFF 但不是 WEBP（WAV）不算图片",
    person.sniff_image(b"RIFF\x00\x00\x00\x00WAVE") is None)
chk("SVG（文本）不算图片", person.sniff_image(b"<svg xmlns=...>") is None)
chk("空串 / None 给 None 不抛异常",
    person.sniff_image(b"") is None and person.sniff_image(None) is None)
chk("声明里的 MIME 不能被骗：扩展名无关，只看字节",
    person.sniff_image(b"not an image at all") is None)
chk("认得出的类型全在 ui_server 的放行表里（两处说的同一套话）",
    set(person.sniff_image(x) for x in (PNG, JPG, GIF, WEBP))
    <= set(re.findall(r"\"(image/[a-z]+)\"", SRV.split("def person_mime_ok", 1)[1][:200])))
chk("save_avatar 真的走 person.sniff_image（不是自己另认一套）",
    "mime = person.sniff_image(raw)" in SRV)


# ── 二、名字 / 简介清洗 ────────────────────────────────────────
chk("普通中英文原样保留", person.clean_text("归藏 guizang", 40) == "归藏 guizang")
chk("C0 控制字符（除换行）去掉", person.clean_text("a\x00\x01\x1fb", 40) == "ab")
chk("换行折成空格再 strip", person.clean_text("  a\nb  ", 40) == "a b")
chk("DEL 与 C1 控制区也去掉（这一段最容易漏）",
    person.clean_text("a\x7fb\x85c\x9fd", 40) == "abcd")
chk("零宽空格去掉", person.clean_text("归\u200b藏", 40) == "归藏")
chk("BOM 去掉", person.clean_text("\ufeff名字", 40) == "名字")
chk("LRM / RLM 去掉", person.clean_text("a\u200eb\u200fc", 40) == "abc")
chk("双向嵌入 / 覆盖去掉（否则整行字会倒着排）",
    person.clean_text("abc\u202edef", 40) == "abcdef")
chk("双向隔离符去掉", person.clean_text("a\u2066b\u2069c", 40) == "abc")
chk("word joiner 去掉", person.clean_text("a\u2060b", 40) == "ab")
# 这两条是「别洗过头」的反向保护：emoji / 梵文靠它们连字
chk("ZWJ 保留：🧑‍💻 不许被拆成两个人", person.clean_text("🧑\u200d💻", 40) == "🧑\u200d💻")
chk("ZWNJ 保留", person.clean_text("a\u200cb", 40) == "a\u200cb")
chk("emoji 与肤色修饰符原样保留",
    person.clean_text("👋\U0001f3fd", 40) == "👋\U0001f3fd")
chk("尖括号一律换掉（这几项会被拼进 HTML）",
    person.clean_text("<b>hi</b>", 40) == "〈b〉hi〈/b〉")
chk("长度按「字」夹，不按字节", len(person.clean_text("归" * 100, 40)) == 40)
chk("空 / None / 非字符串都有个确定的串",
    person.clean_text(None, 40) == "" and person.clean_text("", 40) == ""
    and person.clean_text(123, 40) == "123")
chk("清洗是幂等的（第二遍不再改动）",
    person.clean_text(person.clean_text(" <x>\u200b ", 40), 40)
    == person.clean_text(" <x>\u200b ", 40))


# ── 三、normalize：脏 config 的兜底 ────────────────────────────
chk("坏形状（None / list / str）退成两个空串，不抛异常",
    person.normalize(None) == {"name": "", "bio": ""}
    and person.normalize(["x"]) == {"name": "", "bio": ""}
    and person.normalize("x") == {"name": "", "bio": ""})
chk("缺键 / 值为 None 都给空串",
    person.normalize({}) == {"name": "", "bio": ""}
    and person.normalize({"profile_name": None})["name"] == "")
chk("手工改脏的 config 也要洗干净（不能只信存的时候那道）",
    person.normalize({"profile_name": "  <x>\u200b ", "profile_bio": "a\x00b"})
    == {"name": "〈x〉", "bio": "ab"})
chk("normalize 也夹长度（旧版写下的超长值不会顶爆界面）",
    len(person.normalize({"profile_name": "归" * 500})["name"]) == person.NAME_MAX
    and len(person.normalize({"profile_bio": "b" * 5000})["bio"]) == person.BIO_MAX)


# ── 四、set_profile：None = 保持原样，"" = 清空 ─────────────────
cfg0 = {"profile_name": "老名字", "profile_bio": "老简介"}
changed, cfg = person.set_profile(dict(cfg0), None, None)
chk("两项都没传：没有改动，原值不动",
    changed == [] and cfg["profile_name"] == "老名字" and cfg["profile_bio"] == "老简介")

changed, cfg = person.set_profile(dict(cfg0), "新名字", None)
chk("只改名字：另一项不许被顺手抹掉",
    changed == ["名字"] and cfg["profile_name"] == "新名字" and cfg["profile_bio"] == "老简介")

changed, cfg = person.set_profile(dict(cfg0), "", None)
chk("空串是「清空」不是「保持原样」（与界面 save_profile 的口径一致）",
    changed == ["名字"] and cfg["profile_name"] == "")

changed, cfg = person.set_profile(dict(cfg0), "老名字", None)
chk("传了但值没变：不算改动（界面才不会白弹一次「已保存」）", changed == [])

changed, cfg = person.set_profile(dict(cfg0), " <b>\u200b ", " x\x00 ")
chk("比较的是洗过之后的值，落盘的也是洗过的值",
    changed == ["名字", "简介"]
    and cfg["profile_name"] == "〈b〉" and cfg["profile_bio"] == "x")

src = dict(cfg0)
changed, back = person.set_profile(src, "改一下", None)
chk("返回的就是传进去那份 cfg（调用方接着 save_cfg 落盘）", back is src)


# ── 五、一处真相：两处上限必须成对 ─────────────────────────────
chk("NAME_MAX / BIO_MAX 是正整数",
    isinstance(person.NAME_MAX, int) and person.NAME_MAX > 0
    and isinstance(person.BIO_MAX, int) and person.BIO_MAX > 0)
m = re.search(r"^AVATAR_MAX\s*=\s*([0-9*\s]+)$", SRV, re.M)
chk("ui_server 里找得到 AVATAR_MAX", bool(m), m)
if m:
    chk("ui_server 的 AVATAR_MAX 与 person.AVATAR_MAX 是同一个数（两份不许漂）",
        eval(m.group(1).strip()) == person.AVATAR_MAX,
        "%s vs %s" % (m.group(1).strip(), person.AVATAR_MAX))


bad = [c for c in checks if not c[0]]
print("\n个人资料体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  x " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
