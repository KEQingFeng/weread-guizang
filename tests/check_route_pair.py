# -*- coding: utf-8 -*-
"""前后端对表：界面与适配器打的每一个 /api/... ，后端路由表里都得真有这么一条。

为什么单开这一份：2026-10-04 那三条「画板打不开 / 导图打不开 / flomo 导入没反应」的工单，
后端代码一条都不缺 —— 缺的是「前后端是不是同一份」。界面每次请求都从磁盘现读（永远是新的），
路由表却是进程起来那一刻装进内存的（可能是几周前那版）。于是新功能一律 HTTP 404，
而界面那句笼统的 catch 把它报成「本机服务没在跑」，方向完全指错了。

这一条不联网、不开浏览器，静态就把同类事故挡在门外：
1) 版本号成对 —— ui_server.py 的 VERSION 必须等于 ui.html 里的 GUIZANG_PAGE，
   不等就是「界面写给 A 版后端看、后端自己是 B 版」，横幅永远判不对。
2) 路由对表 —— 界面和 MCP 适配器里出现的每个 /api/... 都必须落在后端的路由里
   （startswith 那种算前缀命中）。新增功能只写了前端没写后端，在这里就红。
3) 谎话不许回来 —— 「本机服务没在跑」只许出现在注释里（解释为什么不再这么说），
   不许再是任何一句发给用户的话。三种不同的故障（没起 / 起了但旧 / 起了但报错）
   共用一句话，用户就会去重启一个本来就好的服务。
4) 一把尺子 —— 四个入口（.command、.bat、MCP 适配器、Swift 壳）都得走同一套指纹判断，
   少一个就有一条路还会「看见端口有人听就当是我」。
5) 后端得报指纹 —— /api/state 里必须带 code，否则上面几条判不了。

判定是硬的：查出不匹配就是非零退出。真删了某个接口，就把界面里那处调用一起删掉，
别在这份脚本里加白名单把门放开。

跑法：.venv/bin/python tests/check_route_pair.py
"""
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
UI = REPO / "ui.html"
SRV = REPO / "ui_server.py"
MJS = REPO / "mcp" / "guizang-mcp.mjs"
SH = REPO / "启动归藏.command"
BAT = REPO / "启动归藏.bat"
SWIFT = REPO / "shell" / "main.swift"

# 界面 / 适配器里能出现的接口写法：字符串里那一段 /api/xxx（后面可能紧跟 ?book= 或模板变量）
FRONT_CALL = re.compile(r"/api/[A-Za-z0-9_\-/]+")
# 后端认路由的两种写法：整串相等，或按前缀 startswith
BACK_EXACT = re.compile(r'\bpath\s*==\s*"(/api/[^"]+)"')
BACK_PREFIX = re.compile(r'path\.startswith\("(/api/[^"]+)"\)')

# 那句谎话：三种故障共用一句「服务没在跑」。注释里可以留着解释为什么不用它，
# 但字符串里一处都不许有。
LIE = "本机服务没在跑"


def read(p):
    return p.read_text(encoding="utf-8") if p.exists() else ""


def backend_routes(text):
    exact, prefix = set(), set()
    for m in BACK_EXACT.finditer(text):
        exact.add(m.group(1).rstrip("/"))
    for m in BACK_PREFIX.finditer(text):
        prefix.add(m.group(1).rstrip("/"))
    return exact, prefix


def front_paths(text):
    """界面里打出去的接口，去掉尾部顺手带上的标点和模板变量起点。"""
    out = set()
    for m in FRONT_CALL.finditer(text):
        path = m.group(0).rstrip("/")
        if path == "/api":
            continue
        out.add(path)
    return out


def hit(path, exact, prefix):
    if path in exact:
        return True
    return any(path == p or path.startswith(p) for p in prefix)


def comment_only_lines(text, needle):
    """返回「这句话出现、但不像注释」的行号。

    归藏里的注释三种写法：整行 // 、/* 块里的 * 开头 、行尾 // 。
    块注释里还会嵌代码示例，所以按 /* … */ 的区间来判，比只看行首稳。
    """
    spans = [(m.start(), m.end()) for m in re.finditer(r"/\*.*?\*/", text, re.S)]
    bad = []
    pos = 0
    for no, line in enumerate(text.splitlines(), 1):
        start = pos
        pos += len(line) + 1
        if needle not in line:
            continue
        in_block = any(a <= start < b for a, b in spans)
        s = line.strip()
        if in_block or s.startswith("//") or s.startswith("*"):
            continue
        # 行尾挂着一句注释：注释从 // 之后开始，前半截还有这句话就是字符串
        cut = line.find("//")
        if cut >= 0 and needle not in line[:cut]:
            continue
        bad.append(no)
    return bad


def main():
    fail = 0
    ui, srv, mjs = read(UI), read(SRV), read(MJS)
    if not ui or not srv:
        print("FAIL  ui.html 或 ui_server.py 读不到")
        return 1

    # ── 1 版本号成对 ──────────────────────────────────
    ver = re.search(r'^VERSION\s*=\s*"([^"]+)"', srv, re.M)
    page = re.search(r'const\s+GUIZANG_PAGE\s*=\s*"([^"]+)"', ui)
    if not ver or not page:
        print("FAIL  读不到版本号：ui_server.py 要有 VERSION，ui.html 要有 GUIZANG_PAGE")
        fail = 1
    elif ver.group(1) != page.group(1):
        print("FAIL  版本号不成对：后端 %s，界面写给 %s 的后端看"
              % (ver.group(1), page.group(1)))
        print("      界面对不上就永远弹「换新后端」那条横幅，或者反过来把旧的当成新的")
        fail = 1
    else:
        print("PASS  版本号成对：界面与后端同为 %s" % ver.group(1))

    # ── 2 路由对表 ────────────────────────────────────
    exact, prefix = backend_routes(srv)
    if not exact:
        print("FAIL  后端路由一条都没提出来（多半是 ui_server.py 换了写法，改这份脚本的正则）")
        return 1
    orphan = []
    for name, text in (("ui.html", ui), ("guizang-mcp.mjs", mjs)):
        for path in sorted(front_paths(text)):
            if not hit(path, exact, prefix):
                orphan.append((name, path))
    if orphan:
        fail = 1
        print("FAIL  %d 个接口后端没有这条路由（打过去就是 HTTP 404，"
              "界面会把它说成「后端没启动」）：" % len(orphan))
        for name, path in orphan:
            print("        %s → %s" % (name, path))
    else:
        print("PASS  路由对表：界面与适配器打的 %d 个接口，后端 %d 条精确 + %d 条前缀全接得住"
              % (len(front_paths(ui) | front_paths(mjs)), len(exact), len(prefix)))

    # ── 3 那句谎话只许待在注释里 ──────────────────────
    bad = comment_only_lines(ui, LIE)
    if bad:
        fail = 1
        print("FAIL  ui.html 还有 %d 处把三种故障说成一句话（ui.html:%s）"
              % (len(bad), "、".join(str(n) for n in bad)))
        print("      该走 gzApi：它分得清没起 / 起了但旧 / 起了但报错，各说各的话")
    else:
        print("PASS  「%s」只出现在注释里，界面不再拿它盖住别的原因" % LIE)

    # ── 4 四个入口用同一把尺子 ─────────────────────────
    need = [
        (SH, "platform_compat.py verdict", ".command 启动前问一句端口上是谁"),
        (SH, "platform_compat.py ready", ".command 等新后端起来时认指纹"),
        (BAT, "platform_compat.py verdict", ".bat 启动前问一句端口上是谁"),
        (BAT, "platform_compat.py ready", ".bat 等新后端起来时认指纹"),
        (MJS, "function diskCode", "MCP 适配器自己算磁盘指纹"),
        (MJS, "--identity", "MCP 适配器留了个给门禁对的自检入口"),
        (SWIFT, "portVerdict", "Swift 壳起后端前问一句端口上是谁"),
        (SRV, '"code": CODE_HASH', "/api/state 把指纹报出去"),
        (SRV, "--takeover", "后端能把自家旧进程的端口接过来"),
        (SRV, "--handoff", "一键换班时接班人等老人把端口放下"),
    ]
    missing = [(p.name, why) for p, needle, why in need if needle not in read(p)]
    if missing:
        fail = 1
        print("FAIL  %d 处「认指纹」的活儿没接上：" % len(missing))
        for name, why in missing:
            print("        %s —— %s" % (name, why))
    else:
        print("PASS  一把尺子：两个启动脚本、MCP 适配器、Swift 壳和后端本身，%d 处都在" % len(need))

    print(("FAIL  前后端对表没过") if fail else ("PASS  前后端对表：版本号、路由、话术、四个入口都对得上"))
    return fail


if __name__ == "__main__":
    sys.exit(main())
