# -*- coding: utf-8 -*-
"""安娜账本那一个落盘口（anna_state._atomic_write）：临时件不许共用，失败不留残件。

为什么单开一条门禁
    `cache/anna.json` 这一份账本**不是一个人在用**：

      · 后端请求线程 —— `/api/anna` 的 search / domain / 清账都调
        `anna_state.write_state()`，而后端是 `ThreadingHTTPServer`，两个请求可以同时
        进 `write_state`（这一路没走 `_WRITE_LOCK`）；
      · 窗口那一趟子进程 —— `anna_browser.py` 领关键词、接住下载、收尾报数，十来个
        调用点也写同一份账本。

    旧写法把临时件写死成 `path + ".tmp"`，两个写手同时进来时有两种代价：

      · **写坏**：后来者 `open(..., "w")` 把前一个人写了一半的临时件截断，两边各自
        按自己的文件偏移往下写，`os.replace` 把两份内容交织在一起的那一份顶成正式
        账本 —— `read_state` 解不动就整份退回空账，界面上是「入库记录全没了」；
      · **当场抛**：先收尾的那一个 `os.replace` 把共用的临时件整个搬走，后一个写手
        轮到自己那一句时临时件已经不在，抛 `FileNotFoundError`。这条异常从
        `counted()` / `write_state()` 直接穿出去：`anna_browser.handle_download`
        是在 `counted()` **之后**才 `queue.append(...)` 的，异常一抛，文件接住了却
        永远排不进队列（书架上就是没有这本，得等下一次「补收」）。

    同一个毛病还有第二半：写入这一步失败（磁盘满、序列化不了）时，旧写法直接把异常
    抛出去，临时件留在 `cache/` 里没人认领。

收口
    临时件一次一个名字（进程号 + 一小串随机），与 `ui_server._write_json` 同一把尺子、
    比 `flomo_notes`（只带进程号）更严一档 —— 因为这条路上真的有两个进程；写失败时
    先把临时件收干净再抛，旧档始终完整（`board` / `mindmap` / `login` / `sync` 那几个
    写口早就是这个规矩）。

    这只解决「账本被写坏」，不解决「读—改—写互相覆盖」：两个写手先后落盘时后一个
    仍可能基于旧读数。计数仍以最后一次为准，下面 E 那两条也因此只钉「解得动、不留
    残件」，不钉条数。

钉五面：
  A. 正常写：写得下、读得回、回执与落盘一致，临时件与正式账本分开、一次一个名字；
  B. 对照：两个写手共用同一个临时件名（旧写法）时，顶上去的不是任何一份完整的账；
  C. 写失败：照旧抛，临时件收干净，旧档逐字节不动；
  D. 崩过之后同一个口子还能用，口令条（`write_cmd`）走的是同一个口子；
  E. 两个真进程同时写：最后落地的账本一定解得动、不留 `.tmp`（实机旁证；这一条的
     回归探测器是 A4「名字一次一个」与 C2「失败不留残件」）。

跑法：纯函数 + 系统临时目录沙盒，不联网、不开浏览器、不起服务，归 --fast 静态段。
"""
import atexit
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import anna_state as anna  # noqa: E402

SB = pathlib.Path(tempfile.mkdtemp(prefix="guizang-anna-atomic-"))
atexit.register(lambda: shutil.rmtree(SB, ignore_errors=True))

P = anna.state_path(str(SB))
FAIL = []
PASSED = [0]


def chk(name, ok, extra=""):
    if ok:
        PASSED[0] += 1
        print("  ok  " + name)
    else:
        FAIL.append(name)
        print(" FAIL " + name + (("  <- " + esc(extra)[:220]) if extra else ""))


def esc(s):
    """extra 里可能带着落单的半个代理码点：直接 print 会自己抛，把一条断言失败
    伪装成整个套件崩。转义成 \\ud800 再打。"""
    return str(s).encode("unicode_escape").decode("ascii")


def tmps():
    """账本与口令条都住在 cache/ 下，残件也只会落在那儿。"""
    d = os.path.dirname(P)
    return sorted(n for n in os.listdir(d) if n.endswith(".tmp")) if os.path.isdir(d) else []


def canon(payload):
    return json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")


def capture_tmps(fn):
    """跑一趟 fn，把它实际打开的临时件名字记下来（问 json.dump 拿到的那个 file 对象）。"""
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


HI = chr(0xD800)          # 落单的半个代理码点，用 chr() 现造，源码里不出现字面量

print("── A. 正常写：分开的临时件、一次一个名字 ──────────────")
PAY = {"who": "甲", "n": 3, "t": "图😀好"}
back = anna._atomic_write(P, PAY)
chk("A1 写得下、读得回，回执与落盘那份一致",
    back == PAY and json.load(open(P, encoding="utf-8")) == PAY,
    (back, open(P, encoding="utf-8").read()[:80]))
chk("A2 正常写完之后目录里没有 .tmp 残件", tmps() == [], tmps())

t1 = capture_tmps(lambda: anna._atomic_write(P, {"a": 1}))
t2 = capture_tmps(lambda: anna._atomic_write(P, {"a": 2}))
chk("A3 临时件与正式账本分开（不是原地截断再写）",
    bool(t1) and t1[0] != os.path.basename(P) and t1[0].endswith(".tmp"), t1)
chk("A4 每次写的临时件名都不一样，且带着自己的进程号",
    bool(t1) and bool(t2) and t1[0] != t2[0] and str(os.getpid()) in t1[0],
    (t1, t2))

# 这一条守的是「摘坏字符」那条老契约没被顺手改掉。
sur = anna._atomic_write(P, {"t": "坏" + HI + "字"})
chk("A5 落单的半个代理码点仍旧摘掉，正常字一个不改",
    sur.get("t") == "坏字" and json.load(open(P, encoding="utf-8")) == sur, sur)

print("── B. 对照：共用同一个临时件名会写出什么 ──────────────")
anna._atomic_write(P, {"who": "上一份好的"})
good_before = open(P, "rb").read()
PAY_A = {"who": "甲", "pad": "甲" * 60}
PAY_B = {"who": "乙", "pad": "乙" * 60}
CA, CB = canon(PAY_A), canon(PAY_B)
old = P + ".tmp"                       # 旧写法：两个写手共用的那一个名字
cut = max(1, len(CA) // 2)             # 甲写到一半；两份都必须够长，这条对照才作数
# 两个文件句柄照着旧写法的顺序各写各的：甲写了一截 → 乙重开（把甲那截截断）整份写 →
# 甲从**自己的偏移**接着往下写。这正是两个进程共用一个临时件名时在磁盘上发生的事。
fa = open(old, "wb")
fb = open(old, "wb")
fa.write(CA[:cut]); fa.flush()
fb.write(CB); fb.flush()
fa.write(CA[cut:]); fa.close()
fb.close()
os.replace(old, P)
raw = open(P, "rb").read()
try:
    json.loads(raw.decode("utf-8"))
    parsed = True
except Exception:
    parsed = False
chk("B1 对照：共用 .tmp 时顶上去的不是任何一份完整的账（旧的写法真会把账写坏）",
    raw not in (CA, CB), "parsed=%s raw=%s" % (parsed, esc(raw[:90])))
chk("B2 即便如此，替换发生之前正式账本一直是完整的（原子性那一半没坏）",
    good_before == canon({"who": "上一份好的"}), good_before[:60])

# 共用同一个临时件名的第二种代价，而且是**当场抛异常**的那一种：先收尾的那一个
# `os.replace` 会把两人共用的临时件整个搬走，后一个写手轮到自己那一句 `os.replace`
# 时，临时件已经不在了 —— 抛 FileNotFoundError，而不是安静地写坏。这条路从 `counted()`
# / `write_state()` 直接穿出去：`anna_browser.handle_download` 在 `counted()` 之后才
# `queue.append(...)`，异常一抛，文件接住了却永远排不进队列（书架上就是没有这本）。
shared = P + ".tmp"
with open(shared, "w", encoding="utf-8") as f1:
    json.dump({"who": "甲"}, f1, ensure_ascii=False, indent=1)
with open(shared, "w", encoding="utf-8") as f2:        # 乙也开工，还抢先把临时件顶替了
    json.dump({"who": "乙"}, f2, ensure_ascii=False, indent=1)
    os.replace(shared, P)
lost = None
try:
    os.replace(shared, P)                              # 甲轮到自己那一句
except FileNotFoundError:
    lost = "FileNotFoundError"
chk("B3 对照：共用 .tmp 时后一个写手的 replace 会当场 FileNotFoundError（异常穿出回调）",
    lost == "FileNotFoundError", lost)

print("── C. 写失败：照旧抛、收干净、旧档不动 ────────────────")
anna._atomic_write(P, {"good": "旧的一份"})
before = open(P, "rb").read()
raised = None
try:
    anna._atomic_write(P, {"bad": {1, 2}})     # set 不是 JSON 类型，必然写不进去
except TypeError:
    raised = "TypeError"
except Exception as e:                          # noqa: BLE001 —— 别的异常也算没接住
    raised = type(e).__name__
chk("C1 真写不进去时照旧抛（不吞异常、不静默丢这一趟）", raised == "TypeError", raised)
chk("C2 写崩后临时件收干净了", tmps() == [], tmps())
chk("C3 写崩不毁旧档（旧的一份逐字节还在）", open(P, "rb").read() == before,
    open(P, "rb").read()[:80])

print("── D. 崩过之后还能用；口令条同一个口子 ────────────────")
st = anna.counted(str(SB), {"caught_delta": 3})
chk("D1 崩过之后同一个口子照旧可用（write_state 落得下）",
    st.get("caught") == 3 and json.load(open(P, encoding="utf-8")).get("caught") == 3,
    st)
cmd = anna.write_cmd(str(SB), "三体")
chk("D2 口令条走同一个口子：写得下、读得回",
    cmd.get("term") == "三体"
    and json.load(open(anna.cmd_path(str(SB)), encoding="utf-8")).get("term") == "三体",
    cmd)
chk("D3 两个口子写完之后也都没有 .tmp 残件", tmps() == [], tmps())

print("── E. 两个真进程同时写：账本一定解得动 ────────────────")
# A4 / C2 才是这一条的**回归探测器**（拿旧写法跑一定红）；这一节是「就这样也照常」那
# 一半：两个真进程各写 150 次，最后落地的必须是**某一份完整的账**、不是两份交织。
# 旧写法在这一节**可能**侥幸不红（谁先谁后由调度决定），所以不当探测器用，只当实机旁证；
# 确定性那一半在 B1（共用 .tmp 会写出什么）与 A4（名字一次一个）。
CHILD = (
    "import sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import anna_state as anna\n"
    "for i in range(150):\n"
    "    anna._atomic_write(sys.argv[2], {'who': 'child', 'i': i, 'pad': '子' * 400})\n"
)
procs = [subprocess.Popen([sys.executable, "-c", CHILD, str(REPO), P],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
         for _ in range(2)]
for i in range(150):
    anna._atomic_write(P, {"who": "parent", "i": i, "pad": "父" * 400})
kids = [(pr.wait(timeout=90), pr.communicate()[0][-200:]) for pr in procs]
ok, why = True, ""
try:
    json.load(open(P, encoding="utf-8"))
except Exception as e:
    ok, why = False, "%s: %s" % (type(e).__name__, e)
chk("E1 两个进程各写 150 次，最后落地的账本一定解得动", ok, why)
chk("E2 并写之后也不留 .tmp 残件", tmps() == [], tmps())
chk("E3 两个子进程都正常收工（没有把异常咽进临时件里）",
    all(rc == 0 for rc, _ in kids), kids)

print("\n安娜账本落盘体检：%d 项通过，失败 %d 项" % (PASSED[0], len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
