# -*- coding: utf-8 -*-
"""门禁自身的接线体检：写好的 check_*.py 到底有没有真的挂进 run_all.sh。

为什么这条值得单独一个离线门禁
    `tests/run_all.sh` 是唯一的入口，一个套件只有被那条 `run "…" "$PY" tests/check_x.py`
    点过名才会跑。新加一条套件却忘了登记，是一类**没有任何症状**的坏：文件在仓库里、
    单独跑也绿，只有全量门禁永远不知道它存在 —— 之后它想守的那个坑再犯，谁也不会红。
    这与 check_package_list 守的「忘了进 APP_FILES，装起来一开就哑」是同一种病，
    只是换到了门禁自己身上：一个是「漏进包」，一个是「漏进门」。

两个方向一起守
    正向：`tests/check_*.py` / `tests/test_*.py` 每一个都得真被调起（手动 / 按需的
          极少数写在 MANUAL 里并注明理由，不允许靠注释里提一句就算数）。
    反向：run_all.sh 调起的每个 `tests/*.py` 都得在仓库里 —— 打错一个字母、或删了
          文件忘了删登记行时，`--fast` 根本走不到浏览器段，那种登记行会烂很久没人知道。

为什么解析要「认引号」
    run 行的名字、echo 的说明里会成串地提到 `tests/check_x.py`（本仓库的注释文化就是
    把前因后果写进套件名）。如果按子串扫全文，在话术里提一句就等于「登记过了」，这条
    门禁就彻底空转了。所以：注释整行不看；`run` 行上的路径算登记（名字与路径都在这一行）；
    其余行只看引号**外**的路径 —— echo 里那句「怎么手动跑」自然不算「自动跑」。

纯文件走查，不起服务、不联网、不开浏览器，归 --fast。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_ALL = os.path.join(ROOT, "tests", "run_all.sh")

# 只有手动 / 按需才跑的套件：必须是极少数，且在这里写清「为什么不能自动跑」。列进来的
# 仍要满足反向那条（文件得在）—— 豁免过期了会被 A2 点名。
MANUAL = {
    "tests/check_clip_live.py":
        "剪藏真机走查要一份真公众号链接表（GUIZANG_CLIP_LINKS），链接会过期，不入库",
}

SUITE_RE = re.compile(r"tests/[A-Za-z0-9_][A-Za-z0-9_./-]*\.py")

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:300]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def outside_quotes(line):
    """把一行里落在引号之外的片段拼起来（`\\"` 这种转义不当收尾）。

    引号内的内容不算「登记」——否则 run 的名字、echo 的说明里提一句就算接线，
    这条门禁等于空转。
    """
    out = []
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def registered_paths(text):
    """从 run_all.sh 的正文里挑出「真被调起」的 tests/*.py。"""
    out = set()
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("run "):
            # run 行上的路径就是登记（名字里出现路径的概率极低，且真出现也是同一行的
            # `run`，误认的代价远小于漏认）。
            out.update(SUITE_RE.findall(line))
        else:
            out.update(SUITE_RE.findall(outside_quotes(line)))
    return out


def unregistered_suites(suites, registered, manual=()):
    """哪些套件写了文件却没挂进 run_all.sh（豁免的除外）。"""
    skip = set(manual)
    return sorted(f for f in suites if f not in registered and f not in skip)


def missing_paths(registered, exists):
    """登记了、仓库里却没有的路径（打错字 / 删了文件忘删登记行）。"""
    return sorted(p for p in registered if not exists(p))


def suite_files():
    """要守的套件：tests/ 下的 check_*.py 与 test_*.py（helper 不在此列）。"""
    tdir = os.path.join(ROOT, "tests")
    out = []
    for name in sorted(os.listdir(tdir)):
        if name.endswith(".py") and (name.startswith("check_") or name.startswith("test_")):
            out.append("tests/" + name)
    return out


def exists(rel):
    return os.path.exists(os.path.join(ROOT, rel))


# ── A. 真仓库：两个方向都不许漂 ──────────────────────────────────
text = open(RUN_ALL, encoding="utf-8").read()
registered = registered_paths(text)
suites = suite_files()

A1 = unregistered_suites(suites, registered, MANUAL)
chk("每个 tests/check_*.py、tests/test_*.py 都真被 run_all.sh 调起（漏挂 = 门禁永远不跑）",
    not A1, A1)

A2 = [f for f in MANUAL if not exists(f)]
chk("MANUAL 里的豁免套件都还在（豁免过期了要删掉，别留死名字）", not A2, A2)

A3 = missing_paths(registered, exists)
chk("run_all.sh 调起的每个 tests/*.py 都在仓库里（打错字 / 删文件忘删登记行）",
    not A3, A3)

# 解析别退化成空表：空表在上面的 A1 里会全红，但先挡一道，错误信息更直白。
chk("登记解析没退化成空表（>100 条且含一条已知套件）",
    len(registered) > 100 and "tests/check_package_list.py" in registered,
    (len(registered),) + tuple(sorted(registered)[:5]))

chk("这条门禁自己也挂上了（自指）",
    "tests/check_gate_wiring.py" in registered, sorted(registered)[:5])


# ── B. 对照：检测函数真的会红、也会安静 ──────────────────────────
def _fake_registered(lines):
    return registered_paths("\n".join(lines))


# B1：一个 run 行正常登记 → 不报。
_fixture = ['run "假套件" "$PY" tests/check_fake_ok.py']
chk("对照：run 行登记了的套件不报",
    unregistered_suites(["tests/check_fake_ok.py"], _fake_registered(_fixture)) == [],
    _fake_registered(_fixture))

# B2：整行被注释掉 → 等于没挂，必须报（这是「忘了登记」最常见的样子）。
_fixture = ['# run "假套件" "$PY" tests/check_fake_off.py']
chk("对照：被注释掉的 run 行不算登记，会点名",
    unregistered_suites(["tests/check_fake_off.py"], _fake_registered(_fixture))
    == ["tests/check_fake_off.py"],
    _fake_registered(_fixture))

# B3：只在 echo 的说明里提了一句（引号内）→ 不算登记，必须报。
#     这条钉住「认引号」的分寸：本仓库的套件名就是一口气的长说明，最容易滑成子串扫。
_fixture = ['echo "手动跑：\\"$PY\\" tests/check_fake_echo.py"']
chk("对照：只在 echo 说明里提过的套件不算登记（引号内不算数）",
    unregistered_suites(["tests/check_fake_echo.py"], _fake_registered(_fixture))
    == ["tests/check_fake_echo.py"],
    _fake_registered(_fixture))

# B4：豁免的不会报。
chk("对照：MANUAL 里的套件不报",
    unregistered_suites(["tests/check_fake_manual.py"], set(),
                        {"tests/check_fake_manual.py"}) == [])

# B5：登记了一个不存在的路径 → 反向要报。
chk("对照：登记了不存在的路径会点名",
    missing_paths({"tests/check_ghost.py"}, lambda _r: False) == ["tests/check_ghost.py"])

# B6：干净的输入两边都安静。
chk("对照：干净输入两个方向都不报",
    unregistered_suites(["tests/check_a.py"], {"tests/check_a.py"}) == []
    and missing_paths({"tests/check_a.py"}, lambda _r: True) == [])

print()
if FAIL:
    print("门禁接线：失败 %d 项" % len(FAIL))
    for n in FAIL:
        print("  ✗ " + n)
    sys.exit(1)
print("门禁接线：通过 %d 项，失败 0 项" % PASSED)
