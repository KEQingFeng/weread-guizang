# -*- coding: utf-8 -*-
"""运行时回执（platform_compat.write_runtime）：临时件一次一个名字，失败 / 中止都收干净。

为什么单开一条门禁
    `runtime.json` 是「服务这一趟绑到了哪个口」的唯一回执：8770 被占时服务会自己
    往后找端口，调用方（MCP 适配器、壳）只认这份回执。这一格有两条写手够得着同一
    份文件 —— 用户连点两下启动图标、或 `--takeover` 的新实例跟还没退出的旧实例各自
    绑到一个端口时，两个 `write_runtime` 会撞在一起。

    旧写法把临时件写死成 `runtime_file(repo) + ".tmp"`，于是：

      · **当场抛**：先收尾的那个 `os.replace` 把共用的临时件整个搬走，后一个轮到
        自己那一句时文件已经不在，抛 `FileNotFoundError`。而这一层把它吞成 `False`
        （写失败不影响服务本身），于是那个进程最终绑的端口一个字都没落下，适配器
        照着旧回执去敲一个没人应答的口子 —— 用户看到的是「归藏没开着」；
      · **写坏**：后来者 `open(..., "w")` 把前一个人写了一半的临时件截断，两边各按
        自己的偏移往下写，`os.replace` 把两份交织的 JSON 顶成正式回执，读它的人
        （`json.loads`）当场解不动。

    同一个毛病还有第二半：写入这一步失败（盘满 / 只读目录）时，旧写法一行收尾都
    没有，半截 `.tmp` 留在数据根（源码直接跑时就是仓库根）没人认领；中止信号那条
    退出路径也一样。

收口
    临时件从 `_tmp_for(path)` 取名字（进程号 + 一小串随机，与画板 / 笔记 / 便签 /
    写作 / 取书同一把尺子），失败与中止信号两条退出路径都先把残件收干净；写入失败
    仍按老契约吞成 `False`（别把服务启动带崩），但 `KeyboardInterrupt` / `SystemExit`
    照旧往外抛（用户要停，不该被这一层吞掉）。`.gitignore` 同时把随机名那一族排掉，
    断电 / 强杀留下的半截件不会在 `git status` 里冒充新文件。

钉五面：
  A. 正常写：写得下、读得回、临时件与正式回执分开且一次一个名字、就落在同一个目录；
  B. 对照：两个写手共用固定 `.tmp` 时，后一个 `os.replace` 当场 `FileNotFoundError`；
  C. 交错写：把「第二个写手」插在第一次 `json.dump` 与 `os.replace` 之间，新写法两次
     都成功、最终回执解得动、不留残件（B 是它的回归探测器）；
  D. 写失败：吞成 `False`、残件收干净、旧回执逐字节不动；
  E. 中止信号：`KeyboardInterrupt` / `SystemExit` 照旧往外抛，残件同样收干净；
  F. 接线与源码形状：`ui_server` 仍从 `pc.write_runtime` 写端口、`.gitignore` 认随机名。

跑法：纯函数 + 系统临时目录沙盒（`GUIZANG_DATA` 指进沙盒），不联网、不开浏览器、
不起服务，归 --fast 静态段。
"""
import atexit
import json
import os
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import platform_compat as pc  # noqa: E402

SB = pathlib.Path(tempfile.mkdtemp(prefix="guizang-runtime-atomic-"))
atexit.register(lambda: shutil.rmtree(SB, ignore_errors=True))
REPO_S = str(REPO)
DEST = pathlib.Path(pc.runtime_file(REPO_S))          # GUIZANG_DATA 还没设，先看默认

_OLD_DATA = os.environ.get("GUIZANG_DATA")
os.environ["GUIZANG_DATA"] = str(SB)                   # data_dir(repo) 跟着它走
DEST = pathlib.Path(pc.runtime_file(REPO_S))
assert str(DEST).startswith(str(SB)), DEST             # 沙盒没生效就别往下跑

FAIL = []
PASSED = [0]


def chk(name, ok, extra=""):
    if ok:
        PASSED[0] += 1
        print("  ok  " + name)
    else:
        FAIL.append(name)
        print(" FAIL " + name + (("  <- " + str(extra)[:220]) if extra else ""))


def tmps():
    return sorted(n for n in os.listdir(SB) if n.endswith(".tmp"))


def read_dest():
    with open(DEST, encoding="utf-8") as f:
        return json.load(f)


def capture_names(fn):
    """跑一趟 fn，把它实际写临时件的那个文件名记下来（问 json.dump 拿到的 file 对象）。"""
    seen = []
    real = json.dump

    def spy(obj, fp, *a, **kw):
        seen.append(os.path.basename(getattr(fp, "name", "")))
        return real(obj, fp, *a, **kw)

    json.dump = spy
    try:
        fn()
    finally:
        json.dump = real
    return seen


def interleaved(fn, second):
    """跑 fn，但在它第一次 `json.dump` 落地之后、`os.replace` 之前插进 second 那一趟完整写。

    这正是两个写手在同一份回执上错开的那一瞬：固定名时 second 的 `os.replace` 会把
    共用的临时件搬走，fn 轮到自己那一句时文件名已经不在（B 用这一点做对照）。
    """
    real = json.dump
    state = {"fired": False}

    def hook(obj, fp, *a, **kw):
        out = real(obj, fp, *a, **kw)
        if not state["fired"]:
            state["fired"] = True
            second()
        return out

    json.dump = hook
    try:
        return fn()
    finally:
        json.dump = real


def _old_write(dest, port):
    """改之前那把 write_runtime 的形状：临时件写死成 `<目标>.tmp`，失败一行不收。"""
    tmp = str(dest) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"port": port}, f)
    os.replace(tmp, str(dest))


print("── A. 正常写：分开的临时件、一次一个名字 ──────────────")
ok = pc.write_runtime(REPO_S, 8770, version="1.2.1", code="abc123")
chk("A1 写得下，回执解得动、端口对得上",
    ok is True and read_dest().get("port") == 8770, (ok, read_dest()))
chk("A2 正常写完之后沙盒里没有 .tmp 残件", tmps() == [], tmps())

n1 = capture_names(lambda: pc.write_runtime(REPO_S, 8770))
n2 = capture_names(lambda: pc.write_runtime(REPO_S, 8771))
chk("A3 临时件与正式回执分开（不是原地截断再写）",
    bool(n1) and n1[0] != DEST.name and n1[0].endswith(".tmp"), n1)
chk("A4 每次写的临时件名都不一样，且带着自己的进程号",
    bool(n1) and bool(n2) and n1[0] != n2[0] and str(os.getpid()) in n1[0],
    (n1, n2))
chk("A5 临时件就落在数据根（同一个文件系统，os.replace 才是原子的）",
    bool(n1) and os.path.dirname(os.path.join(str(SB), n1[0])) == str(SB), n1)

print("── B. 对照：共用固定 .tmp 会当场 FileNotFoundError ─────")
lost = None
try:
    interleaved(lambda: _old_write(DEST, 1), lambda: _old_write(DEST, 2))
except FileNotFoundError:
    lost = "FileNotFoundError"
except Exception as e:                                  # noqa: BLE001 —— 别的异常也算没接住
    lost = type(e).__name__
chk("B1 对照：共用 .tmp 时后一个写手的 replace 扑空（旧写法的代价是真的）",
    lost == "FileNotFoundError", lost)
if os.path.exists(str(DEST) + ".tmp"):
    os.remove(str(DEST) + ".tmp")

print("── C. 交错写：新写法两次都成功，回执解得动 ────────────")
first = interleaved(lambda: pc.write_runtime(REPO_S, 8770),
                    lambda: pc.write_runtime(REPO_S, 9999))
try:
    doc = read_dest()
    parsed = True
except Exception as e:                                  # noqa: BLE001
    doc, parsed = {"err": "%s: %s" % (type(e).__name__, e)}, False
chk("C1 第二个写手插在 dump 与 replace 之间，两次写都回 True",
    first is True, first)
chk("C2 最终回执一定解得动、是完整的一份（不是两份交织的 JSON）",
    parsed and doc.get("port") in (8770, 9999), doc)
chk("C3 交错写之后也不留 .tmp 残件", tmps() == [], tmps())

print("── D. 写失败：吞成 False、收干净、旧回执不动 ──────────")
pc.write_runtime(REPO_S, 8770)
before = DEST.read_bytes()
real_replace = os.replace
raised = {}


def replace_boom(*a, **kw):
    raised["called"] = True
    raise OSError("磁盘满")


pc.os.replace = replace_boom
try:
    d1 = pc.write_runtime(REPO_S, 8888)
finally:
    pc.os.replace = real_replace
chk("D1 replace 失败时回 False（服务启动不许被这一格带崩）",
    d1 is False and raised.get("called"), (d1, raised))
chk("D2 replace 失败后残件收干净了", tmps() == [], tmps())
chk("D3 replace 失败不毁旧回执（旧的一份逐字节还在）",
    DEST.read_bytes() == before, DEST.read_bytes()[:120])

real_dump = json.dump


def dump_boom(*a, **kw):
    raise TypeError("序列化不了")


json.dump = dump_boom
try:
    d2 = pc.write_runtime(REPO_S, 8888)
finally:
    json.dump = real_dump
chk("D4 dump 失败时也回 False，残件同样收干净",
    d2 is False and tmps() == [], (d2, tmps()))
chk("D5 dump 失败不毁旧回执", DEST.read_bytes() == before, DEST.read_bytes()[:120])

print("── E. 中止信号：照旧往外抛，残件收干净 ────────────────")


def dump_interrupt(exc):
    def boom(*a, **kw):
        raise exc
    return boom


for label, exc in (("KeyboardInterrupt", KeyboardInterrupt()),
                   ("SystemExit", SystemExit(2))):
    json.dump = dump_interrupt(exc)
    got = None
    try:
        pc.write_runtime(REPO_S, 8888)
    except BaseException as e:                          # noqa: BLE001 —— 这里要的就是它
        got = type(e).__name__
    finally:
        json.dump = real_dump
    chk("E1 %s 照旧往外抛（用户要停，不该被这一层吞掉）" % label,
        got == type(exc).__name__, got)
    chk("E2 %s 之后残件收干净了" % label, tmps() == [], tmps())

print("── F. 接线与源码形状 ────────────────────────────────")
src = (REPO / "platform_compat.py").read_text(encoding="utf-8")
chk("F1 write_runtime 的临时件从 _tmp_for 取（不再是 runtime_file(repo) + \".tmp\"）",
    "_tmp_for(dest)" in src and 'runtime_file(repo) + ".tmp"' not in src)
chk("F2 _tmp_for 用的是 pid + 一小串随机（与这一族同一把尺子）",
    'uuid.uuid4().hex[:8]' in src and 'os.getpid()' in src)
usrc = (REPO / "ui_server.py").read_text(encoding="utf-8")
chk("F3 接线：服务绑到端口后仍从 pc.write_runtime 写回执",
    "pc.write_runtime(REPO, p," in usrc)
gi = (REPO / ".gitignore").read_text(encoding="utf-8")
chk("F4 .gitignore 认随机名那一族（强杀残件不在 git status 里冒充新文件）",
    "runtime.json.*.tmp" in gi, gi[-120:])

if _OLD_DATA is None:
    os.environ.pop("GUIZANG_DATA", None)
else:
    os.environ["GUIZANG_DATA"] = _OLD_DATA

print("\n运行时回执落盘体检：%d 项通过，失败 %d 项" % (PASSED[0], len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
