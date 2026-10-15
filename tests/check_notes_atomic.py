#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""笔记落盘临时件收口（book_notes.py）：两个写手同时写，不许把整本账写坏。

为什么单独一份：`notes.json` 是这本书划线的**唯一凭据**（`load_notes` 解不动就
整份退回空结构，界面上就是「这本书的划线全没了」），而 `_write_json` / `_write_text`
原来把临时件写死成 `path + ".tmp"`。后端是 `ThreadingHTTPServer`，同一个进程里
两个请求能并着进来（一本书的 `/api/mynotes` 保存和 `/api/mynotes/export`、或者
两个标签页各存一次），于是：

  · 后来者 `open(tmp, "w")` 把前一个人写了一半的临时件截断，两边各自按自己的
    偏移往下写，最后 `os.replace` 把**两份内容交织在一起**的那一份顶成正式账本；
  · 先收尾的那一个 `os.replace` 会把共用的临时件整个搬走，后一个写手走到自己
    那句 `os.replace` 时临时件已经不在，抛 `FileNotFoundError` —— 界面上就是
    「存不下」，可它前面那份账也已经被搅了。

与 `anna_state._atomic_write` / `flomo_notes` 踩的是同一个坑，收口口径也一致：
临时件名字一次一个（进程号 + 随机串），失败 / 中止路径把临时件收掉；此外不再让
「旧档先挪成 .prev、新档顶不上去」把 `notes.json` 整个留空。

这一条把落盘口钉住：
  · 正常写：内容原样、改名完成、不留 .tmp、上一版仍留在 .prev；
  · 临时件名字一次一个（带进程号、两次不同）；
  · 对照：共用 .tmp 的老写法确实会写出解不动的账 / 让第二个写手 FileNotFoundError；
  · 写一半崩（序列化失败 / Ctrl-C）、改名失败：旧档必须完整，.tmp 必须收掉；
  · 两个线程各写 50 次：不抛、落地的一定是两份完整稿之一、不留 .tmp；
  · 接线：两个写口都走 `_tmp_name`，不再写死 `path + ".tmp"`。

纯逻辑 + 系统临时目录里的沙盒书库，不起服务、不联网、不碰真实书库，归静态段，
`--fast` 也要带。
"""
import inspect
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 数据目录指进临时沙盒再 import：免得这条路真去动用户真实书库。
SANDBOX = pathlib.Path(tempfile.mkdtemp(prefix="guizang-notes-atomic-"))
os.environ["GUIZANG_DATA"] = str(SANDBOX / "cache")
os.environ["GUIZANG_BOOKS"] = str(SANDBOX / "books")
DIR = str(SANDBOX / "sandbox")
os.makedirs(DIR, exist_ok=True)

import book_notes  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


real_open = open
real_os = os


def read_text(p):
    with io.open(p, encoding="utf-8") as f:
        return f.read()


def tmps():
    return sorted(n for n in os.listdir(DIR) if n.endswith(".tmp"))


# ── 一、正常写：内容原样、改名完成、不留 .tmp ─────────────────────
P = os.path.join(DIR, "notes.json")
book_notes._write_json(P, {"items": ["A1", "A2"]})
chk("基本：内容原样落盘、原样读回",
    json.loads(read_text(P)) == {"items": ["A1", "A2"]}, read_text(P))
chk("基本：不留 .tmp", tmps() == [], tmps())

# 覆盖：新稿顶上，上一版仍留在 .prev（旧行为不许被这次收口改掉）
book_notes._write_json(P, {"items": ["B1"]})
chk("覆盖：新稿顶上", json.loads(read_text(P)) == {"items": ["B1"]}, read_text(P))
chk("覆盖：上一版仍在 .prev", json.loads(read_text(P + ".prev")) == {"items": ["A1", "A2"]},
    read_text(P + ".prev"))
chk("覆盖后仍不留 .tmp", tmps() == [], tmps())

# ── 二、临时件名字一次一个（带进程号、两次不同） ────────────────────
seen = []
real_tmp_name = book_notes._tmp_name


def capture(path):
    n = real_tmp_name(path)
    seen.append(n)
    return n


try:
    book_notes._tmp_name = capture
    book_notes._write_json(os.path.join(DIR, "n2.json"), {"a": 1})
    book_notes._write_text(os.path.join(DIR, "n2.txt"), "hello")
finally:
    book_notes._tmp_name = real_tmp_name

chk("名字：临时件确实叫过（两个写口都走同一把尺子）", len(seen) == 2, seen)
chk("名字：两次名字不相同", len(seen) == 2 and seen[0] != seen[1], seen)
chk("名字：带本进程号，落在目标旁边、以 .tmp 结尾",
    bool(seen) and all(str(os.getpid()) in n and n.endswith(".tmp")
                       and os.path.dirname(n) == DIR for n in seen), seen)
chk("名字：不是目标路径就地加 .tmp 那个共用名",
    all(not n.endswith("n2.json.tmp") and not n.endswith("n2.txt.tmp") for n in seen), seen)

# ── 三、对照：共用 .tmp 的老写法真会把账写坏 ─────────────────────────
# 没有这一条，上面的断言可能只是因为「写坏了也没人看」。这里按老写法排一次确定的
# 交织：A 先写半截，B 用同一个名字截断并写完，A 再按自己的偏移接着写，最后 A 收尾。
old_target = os.path.join(DIR, "old.json")
shared = old_target + ".tmp"
fa = real_open(shared, "w", encoding="utf-8")
fa.write('{"items": ["A1", "A2", ')
fb = real_open(shared, "w", encoding="utf-8")     # 同名截断 A 写了一半的那份
fb.write('{"items": ["B1", "B2"]}')
fa.write('"A3"]}')                                # A 接着自己那个偏移写
fa.close()
fb.close()
real_os.replace(shared, old_target)               # A 收尾，把交织内容顶成正式账
broken = read_text(old_target)
try:
    json.loads(broken)
    parseable = True
except Exception:
    parseable = False
chk("对照：共用 .tmp 顶上去的不是任何一份完整的账（解不动）", not parseable, broken)
chk("对照：它也不是 B 的那份完整稿", broken != '{"items": ["B1", "B2"]}', broken)

# 共用一个名字时，先收尾那个把临时件搬走，后一个写手的 replace 当场 FileNotFoundError
try:
    real_os.replace(shared, old_target)
    second_threw = None
except FileNotFoundError as e:
    second_threw = e
chk("对照：共用 .tmp 时后一个写手的 replace 当场 FileNotFoundError",
    isinstance(second_threw, FileNotFoundError), second_threw)
shutil.rmtree(DIR, ignore_errors=True)
os.makedirs(DIR, exist_ok=True)

# ── 四、失败路径：旧档必须完整，.tmp 必须收掉 ───────────────────────
# 四·a：序列化失败（内容里有 json 认不出的东西）—— 抛，但旧档一字未动
target = os.path.join(DIR, "d1.json")
book_notes._write_json(target, {"v": "old"})
before = read_text(target)
threw = None
try:
    book_notes._write_json(target, {"v": {1, 2}})     # set 不可序列化
except Exception as e:                                # noqa: BLE001
    threw = e
chk("序列化失败：异常照旧往外抛", threw is not None, threw)
chk("序列化失败：旧档一字未动", read_text(target) == before, read_text(target))
chk("序列化失败：不留 .tmp", tmps() == [], tmps())


# 四·b：改名失败 —— 新稿顶不上去时，旧档不许被挪成 .prev 就丢了
class _BoomOS:
    """只把「临时件改名顶上去」那一步打掉，其余（path / remove / getpid）原样透传。"""

    def __getattr__(self, name):
        return getattr(real_os, name)

    def replace(self, a, b):
        if str(a).endswith(".tmp"):
            raise OSError("改名失败")
        return real_os.replace(a, b)


target2 = os.path.join(DIR, "d2.json")
book_notes._write_json(target2, {"v": "old"})
threw2 = None
try:
    book_notes.os = _BoomOS()
    book_notes._write_json(target2, {"v": "new"})
except OSError as e:
    threw2 = e
finally:
    book_notes.os = real_os
chk("改名失败：抛出 OSError", isinstance(threw2, OSError), threw2)
chk("改名失败：旧档还在（没被挪成 .prev 就丢）",
    os.path.isfile(target2) and json.loads(read_text(target2)) == {"v": "old"},
    os.path.exists(target2))
chk("改名失败：不留 .tmp", tmps() == [], tmps())


# 四·c：写到一半被 Ctrl-C —— 异常照旧往外抛，但旧档完整、.tmp 收干净
class _HalfFile:
    """open(path,"w") 的替身：先写半截，再按「被强杀」的样子炸掉。"""

    def __init__(self, real):
        self._real = real

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._real.close()
        return False

    def write(self, s):
        self._real.write(str(s)[:3])          # 半截
        raise KeyboardInterrupt()


target3 = os.path.join(DIR, "d3.json")
book_notes._write_json(target3, {"v": "old"})


def _half_open(path, mode="r", *a, **k):
    return _HalfFile(real_open(path, mode, *a, **k))


ki = False
try:
    book_notes.open = _half_open
    book_notes._write_json(target3, {"v": "new"})
except KeyboardInterrupt:
    ki = True
finally:
    del book_notes.open
chk("写一半被 Ctrl-C：异常照旧往外抛", ki)
chk("写一半被 Ctrl-C：旧档完整", json.loads(read_text(target3)) == {"v": "old"},
    read_text(target3))
chk("写一半被 Ctrl-C：不留 .tmp", tmps() == [], tmps())

# ── 五、两个线程各写 50 次：落地的一定是两份完整稿之一 ────────────────
conc = os.path.join(DIR, "conc.json")
docs = ({"items": ["x" * 200 + "A"]}, {"items": ["y" * 200 + "B"]})
errs = []


def hammer(doc):
    for _ in range(50):
        try:
            book_notes._write_json(conc, doc)
        except BaseException as e:                       # noqa: BLE001
            errs.append(repr(e))


ts = [threading.Thread(target=hammer, args=(docs[i],)) for i in range(2)]
for t in ts:
    t.start()
for t in ts:
    t.join()
chk("并写：两个写手都不抛（不再有 FileNotFoundError 穿出来）", not errs, errs[:3])
try:
    landed = json.loads(read_text(conc))
except Exception as e:                                   # noqa: BLE001
    landed = "解不动：%r" % (e,)
chk("并写：落地的账本解得动，且是两份完整稿之一", landed in docs, landed)
chk("并写：写完之后不留 .tmp", tmps() == [], tmps())

# ── 六、真入口：save_notes / export_notes 端到端不留残件 ─────────────
book = os.path.join(DIR, "book")
os.makedirs(book, exist_ok=True)
book_notes.save_notes(book, {"marks": [], "entries": [], "note": "一句话"})
saved = book_notes.load_notes(book)
chk("save_notes：落得下、解得动", saved.get("note") == "一句话", saved)
chk("save_notes：不留 .tmp",
    [n for n in os.listdir(book) if n.endswith(".tmp")] == [], os.listdir(book))
res = book_notes.export_notes(book, saved, book_label="测试")
chk("export_notes：notes.md 落下了", os.path.isfile(res["path"]) and res["chars"] > 0, res)
chk("export_notes：不留 .tmp",
    [n for n in os.listdir(book) if n.endswith(".tmp")] == [], os.listdir(book))

# ── 七、接线：两个写口都走 _tmp_name，收口不是空转 ────────────────────
src_json = inspect.getsource(book_notes._write_json)
src_text = inspect.getsource(book_notes._write_text)
chk("接线：_write_json 的临时件走 _tmp_name",
    "_tmp_name(" in src_json and 'path + ".tmp"' not in src_json, src_json[:160])
chk("接线：_write_text 的临时件走 _tmp_name",
    "_tmp_name(" in src_text and 'path + ".tmp"' not in src_text, src_text[:160])
chk("接线：失败 / 中止路径收临时件（BaseException + _discard）",
    "BaseException" in src_json and "_discard(" in src_json)
chk("接线：改名失败会把 .prev 搬回来", "os.replace(prev, path)" in src_json)

shutil.rmtree(SANDBOX, ignore_errors=True)

print("\n笔记落盘临时件收口体检：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
for name in FAIL:
    print("  x " + name)
sys.exit(1 if FAIL else 0)
