# -*- coding: utf-8 -*-
"""内联脚本体检：有没有「调一个从没写出来的函数」—— 按下去没反应那一类事故，静态就能抓。

为什么单开这一份：连着几轮都犯同一个错 —— 重构时把一个函数名留在了调用处，却没留下定义
（这一轮就撞上一次：勾选那一列点下去只抛 ReferenceError，批量那排按钮从来没露过面）。
浏览器里的表现是「界面看着一切正常，就是点了没反应」，而真机走查只能覆盖它走过的路径。
所以这条不联网、不开浏览器：把内联 <script> 里的注释清掉，拿「所有被当函数调用的名字」
减「所有被定义过的名字」减「浏览器自带的」减「CSS 函数名」，剩下的就是会抛错的地方。

判定是硬的：查出未定义就是非零退出。真加了新的浏览器 API 就把它加进 BUILTIN，
别把这条门禁关掉。误报也只许往里加名字、不许整个跳过。

第二条规则管「拼错但读着不报错」的 DOM 属性：el.readonly / el.contenteditable 这种小写写法
在 DOM 上根本不存在，读它得到 undefined，赋值则只是往对象上挂个没人看的字段 —— 界面看着正常、
功能静默失效，真机走查还容易跟着一起读错（本轮就同时踩了实现和自查两份）。这类一律算 FAIL。

跑法：.venv/bin/python tests/check_js_refs.py [文件…]
      默认查 ui.html 与 shell/onboarding.html
"""
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import selftest  # noqa: E402

IDENT = r"[A-Za-z_$][\w$]*"

# 浏览器 / 全局自带的，外加几个关键字（await (…) / if (…) 会被当成一次调用）
BUILTIN = set("""
fetch parseInt parseFloat isNaN isFinite encodeURIComponent decodeURIComponent
encodeURI decodeURI setTimeout setInterval clearTimeout clearInterval
requestAnimationFrame cancelAnimationFrame structuredClone queueMicrotask import require
JSON Math Object Array String Number Boolean Date RegExp Promise Proxy Reflect Symbol BigInt
Error TypeError RangeError SyntaxError ReferenceError EvalError URIError AggregateError
console document window globalThis self location history navigator
localStorage sessionStorage indexedDB performance getComputedStyle
URL URLSearchParams Blob File FileReader FormData Headers Response Request AbortError
Intl CSS IntersectionObserver MutationObserver ResizeObserver Image Audio Option Worker
SharedWorker Event CustomEvent ClipboardItem Crypto
matchMedia postMessage alert confirm prompt open close scrollTo scrollBy screen
if for while switch catch return typeof instanceof in of void delete throw
case default else async await function class const let var do try finally break continue yield this super
""".split())

# 内联样式串里的函数名（'transform: translate3d(…)'、'cubic-bezier(…)' 这种）：
# 它们是 CSS 文本，不是 JS 调用。列在这儿而不是去猜字符串边界 —— 手搓的字符串扫描
# 一旦被一个正则里的引号带偏，会吞掉上千行真代码，反而把该报的漏掉。
CSS_FN = set("""
rgba rgb hsl hsla oklch color translate translate3d translateX translateY scaleX scaleY scale
rotate rotateX rotateY skew matrix matrix3d cubic-bezier bezier repeat minmax min max clamp
calc attr url counter blur brightness contrast saturate opacity crossfade path rect steps
linear-gradient radial-gradient conic-gradient symbol format local var env
""".split())


def strip_comments(code):
    """只清注释，不动字符串。

    为什么不清字符串：模板字面量里嵌着 HTML 和 CSS，手搓的引号配对一旦被
    「正则字面量里的引号」或「注释里的半角撇号」带偏，就会把成百行真代码当字符串删掉 ——
    实测过一版，定义全被吞，误报四十多条。只清注释的话，最坏情况是 http:// 后面少看几行，
    方向是「少报」而不是「多报」，可以接受。
    """
    lines = [l.split("//")[0] for l in code.split("\n")]
    return re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"),
                  "\n".join(lines), flags=re.S)


def defined_names(js):
    """这个脚本里所有「有名字的东西」：函数声明、变量、解构、形参、单参箭头、对象方法。"""
    names = set(re.findall(r"\bfunction\s*\*?\s*(" + IDENT + r")", js))
    names |= set(re.findall(r"\b(?:const|let|var)\s+(" + IDENT + r")", js))
    for grp in re.findall(r"\b(?:const|let|var)\s*(\[[^\]]*\]|\{[^}]*\})", js):
        for part in re.split(r"[,\[\]{}]", grp):
            part = part.split(":")[-1].split("=")[0].strip()
            if re.fullmatch(IDENT, part or ""):
                names.add(part)
    for m in re.finditer(r"\bfunction\s*[\w$]*\s*\(([^)]*)\)", js):
        for part in m.group(1).split(","):
            part = part.split("=")[0].strip()
            if re.fullmatch(IDENT, part or ""):
                names.add(part)
    for m in re.finditer(r"\(([^()]*)\)\s*=>", js):        # (a, b) => …
        for part in m.group(1).split(","):
            part = part.split("=")[0].strip()
            if re.fullmatch(IDENT, part or ""):
                names.add(part)
    for m in re.finditer(r"(?<![\w$.])(" + IDENT + r")\s*=>", js):   # x => …（没有括号那个形）
        names.add(m.group(1))
    # 对象字面量里的方法：name(args) { … }。只认带方法体的这种 ——
    # 光按「行首的 name(」当定义会把单独一行的调用也算成定义，那条 bug 就漏了。
    names |= set(re.findall(r"^\s*(?:async\s+)?(" + IDENT + r")\s*\([^)\n]*\)\s*\{", js, re.M))
    return names


def first_call_line(src, name):
    """回一条能跳的 ui.html 行号：在原文里找第一处「不是在注释里」的该名字被当函数用的地方。"""
    pat = re.compile(r"(?<![\w$.])" + re.escape(name) + r"\s*\(")
    for i, line in enumerate(src.split("\n"), 1):
        body = line.split("//")[0]
        if pat.search(body):
            return i
    return 0


# DOM 上「属性名」和「标签上那个名字」不一样的一批：读 el.readonly 不会报错，它只是 undefined。
# 于是 el.readonly = false 解不开只读（真栽过一次：所有要人填的弹层都打不进字，
# 而自查读的是同一个小写名，永远「通过」）。这类拼错静默失效，只能靠静态拦。
MISSING_PROPS = """
readonly contenteditable innertext innerhtml outertext scrolltop scrollleft
offsetheight offsetwidth tabindex maxlength rowspan colspan innerwidth innerheight
documentelement addeventlistener removeeventlistener queryselectorall setattribute
getattribute classname tagname nodename parenode childnodes
""".split()


def bad_props(js):
    """找「小写形式的 DOM 属性」：el.readonly 这种读不报错、永远是 undefined 的写法。"""
    hits = []
    for name in MISSING_PROPS:
        pat = re.compile(r"\." + name + r"\b\s*(?:[=;,)\[\]!&|+\-]|$)")
        for i, line in enumerate(js.split("\n"), 1):
            body = line.split("//")[0]
            if pat.search(body):
                hits.append((name, body.strip()[:90], i))
    return hits


def line_in_src(path, snippet):
    """把内联脚本里的行号换算回文件里的行号，方便直接点过去。"""
    src = path.read_text(encoding="utf-8")
    token = snippet.split("=")[0].strip()
    for i, line in enumerate(src.split("\n"), 1):
        if token and token in line:
            return i
    return 0


def check(path):
    src = path.read_text(encoding="utf-8")
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", src, re.S)
    if not blocks:
        return {}
    js = "\n".join(blocks)
    known = defined_names(js)
    calls = set(re.findall(r"(?<![\w$.])([a-z][\w$]{2,})\s*\(", strip_comments(js)))
    bad = {}
    for name in calls:
        if name in known or name in BUILTIN or name in CSS_FN:
            continue
        bad[name] = first_call_line(src, name)
    for name, snippet, _i in bad_props(strip_comments(js)):
        bad[name + "（小写 DOM 属性，恒为 undefined）"] = line_in_src(path, snippet)
    return bad


targets = [pathlib.Path(a) for a in sys.argv[1:]] or [selftest.REPO / "ui.html",
                                                      selftest.REPO / "shell" / "onboarding.html"]
fail = 0
for t in targets:
    if not t.exists():
        print(f"SKIP  {t.name}：文件不在")
        continue
    bad = check(t)
    if bad:
        fail = 1
        print(f"FAIL  {t.name}：{len(bad)} 处「用了却没写出来的东西」—— 按下去没反应或静默失效那一类")
        for name, line in sorted(bad.items(), key=lambda kv: (kv[1], kv[0])):
            print(f"        {name}  {t.name}:{line}")
    else:
        print(f"PASS  {t.name}：内联脚本没有「调了却没定义」的函数，也没写小写形式的 DOM 属性")
sys.exit(fail)
