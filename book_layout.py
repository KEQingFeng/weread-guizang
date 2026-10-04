"""书库的目录结构：一本书该住在哪个文件夹里。

1.0.0 之前，取回的书、自己导入的书、剪藏的文章、订阅收进来的条目、视频转出来的
笔记，全平铺在书库根下 —— 目录名是书号（`36332dc0813abb933g016d22`、`clip_xxx`），
几百本摊在一起，人在访达里根本看不出谁是什么。这一轮界面上把四个模块拆开了，
磁盘上跟着拆开：一类一个文件夹，一眼能认。

    书库/
      微信读书/<书号>/…      取回来的整本
      本地书架/imp_<名>/…    自己导入的 MD / EPUB / PDF
      剪藏/clip_<名>/…       公众号、知乎、小红书、X 的文章
      订阅/feed_<名>/…       RSS 条目收进来的那一篇
      视频/video_<名>/…      视频转出来的笔记
      便签/flomo_<名>/…      flomo 导进来、又收成一条一篇的那笔记

只有「这一层目录是模块文件夹」这件事是新的，**书号本身不变**：界面、接口、MCP
用的还是那个 id，所以旧链接、旧笔记、旧导图全都照常打得开。位置由 `resolve()`
现找：先按 id 猜它该在哪个模块，再退回平铺的旧位置 —— 于是没搬动的老书、命令行
跑出来的平铺新书，都不会因为这次改结构而「看不见」。

搬旧书走 `migrate()`，只在启动时跑一次，规矩是**绝不覆盖**：目标位已经有同名目录
就跳过并出声，让人自己决定。删除从来不在这里发生。
"""

import os
import re

# 模块 → 书库里的子目录名（界面上的分区与磁盘上的文件夹一一对应）
MODULES = [
    ("weread", "微信读书"),
    ("local", "本地书架"),
    ("clip", "剪藏"),
    ("feed", "订阅"),
    ("video", "视频"),
    ("flomo", "便签"),
]
DIR_OF = dict(MODULES)
MODULE_OF = {v: k for k, v in MODULES}
ORDER = [m for m, _ in MODULES]

# 目录名前缀：meta 坏了、还没有 meta 时，靠前缀认这本书属于哪一路
PREFIX_OF = {"clip_": "clip", "feed_": "feed", "video_": "video",
             "imp_": "local", "flomo_": "flomo"}

# 书号只允许这些字符（与 ui_server.safe_book_dir 同一道闸，两边都把一次不算多）
ID_RE = re.compile(r"[A-Za-z0-9_\-]+\Z")

# 什么才算「一本书的目录」：有三样里的一样就算，缺 meta 的半成品也认，
# 免得迁移时把取到一半的书留在根上，下次又当陌生目录搬一遍。
MARKERS = ("meta.json", "chapters", "_catalog.json")


def ok_id(book_id):
    return bool(book_id) and bool(ID_RE.match(str(book_id)))


def module_of(book_id, meta=None):
    """这本书属于哪个模块。

    先信 meta（`source` 是写进书里的实话，`format` 是老书的别名），
    再看目录名前缀，最后才算它是取回来的书 —— 猜错不会坏事：resolve() 会把
    所有可能的位置都找一遍，界面只是少一个分组，文件一个都不会丢。
    """
    meta = meta or {}
    for key in ("source", "format"):
        v = str(meta.get(key) or "")
        if v in DIR_OF and v != "weread":
            return v
    # 导入的书写 source=local，但 format 是文件本来的格式（md/epub/pdf/txt），
    # 那种情况下前缀 imp_ 才是准的，所以前缀要在 format 之后再看一次。
    for pre, mod in PREFIX_OF.items():
        if str(book_id or "").startswith(pre):
            return mod
    src = str(meta.get("source") or "")
    if src == "local":
        return "local"
    if src == "weread":
        return "weread"
    return "weread"


def is_module_dir(name):
    return str(name or "") in MODULE_OF


def module_path(books_root, module):
    """<书库>/<模块文件夹>，不建目录。"""
    return os.path.join(books_root, DIR_OF.get(module or "weread", DIR_OF["weread"]))


def book_dir(books_root, module):
    """模块目录（要写进去之前先建出来）。"""
    d = module_path(books_root, module)
    os.makedirs(d, exist_ok=True)
    return d


def candidates(books_root, book_id, module=None):
    """这本书可能出现的位置，按「最可能是哪儿」排序。"""
    root = os.path.abspath(books_root)
    mods = [module] if module in DIR_OF else list(ORDER)
    for m in mods:
        yield os.path.join(root, DIR_OF[m], book_id)
    yield os.path.join(root, book_id)      # 平铺的旧位置（与没搬动的老书）


def resolve(books_root, book_id):
    """找到这本书真正在的目录，返回绝对路径；找不到给 None。

    只认「是个目录」且「像一本书」的，避免把模块文件夹本身当成一本书。
    """
    if not ok_id(book_id):
        return None
    for path in candidates(books_root, book_id):
        real = os.path.realpath(path)
        if not real.startswith(os.path.realpath(books_root) + os.sep):
            continue                       # 路径穿越：无论怎么拼都不许越出书库
        if os.path.isdir(real) and is_book_dir(real):
            return real
    return None


def locate(books_root, book_id):
    """resolve 的另一种问法：一起给出它属于哪个模块（在找到之前只能靠猜）。"""
    d = resolve(books_root, book_id)
    if not d:
        return None, module_of(book_id)
    name = os.path.basename(os.path.dirname(d))
    return d, MODULE_OF.get(name) or module_of(book_id, read_meta(d))


def is_book_dir(path):
    return any(os.path.exists(os.path.join(path, m)) for m in MARKERS)


def read_meta(book_dir):
    """读一本书的 meta.json；坏了、没有都给空 dict（调用方各有一套兜底）。"""
    import json
    try:
        with open(os.path.join(book_dir, "meta.json"), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def walk(books_root):
    """把书库里所有的书走一遍，给 (书号, 目录, 模块)。

    先收模块文件夹里的那一层（1.0.1 之后的正解），再收还平铺在书库根上的旧目录；
    根上那些平铺的 `*.md` 合并稿不是目录，自然不收。

    同一个书号在两处都出现时（迁移撞名、跳过没搬）**只列模块目录里的那一份** ——
    界面上出现两张一模一样的卡比少一张更难解释。
    """
    out = []
    if not os.path.isdir(books_root):
        return out
    seen = set()
    for mod in ORDER:
        top = module_path(books_root, mod)
        if not os.path.isdir(top):
            continue
        for sub in sorted(os.listdir(top)):
            d = os.path.join(top, sub)
            if os.path.isdir(d) and is_book_dir(d):
                seen.add(sub)
                out.append((sub, d, mod))
    for name in sorted(os.listdir(books_root)):
        top = os.path.join(books_root, name)
        if name.startswith(".") or not os.path.isdir(top):
            continue
        if is_module_dir(name):
            continue
        if name not in seen and is_book_dir(top):
            out.append((name, top, module_of(name, read_meta(top))))
    return out


def migrate(books_root, log=None):
    """把还平铺在书库根上的书搬进各自模块文件夹。只在启动时跑一次。

    三条硬规矩：
      · **目标位已经有东西就跳过**，绝不覆盖、绝不合并 —— 同名书号同时出现在
        两处时，谁对谁错看不出来，宁可出声交给人，不要替人决定。
      · 只搬「像一本书」的目录；模块文件夹、散文件一律不动。
      · 全程不删。搬不动就留在原地，`resolve()` 照样找得到它，界面不会少一本书。
    """
    say = log or (lambda s: None)
    if not os.path.isdir(books_root):
        return []
    moved, skipped = [], []
    for name in sorted(os.listdir(books_root)):
        src = os.path.join(books_root, name)
        if name.startswith(".") or not os.path.isdir(src):
            continue
        if is_module_dir(name):
            continue
        if not is_book_dir(src):
            continue
        module = module_of(name, read_meta(src))
        dest = os.path.join(books_root, DIR_OF[module], name)
        if os.path.exists(dest):
            skipped.append(name)
            continue
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil_move(src, dest)
            moved.append((name, module))
        except OSError as e:
            skipped.append(name)
            say("书库整理没搬动 %s：%s" % (name, str(e)[:80]))
    if skipped:
        say("有 %d 本没搬（目标位已有同名目录或权限不足），它们留在原处照常可读。"
            % len(skipped))
    return moved


def shutil_move(src, dest):
    """搬目录：跨卷时 os.rename 会失败，退回 copy + rmtree 之前先想清楚 ——
    这里宁可失败出声，也不做「复制完再删源」这种半途崩了就变成两份的操作。"""
    import os as _os
    _os.rename(src, dest)
