# -*- coding: utf-8 -*-
"""书目录 meta.json 的脏形状收口：合法 JSON 但不是对象时，六条路不再各自炸。

meta.json 是「一本书的身份」：书名、作者、来源、来路都在里头。它会被人手工编辑、
被云同步落半截、被旧版本写成别的形状 —— ``[]`` / ``"书名"`` / ``123`` / ``null``
都是合法 JSON，``json.load`` 照收。旧代码在这些地方只 ``try`` 了「读不开」，读出来
直接 ``.get(...)`` / ``.update(...)``：

  · 合并稿（merged_markdown）/ 打包（build_zip）/ 导出（export_book）/ 目录
    （book_outline）/ 改书名（write_meta）/ 清残稿（reset_book_output）各要一处
    AttributeError：用户看到的是「导出没反应 / 书打不开 / 保存失败」；
  · 书架清单（list_books）与 ``book_meta()`` 早就是「不是对象就回空表」，可这把尺子
    只在那两处，别的路各抄各的。

现在六条路统一走 ``ui_server._meta_dict()``，而它只复用书库那边已有的
``book_layout.read_meta()`` —— 同一条规矩只有一份实现。这一条把「脏形状当没 meta、
好数据一个字不改、六条路一个都不抛」连同一条「旧读法确实会抛」的对照一起钉住。

跑法：纯函数 + 系统临时目录沙盒，不联网、不开浏览器、不起服务，归 --fast 静态段。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = tempfile.mkdtemp(prefix="guizang-meta-shape-")
os.environ["GUIZANG_DATA"] = os.path.join(ROOT, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(ROOT, "books")

import ui_server                                              # noqa: E402

FAIL = []
PASSED = 0

# 合法的 JSON、但不是对象的四种形状。前三种最阴：`"书名"` 迭代出来是一串单字，
# 早期有的读法会把它当成「一个字段」，安安静静地写坏。
BAD_SHAPES = (("数组", []), ("字符串", "书名"), ("数字", 123), ("null", None))


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def call(fn, *a, **k):
    """把入口调一次：抛了就把异常当结果交出去，别让整条套件倒在第一发上。"""
    try:
        return fn(*a, **k), None
    except Exception as e:                       # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)


def write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as f:
        if isinstance(obj, str):
            f.write(obj)          # 原样落一段字节：专造「合法 JSON、形状不对」/ 半截
        else:
            json.dump(obj, f, ensure_ascii=False)


def seed(book_id, meta):
    """造一本「像一本书」的目录，meta 可以是任意 JSON 形状 / 原样字节。返回目录路径。"""
    d = os.path.join(ui_server.OUT_DIR, book_id)
    os.makedirs(os.path.join(d, "chapters"), exist_ok=True)
    with io.open(os.path.join(d, "chapters", "0001.md"), "w", encoding="utf-8") as f:
        f.write("# 第一章\n\n正文。\n")
    write(os.path.join(d, "meta.json"), meta)
    return d


def meta_on_disk(book_id):
    d = os.path.join(ui_server.OUT_DIR, book_id)
    with io.open(os.path.join(d, "meta.json"), encoding="utf-8") as f:
        return json.load(f)


# ── A. 那把尺子本身 ────────────────────────────────────────────────
os.makedirs(ui_server.OUT_DIR, exist_ok=True)
a = seed("a_good", {"title": "好书", "source": "clip", "url": "https://x"})
chk("A1 好形状原样读出（好数据一个字不改）",
    ui_server._meta_dict(a) == {"title": "好书", "source": "clip", "url": "https://x"})

for label, raw in BAD_SHAPES:
    write(os.path.join(a, "meta.json"), raw)
    got, err = call(ui_server._meta_dict, a)
    chk("A2 脏形状「%s」→ 空表，且 .get 不抛" % label,
        err is None and got == {} and got.get("title") is None, (got, err))

write(os.path.join(a, "meta.json"), "{半截")
chk("A3 半截 JSON → 空表（老行为不许回退）", ui_server._meta_dict(a) == {})
os.remove(os.path.join(a, "meta.json"))
chk("A4 文件不在 → 空表", ui_server._meta_dict(a) == {})
chk("A5 目录本身是 None → 空表，不抛", ui_server._meta_dict(None) == {})

# ── B. 目录 book_outline ───────────────────────────────────────────
seed("b_dirty", [])
out, err = call(ui_server.book_outline, "b_dirty")
chk("B1 脏数组：目录读得出、书号当标题、作者为空",
    err is None and out.get("title") == "b_dirty" and out.get("author") == ""
    and len(out.get("chapters") or []) == 1, (out, err))

seed("b_str", "书名")
out, err = call(ui_server.book_outline, "b_str")
chk("B2 脏字符串：退回书号，不许把书名拆成一个个单字",
    err is None and out.get("title") == "b_str", (out, err))

seed("b_good", {"title": "正常的书", "author": "作者"})
out, err = call(ui_server.book_outline, "b_good")
chk("B3 好 meta：书名 / 作者照原样带出来",
    err is None and out.get("title") == "正常的书" and out.get("author") == "作者", (out, err))

# ── C. 合并稿 merged_markdown ──────────────────────────────────────
seed("c_dirty", [])
md, err = call(ui_server.merged_markdown, "c_dirty")
chk("C1 脏数组：合并稿照出，标题退回书号",
    err is None and (md or "").startswith("# c_dirty\n"), (md, err))

seed("c_good", {"title": "我的书名"})
md, err = call(ui_server.merged_markdown, "c_good")
chk("C2 好 meta：合并稿用真书名开头",
    err is None and (md or "").startswith("# 我的书名\n"), (md, err))

# ── D. 打包 build_zip ──────────────────────────────────────────────
seed("d_dirty", [])
dest, err = call(ui_server.build_zip, "d_dirty")
ok_zip = False
if err is None and dest and os.path.isfile(dest):
    with zipfile.ZipFile(dest) as z:
        ok_zip = any(n.endswith(".md") for n in z.namelist())
chk("D1 脏数组：zip 打得出来、里面有合并稿", err is None and ok_zip, (dest, err))

seed("d_good", {"title": "我的/书:名"})
dest, err = call(ui_server.build_zip, "d_good")
names = []
if err is None and dest and os.path.isfile(dest):
    with zipfile.ZipFile(dest) as z:
        names = [n for n in z.namelist() if n.endswith(".md")]
chk("D2 好 meta：zip 里的文件名用真书名，路径符号已换成下划线",
    err is None and names == ["我的_书_名.md"], (names, err))

# ── E. 改书名 write_meta ───────────────────────────────────────────
for label, raw in (("数组", []), ("字符串", "书名")):
    bid = "e_%s" % ("arr" if label == "数组" else "str")
    seed(bid, raw)
    _, err = call(ui_server.write_meta, bid, "改后的名", "改后的作者", True)
    disk, derr = call(meta_on_disk, bid)
    chk("E %s：改书名不抛，落盘的是对象、新值写进去了" % label,
        err is None and derr is None and isinstance(disk, dict)
        and disk.get("title") == "改后的名" and disk.get("author") == "改后的作者"
        and disk.get("done") is True, (err, disk))

seed("e_good", {"title": "旧名", "source": "clip", "url": "https://mp.weixin.qq.com/s/x"})
_, err = call(ui_server.write_meta, "e_good", "新名", "", True)
disk, derr = call(meta_on_disk, "e_good")
chk("E3 好 meta：没碰的字段（来源 / 原文链接）一个都不丢",
    err is None and derr is None and disk.get("title") == "新名"
    and disk.get("source") == "clip"
    and disk.get("url") == "https://mp.weixin.qq.com/s/x", (err, disk))

# ── F. 清残稿 reset_book_output ────────────────────────────────────
seed("f_dirty", [])
res, err = call(ui_server.reset_book_output, "f_dirty")
disk, derr = call(meta_on_disk, "f_dirty")
chk("F1 脏数组：清残稿不抛，身份重建成一个对象（done 置回 False、标题退回书号）",
    err is None and derr is None and isinstance(res, dict) and res.get("ok") is True
    and res.get("title") == "f_dirty" and isinstance(disk, dict)
    and disk.get("done") is False, (res, err, disk))

seed("f_good", {"title": "要留住的书名", "author": "作者甲", "url": "https://x"})
res, err = call(ui_server.reset_book_output, "f_good")
disk, derr = call(meta_on_disk, "f_good")
chk("F2 好 meta：清残稿只把 done 置回 False，书名 / 作者 / 来路都不动",
    err is None and derr is None and isinstance(disk, dict)
    and disk.get("title") == "要留住的书名" and disk.get("author") == "作者甲"
    and disk.get("url") == "https://x" and disk.get("done") is False, (res, err, disk))

# ── G. 只取标题 meta_title ─────────────────────────────────────────
seed("g_dirty", 123)
chk("G1 脏数字 → 回书号", ui_server.meta_title("g_dirty") == "g_dirty")
seed("g_good", {"title": "只要标题"})
chk("G2 好 meta → 回真标题", ui_server.meta_title("g_good") == "只要标题")

# ── H. 书架清单不许被脏 meta 连累 ─────────────────────────────────
seed("h_dirty", [])
books, err = call(ui_server.list_books)
row = next((b for b in (books or []) if b.get("id") == "h_dirty"), {})
chk("H1 书架清单整体不抛，脏 meta 的那本仍在、标题退回书号",
    err is None and row.get("title") == "h_dirty", (err, len(books or [])))

# ── I. 对照：旧读法确实会抛（证明上面喂的是真危险形状）────────────
ctrl = os.path.join(ui_server.OUT_DIR, "i_ctrl")
write(os.path.join(ctrl, "meta.json"), [])
old_get = None
try:
    json.load(io.open(os.path.join(ctrl, "meta.json"), encoding="utf-8")).get("title")
    old_get = "没抛"
except AttributeError:
    old_get = "抛了"
chk("I1 对照：旧读法 json.load(...).get() 在数组形状上确实抛 AttributeError",
    old_get == "抛了", old_get)

old_update = None
try:
    cur = json.load(io.open(os.path.join(ctrl, "meta.json"), encoding="utf-8"))
    cur.update({"title": "x"})
    old_update = "没抛"
except AttributeError:
    old_update = "抛了"
chk("I2 对照：旧写法 json.load(...).update() 在数组形状上确实抛 AttributeError",
    old_update == "抛了", old_update)

shutil.rmtree(ROOT, ignore_errors=True)
print()
print("书目录 meta 脏形状兜底：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  meta.json 被写成非对象时，六条路不再各自 AttributeError")
