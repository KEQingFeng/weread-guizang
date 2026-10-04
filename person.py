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

NAME_MAX = 40
BIO_MAX = 200
AVATAR_MAX = 8 * 1024 * 1024

# 只认这四种魔数：声明的类型不可信，扩展名更不可信，看字节头
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF8", "image/gif"),
)


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
    """去控制字符 + 夹长度。"""
    s = str(s if s is not None else "")
    s = "".join(ch for ch in s if ch == "\n" or ord(ch) >= 32)
    s = s.replace("\n", " ").strip()
    # 尖括号一律换掉：这一份内容会被好几处拼进 HTML
    s = s.replace("<", "〈").replace(">", "〉")
    return s[:limit]


def normalize(cfg):
    """把 config 里那几项整理成界面要的形状。"""
    cfg = cfg if isinstance(cfg, dict) else {}
    return {"name": str(cfg.get("profile_name") or ""),
            "bio": str(cfg.get("profile_bio") or "")}


def set_profile(cfg, name, bio):
    """存名字与简介，返回 (改了什么, 新 cfg)。空串表示「这项保持原样」。

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
