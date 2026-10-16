# -*- coding: utf-8 -*-
"""接管端口时「杀进程」那一步的护栏（platform_compat.stop_backend / hard_kill_pid）。

为什么单开一条门禁
    `hard_kill_pid` 与它上面那层 `stop_backend` 是全仓唯一会**打别人**的动作，
    唯一的调用场景是「一键换新后端」：端口被一份旧代码占着时，把它请走、让新进程
    接管。这一段此前一条离线门禁都没有（`test_backend_identity.py` 要真起进程、
    真占端口，归全套真机段），而它手里握着 SIGTERM / SIGKILL 两种最重的手段。

    旧写法有两处「没看清对象就打」：

      · `hard_kill_pid(pid)` 直接 `os.killpg(os.getpgid(pid), SIGKILL)`。`pid` 为
        0 时 `os.getpgid(0)` 拿到的是**本进程自己的进程组** —— 这一下等于让归藏
        当场把自己连同整个 job 一起打死；负数更糟，`os.kill(-1, SIGKILL)` 会发给
        用户能发的每一个进程；目标恰好与自己在同一组时，那句「连子孙一起收」也会
        把归藏自己一起收走。
      · `stop_backend(pid)` 只信 `take_over_port` 一开始那个 `is_our_backend` 判据，
        中间隔着一次 HTTP 问答、一次 `lsof` 以及最长 `wait` 秒的等待。那条进程完全
        可能已经自己退了、PID 被回收给了一个毫不相干的程序（PID 回绕），于是
        SIGTERM / SIGKILL（连带整个进程组）落在陌生人头上。

收口
    `hard_kill_pid` 在动手之前先挡三样：`pid <= 0`、目标就是本进程、目标与自己在
    同一进程组（这一档只杀它一个，不做 `killpg`）。`stop_backend` 在 SIGTERM 之前
    与硬杀之前各复核一次「它还是不是归藏的后端」，认不出就不动手 —— 与
    `port_listener_pid` 的「宁可不动，也不猜着杀」同一条规矩。

钉五面（A–E 用行为级间谍顶掉 os.kill / os.killpg / os.getpgid，只记账、不真发信号）：
  A. pid <= 0 / 自己 / 认不出的形状：一个信号都不发；
  B. 目标与自己在同一进程组：只 `os.kill` 它一个，绝不用 `killpg`；
  C. 正常情形（别组）：仍 `killpg` 目标那一组 —— 该连子孙一起收的照旧；
  D. `stop_backend`：认不出目标时一个信号都不发；是我们的后端时 SIGTERM 照发、
     硬杀照走；等完之后复核失败（PID 已被分走）时不用 `killpg` 打陌生进程；
  E. 接线与源码形状：`take_over_port` 仍走 `stop_backend`、发信号之前过了
     `is_our_backend`；
  F. 真跑一遍的对照：在**自己一组的子进程**里调 `hard_kill_pid(0)` 与
     `hard_kill_pid(os.getpid())`，进程还得活着（老写法这一下就把自己打死了）。

跑法：纯函数 + 进程内间谍，外加一个另起会话的子进程；不联网、不开浏览器、
不起服务，归 --fast 静态段。
"""
import os
import pathlib
import signal
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))          # 被测模块在仓库根
import platform_compat as pc  # noqa: E402

FAIL, PASSED = [], [0]

_REAL = {"kill": os.kill, "killpg": os.killpg, "getpgid": os.getpgid,
         "is_our_backend": pc.is_our_backend}


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:240]))
    if cond:
        PASSED[0] += 1
    else:
        FAIL.append(name)


def install(pgid_map=None, kill_raises=False):
    """把三个「真会动手」的系统调用换成只记账的间谍。

    `pgid_map` 用来指定某个 pid 落在哪个进程组（键是 pid，值是 pgid）；
    没点在表里的 pid 回落到真的 `os.getpgid`。
    """
    calls = {"kill": [], "killpg": [], "getpgid": []}

    def fake_kill(pid, sig):
        calls["kill"].append((pid, sig))
        if kill_raises:
            raise OSError(3, "gone")

    def fake_killpg(pgid, sig):
        calls["killpg"].append((pgid, sig))

    def fake_getpgid(pid):
        calls["getpgid"].append(pid)
        if pgid_map and pid in pgid_map:
            return pgid_map[pid]
        return _REAL["getpgid"](pid)

    os.kill = fake_kill
    os.killpg = fake_killpg
    os.getpgid = fake_getpgid
    return calls


def restore():
    os.kill = _REAL["kill"]
    os.killpg = _REAL["killpg"]
    os.getpgid = _REAL["getpgid"]
    pc.is_our_backend = _REAL["is_our_backend"]


def with_is_ours(fn):
    pc.is_our_backend = fn


# ── A. pid <= 0 / 自己 / 认不出的形状：一个信号都不发 ───────────────────
# 这一组就是老写法的对照：0 与 -1 在旧写法里分别等于「杀自己这一组」与
# 「杀用户能发的每一个进程」，`hard_kill_pid(os.getpid())` 同样会 killpg 到自家人。
for bad, label in ((0, "0"), (-1, "-1"), (-12345, "-12345")):
    calls = install()
    try:
        pc.hard_kill_pid(bad)
    finally:
        restore()
    chk("A hard_kill_pid(%s) 一个信号都不发" % label,
        calls["kill"] == [] and calls["killpg"] == [], calls)

calls = install()
try:
    pc.hard_kill_pid(os.getpid())
finally:
    restore()
chk("A hard_kill_pid(自己) 一个信号都不发",
    calls["kill"] == [] and calls["killpg"] == [], calls)

calls = install()
try:
    for junk in ("abc", None, [], {}):
        pc.hard_kill_pid(junk)
finally:
    restore()
chk("A 认不出的类型不抛异常、也不发信号",
    calls["kill"] == [] and calls["killpg"] == [], calls)

# 截断这一档：`int(3.5)` 是 3，旧写法会把信号发给**另一个**进程；`True` 则是 1。
# 用「自己 pid 的小数」与 2.5 两种写法，不管本进程 pid 是几都量得准。
calls = install()
try:
    pc.hard_kill_pid(2.5)
    pc.hard_kill_pid(float(os.getpid()) + 0.5)
    pc.hard_kill_pid(True)
    pc.hard_kill_pid(float("nan"))
    pc.hard_kill_pid(float("inf"))
finally:
    restore()
chk("A 小数 / 布尔 / NaN / inf 的 pid 不被截断成别人（一个信号都不发）",
    calls["kill"] == [] and calls["killpg"] == [], calls)

# ── B. 目标与自己在同一进程组：只杀它一个，绝不 killpg ──────────────────
calls = install(pgid_map={7777: 4242, os.getpid(): 4242})
try:
    pc.hard_kill_pid(7777)
finally:
    restore()
chk("B 同一进程组时只 os.kill(pid, SIGKILL)，不用 killpg",
    calls["kill"] == [(7777, signal.SIGKILL)] and calls["killpg"] == [], calls)

# ── C. 正常情形（别组）：仍连子孙一起收 ────────────────────────────────
calls = install(pgid_map={7778: 8888, os.getpid(): 4242})
try:
    pc.hard_kill_pid(7778)
finally:
    restore()
chk("C 不同进程组时仍 killpg 目标那一组（该连子孙一起收的照旧）",
    calls["killpg"] == [(8888, signal.SIGKILL)] and calls["kill"] == [], calls)

# ── D. stop_backend：发信号之前先认人 ─────────────────────────────────
# D1 老写法的对照：认不出目标（不是归藏的后端）时旧写法照样 SIGTERM 出去。
with_is_ours(lambda pid, script="ui_server.py": False)
calls = install()
try:
    r1 = pc.stop_backend(6666, wait=0)
finally:
    restore()
chk("D1 认不出目标时一个信号都不发、回 False",
    r1 is False and calls["kill"] == [] and calls["killpg"] == [], (r1, calls))

# D2 是我们的后端：SIGTERM 照发，等不到（wait=0）再硬杀。
with_is_ours(lambda pid, script="ui_server.py": True)
calls = install(pgid_map={6667: 8888, os.getpid(): 4242})
try:
    pc.stop_backend(6667, wait=0)
finally:
    restore()
chk("D2 目标确实是我们的后端时：先 SIGTERM，等不到再 killpg 硬杀",
    calls["kill"][:1] == [(6667, signal.SIGTERM)]
    and calls["killpg"] == [(8888, signal.SIGKILL)], calls)

# D3 等的那几百毫秒里目标退了、PID 被分走：硬杀之前复核失败就不动手。
_state = {"n": 0}


def _fresh_then_gone(pid, script="ui_server.py"):
    _state["n"] += 1
    return _state["n"] == 1          # 第一次还认得，硬杀前那一次认不出


with_is_ours(_fresh_then_gone)
calls = install(pgid_map={6668: 8888, os.getpid(): 4242})
try:
    r3 = pc.stop_backend(6668, wait=0)
finally:
    restore()
chk("D3 硬杀之前复核失败（PID 已被分走）时不用 killpg 打陌生进程",
    r3 is False and calls["killpg"] == []
    and calls["kill"] == [(6668, signal.SIGTERM)], (r3, calls))

# ── E. 接线与源码形状 ────────────────────────────────────────────────
src = (ROOT / "platform_compat.py").read_text(encoding="utf-8")
chk("E1 stop_backend 发信号之前先过 is_our_backend",
    "if not is_our_backend(int(pid)):" in src)
chk("E2 hard_kill_pid 挡住 pid <= 0 与自己",
    "if p <= 0 or p == os.getpid():" in src)
chk("E3 同一进程组那一档退成 os.kill，不再 killpg",
    "if os.getpgid(p) == os.getpgid(os.getpid()):" in src)
chk("E4 take_over_port 仍走 stop_backend（这一层没被绕开）",
    "stop_backend(pid, wait=wait)" in src)
chk("E5 只放行真正的整数 pid（小数 / 布尔不被截断）",
    "if p != pid:" in src and "isinstance(pid, bool)" in src)

# ── F. 真跑一遍的对照：另起一组，活着才算过 ──────────────────────────
# 子进程用 start_new_session 自成一組，所以老写法那一下 `killpg(getpgid(0))`
# 只会打死它自己 —— 拿它当对照是安全的：改之前子进程必死，改之后它还活着。
if pc.IS_WIN:
    chk("F1 真跑 hard_kill_pid(0)/(自己)：Windows 走 taskkill，另测", True)
else:
    child = (
        "import os, sys\n"
        "sys.path.insert(0, %r)\n"
        "import platform_compat as pc\n"
        "pc.hard_kill_pid(0)\n"
        "pc.hard_kill_pid(-1)\n"
        "pc.hard_kill_pid(os.getpid())\n"
        "print('alive')\n" % str(ROOT)
    )
    proc = subprocess.run([sys.executable, "-c", child], capture_output=True,
                          text=True, timeout=30, start_new_session=True)
    chk("F1 真跑 hard_kill_pid(0)/(-1)/(自己) 的进程还活着（老写法会打死自己这一组）",
        proc.returncode == 0 and "alive" in proc.stdout,
        (proc.returncode, proc.stderr[-200:]))

print("\n杀进程护栏体检：%d 项通过，失败 %d 项" % (PASSED[0], len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
