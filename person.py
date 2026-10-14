"""个人主界面那三样东西：头像、名字、简介。

全都只落在这台机器上，不上传、不进云同步、不进 git：

    cache/avatar.bin      头像原图（二进制，跟壁纸同一个套路，不塞 localStorage）
    cache/config.json     profile_name / profile_bio / avatar_mime

头像为什么不进 localStorage：一张照片动辄几 MB，那边只有 5MB 上限，而且字符串化
还会翻倍 —— 壁纸已经踩过这个坑，这里照同一个决定办。

名字与简介在存之前先夹长度、去掉控制字符。界面是本地单页、不存在别人往这里写数据
的通道，但渲染走的是 innerHTML 拼接的几条老路径，**存的时候就把尖括号和换行控制
符处理干净**比指望每个调用点都记得转义要可靠得多（前端同样会转义，两层都要有）。
"""

import re

NAME_MAX = 40
BIO_MAX = 200
AVATAR_MAX = 8 * 1024 * 1024

# 半个代理码点（U+D800–U+DFFF 单独出现，不成对）：浏览器的 JS 字符串被拦腰截断时
# 会留下它，JSON 那边 `json.loads` 收得下（形如 "\ud83d"），于是它一路走到
# config.json 落盘那一步 —— 而那份文件是 ensure_ascii=False 写的，`json.dump`
# 碰到它就抛 UnicodeEncodeError，用户看到的是「改个名字没保存上」，cache/ 里还
# 留一个半截 .tmp。它是坏输入不是内容，洗掉比让整份资料存不下去强。
# 成对的代理码点不必管：json 解码时已经合成一个真字符，这里只会摘到落单的那个。
_SURROGATE = re.compile("[\ud800-\udfff]")

# 只认这四种魔数：声明的类型不可信，扩展名更不可信，看字节头
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF8", "image/gif"),
)

# 从别处粘过来的名字 / 简介里常混着「看不见的字符」：零宽空格、BOM、双向文本
# 控制符。它们不是空白，`ord(ch) >= 32` 拦不住，最后会显示成「名字看起来是空的」
# 或让整行字倒着排。这里只清这一类、以及 DEL / C1 这两个老控制区。
#
# 注意 U+200C（ZWNJ）/ U+200D（ZWJ）**不在此列**：它们是 emoji 与印度系文字的
# 组成笔画，删掉会把 🧑‍💻 拆成两半，属于「越洗越坏」。
_INVISIBLE = (
    "\u200b",                                      # 零宽空格
    "\u200e", "\u200f",                            # LRM / RLM
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",   # 双向嵌入 / 覆盖
    "\u2060",                                      # word joiner
    "\u2066", "\u2067", "\u2068", "\u2069",        # 双向隔离
    "\ufeff",                                      # BOM / 零宽不换行空格
)


def _is_control(ch):
    """C0（\\n 另算）、DEL、C1 都算控制字符 —— C1 那一段常被漏掉。"""
    o = ord(ch)
    return o < 32 or o == 127 or 128 <= o <= 159


def sniff_image(raw):
    """按魔数认图片类型，认不出来给 None（宁可拒收，不要存个打不开的文件）。"""
    if not raw:
        return None
    for head, mime in _MAGIC:
        if raw.startswith(head):
            return mime
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def clean_text(s, limit):
    """去控制字符（含零宽、双向控制符与落单的半个代理码点）+ 夹长度。"""
    s = str(s if s is not None else "")
    s = _SURROGATE.sub("", s)
    s = "".join(ch for ch in s
                if ch == "\n" or (not _is_control(ch) and ch not in _INVISIBLE))
    s = s.replace("\n", " ").strip()
    # 尖括号一律换掉：这一份内容会被好几处拼进 HTML
    s = s.replace("<", "〈").replace(">", "〉")
    return s[:limit]


def normalize(cfg):
    """把 config 里那几项整理成界面要的形状。

    存的时候已经洗过一道，这里**再洗一道**：config.json 是用户能直接改的文本文件，
    也可能留着旧版本写下的脏值，而这几项会被好几处拼进 HTML。清洗是幂等的，
    对正常值不做任何改动，所以这道兜底没有代价。
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    return {"name": clean_text(cfg.get("profile_name") or "", NAME_MAX),
            "bio": clean_text(cfg.get("profile_bio") or "", BIO_MAX)}


def set_profile(cfg, name, bio):
    """存名字与简介，返回 (改了什么, 新 cfg)。None = 这项保持原样，"" = 清空。

    界面上是两个独立输入框，但保存是一次 POST：不区分「没改」和「清空」的话，
    用户只想改简介就会把名字一起抹掉。
    """
    changed = []
    if name is not None:
        new = clean_text(name, NAME_MAX)
        if str(cfg.get("profile_name") or "") != new:
            cfg["profile_name"] = new
            changed.append("名字")
    if bio is not None:
        new = clean_text(bio, BIO_MAX)
        if str(cfg.get("profile_bio") or "") != new:
            cfg["profile_bio"] = new
            changed.append("简介")
    return changed, cfg
