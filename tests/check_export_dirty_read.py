# -*- coding: utf-8 -*-
"""取书引擎自己的三个脏读口：_progress.json / meta.json / raw 页记录。

为什么单独一份：界面那边（ui_server.list_books / _meta_dict）早就把「账本读得开
不等于形状对」收过口了，可**引擎这侧**（export_precise.main）还留着同一批旧读法，
而且踩的位置更靠前：

  _progress.json  → write_progress() 里 data = json.load(...) 在 try 内，
                    紧跟的 data.update(fields) 在 try 外。`[]` / `"书名"` / `123`
                    / `null` 都是合法 JSON，一句 AttributeError 就在取书**开头**
                    （running=True）或**收尾**（running=False）把整趟任务带走。
  meta.json       → main 开头 / 收尾各 `meta = json.load(...)` 再直接 meta.get / meta.update。
                    脏形状同样是 AttributeError：开头「书名读不出来」，收尾更冤 ——
                    书其实已经取完了，只是 done 写不进去，界面永远显示没取完。
  raw/*.json      → 续传开头清点 seen_imgs：`json.load(...).get("images", [])` + `img["url"]`。
                    raw/ 是逐页扒下来的外部数据，整份是列表、images 混进非对象项都有可能，
                    一条脏记录就让这本书再也取不动，和 collect_tasks「只跳不牵连」相反。

口径与仓库其它账本一致：**认不出的当没有，能认的一个字不改**。meta 一律过
book_layout.read_meta（与界面 / 书库同一把尺子，不另抄一份）；进度不是对象就当从头记；
图片只收「是对象且 url 是非空字符串」的项。纯离线、临时目录，不起浏览器。
"""
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import export_precise as E  # noqa: E402

FAIL = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if not cond:
        FAIL.append(name)


TMP = tempfile.mkdtemp(prefix="guizang-export-dirty-")

DIRTY_SHAPES = [
    ("列表", []),
    ("字符串", "书名"),
    ("数字", 123),
    ("null", None),
    ("布尔", True),
    ("嵌套列表", [[]]),
]


def put(path, obj, raw=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as f:
        if raw is not None:
            f.write(raw)
        else:
            json.dump(obj, f, ensure_ascii=False)
    return path


def fresh(name):
    d = os.path.join(TMP, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return d


# ── A. write_progress：脏 _progress.json 不许把取书带走 ────────────────
for label, shape in DIRTY_SHAPES:
    d = fresh("prog-" + label)
    p = put(os.path.join(d, "_progress.json"), shape)
    try:
        E.write_progress(d, pages=7, running=True)
        got = json.load(io.open(p, encoding="utf-8"))
        chk("A 进度账本 %s：不抛，且当「没有进度」重记（pages/running 落盘）" % label,
            isinstance(got, dict) and got.get("pages") == 7 and got.get("running") is True
            and isinstance(got.get("updated_at"), (int, float)), got)
    except Exception as e:  # noqa: BLE001
        chk("A 进度账本 %s：不抛" % label, False, "%s: %s" % (type(e).__name__, e))

# 读不开的半截档：老路径，也要照旧不抛
d = fresh("prog-half")
p = put(os.path.join(d, "_progress.json"), None, raw='{"pages": ')
try:
    E.write_progress(d, pages=2)
    chk("A 半截 JSON：不抛，当没有进度重写", json.load(io.open(p, encoding="utf-8")).get("pages") == 2)
except Exception as e:  # noqa: BLE001
    chk("A 半截 JSON：不抛", False, "%s: %s" % (type(e).__name__, e))

# 好账本：旧字段一个不丢，新字段合并进去，updated_at 由这次写覆盖
d = fresh("prog-good")
p = put(os.path.join(d, "_progress.json"), {"pages": 5, "keep": "yes", "updated_at": 1})
E.write_progress(d, pages=9)
got = json.load(io.open(p, encoding="utf-8"))
chk("A 好账本：已翻页数被这次覆盖、旧字段 keep 保留、updated_at 刷新",
    got.get("pages") == 9 and got.get("keep") == "yes" and got.get("updated_at") != 1, got)

# 对照：旧读法在这几种形状上确实抛 —— 证明这道闸不是空转
for label, shape in DIRTY_SHAPES:
    d = fresh("prog-old-" + label)
    p = put(os.path.join(d, "_progress.json"), shape)
    try:
        json.load(io.open(p, encoding="utf-8")).update({"pages": 1})
        chk("A 对照：旧读法在 %s 上会抛" % label, False, "没抛")
    except AttributeError:
        chk("A 对照：旧读法在 %s 上抛 AttributeError" % label, True)
    except Exception as e:  # noqa: BLE001
        chk("A 对照：旧读法在 %s 上抛 AttributeError" % label, False,
            "%s: %s" % (type(e).__name__, e))


# ── B. _seen_image_urls：一条脏 raw 记录只跳一条 ──────────────────────
d = fresh("raw-mixed")
put(os.path.join(d, "0001.json"), {"images": [{"url": "https://a/1.png"},
                                              {"url": "https://a/2.png"}]})
put(os.path.join(d, "0002.json"), [])                         # 整份是列表
put(os.path.join(d, "0003.json"), "不是对象")                  # 整份是字符串
put(os.path.join(d, "0004.json"), {"images": {"url": "x"}})   # images 不是列表
put(os.path.join(d, "0005.json"), {"images": [1, None, {"nope": 1},
                                              {"url": 5}, {"url": ""},
                                              {"url": "https://a/3.png"}]})
put(os.path.join(d, "0006.json"), None, raw="{半截")           # 读不开
put(os.path.join(d, "notes.txt"), "images")                   # 非 .json
put(os.path.join(d, "0007.json"), {"images": [{"url": "https://a/1.png"}]})  # 重复
got = E._seen_image_urls(d)
chk("B 混合清单：坏记录整份跳过，好记录照收、去重、非 .json 不看",
    got == {"https://a/1.png", "https://a/2.png", "https://a/3.png"}, got)

d = fresh("raw-missing")
chk("B 目录不存在：回空集，不抛", E._seen_image_urls(os.path.join(d, "nope")) == set())

d = fresh("raw-clean")
put(os.path.join(d, "0001.json"), {"images": [{"url": "https://a/x.jpg"}]})
chk("B 干净清单：一个字不改", E._seen_image_urls(d) == {"https://a/x.jpg"})

# 对照：旧读法在「整份是列表」与「images 里混进非对象」上确实抛
d = fresh("raw-old-list")
p = put(os.path.join(d, "0001.json"), [])
try:
    for img in json.load(io.open(p, encoding="utf-8")).get("images", []):
        img["url"]
    chk("B 对照：旧读法在列表形状上会抛", False, "没抛")
except AttributeError:
    chk("B 对照：旧读法在列表形状上抛 AttributeError", True)

d = fresh("raw-old-item")
p = put(os.path.join(d, "0001.json"), {"images": [1]})
try:
    for img in json.load(io.open(p, encoding="utf-8")).get("images", []):
        img["url"]
    chk("B 对照：旧读法在非对象项上会抛", False, "没抛")
except TypeError:
    chk("B 对照：旧读法在非对象项上抛 TypeError", True)


# ── C. meta 只认对象：与书库共用同一把尺子 ───────────────────────────
for label, shape in DIRTY_SHAPES:
    d = fresh("meta-" + label)
    put(os.path.join(d, "meta.json"), shape)
    m = E.book_layout.read_meta(d)
    chk("C meta %s：read_meta 当没有 meta（回空表，不是那份脏数据）" % label,
        m == {}, m)

d = fresh("meta-good")
put(os.path.join(d, "meta.json"), {"title": "活着", "author": "余华", "keep": 1})
chk("C 好 meta：一个字不改",
    E.book_layout.read_meta(d) == {"title": "活着", "author": "余华", "keep": 1})

# 引擎收尾写 done 的那一步：脏 meta 不许把它判成失败
d = fresh("meta-done-dirty")
put(os.path.join(d, "meta.json"), [])
meta = E.book_layout.read_meta(d)
meta.update({"done": True, "chapters": 3, "missing": [], "updated_at": 2})
chk("C 收尾：脏 meta 上 update 得动（done 写得进去）",
    meta.get("done") is True and meta.get("chapters") == 3, meta)

# ── D. 接线：引擎自己不再留旧的裸读法 ─────────────────────────────────
src = io.open(os.path.join(ROOT, "export_precise.py"), encoding="utf-8").read()
chk("D 接线：开头与收尾的 meta 都走 book_layout.read_meta（两处）",
    src.count("book_layout.read_meta(book_dir)") == 2,
    src.count("book_layout.read_meta(book_dir)"))
chk("D 接线：续传清点走 _seen_image_urls（不再内联 json.load(...).get）",
    "seen_imgs = _seen_image_urls(raw_dir)" in src
    and 'json.load(open(os.path.join(raw_dir, jf)' not in src)
chk("D 接线：write_progress 对读出来的 src 有一道 isinstance 闸",
    "data = data if isinstance(data, dict) else {}" in src)
chk("D 接线：引擎不再凭空引用 meta_path（收尾读写都按 book_dir 现拼）",
    "meta_path" not in src)

shutil.rmtree(TMP, ignore_errors=True)
print()
print("取书引擎脏读口取证：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
