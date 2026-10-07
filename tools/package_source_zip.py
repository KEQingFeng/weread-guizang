"""把源码打成 zip：只装代码与文档，绝不带上 cache/output/凭证。

清单不再自己写第二份 —— 直接读 shell/build_macos.sh 里的 APP_FILES / APP_DIRS。
这个仓库栽过一次：新增后端模块忘了补进打包脚本，打出来的 .app 缺模块；也栽过第二次：
这里的 INCLUDE 还留着已经搬走的老路径，同时漏掉了 book_export.py 那一整批新模块。
一份清单两处写，就一定会分叉，所以只留一处。

除了挑文件，还做一遍内容体检：包里的每一行都不该出现别人的家目录、邮箱、手机号、
cookie 值或书架里的书名 —— 这些是「代码能跑」之外必须守住的边界。
"""
import datetime, pathlib, re, sys, zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUILD = ROOT / "shell" / "build_macos.sh"

# 打包脚本之外、源码包还要额外带上的东西（.app 用不到，但收到 zip 的人要用）。
EXTRA_FILES = ["启动归藏.command", "启动归藏.bat", "README.md"]
EXTRA_DIRS = ["shell", "tools", "tests", "docs"]

SKIP_SUFFIX = {".pyc"}
SKIP_PARTS = {"__pycache__", ".git", ".venv", "cache", "output", "dist", "build"}


def manifest():
    """从 build_macos.sh 里读出 APP_FILES / APP_DIRS 两个 bash 数组。"""
    text = BUILD.read_text(encoding="utf-8")
    out = {}
    for name in ("APP_FILES", "APP_DIRS"):
        m = re.search(rf'^{name}=\(\n(.*?)\)\n', text, re.M | re.S)
        if not m:
            sys.exit(f"读不到 {name}：打包脚本的结构变了，这里的解析也要跟着改")
        out[name] = [l.strip() for l in m.group(1).splitlines()
                     if l.strip() and not l.strip().startswith("#")]
    missing = [f for f in out["APP_FILES"] if not (ROOT / f).exists()]
    if missing:
        sys.exit("清单里的文件不在仓库里：" + "、".join(missing))
    # 反向护栏，判据同 build_macos.sh 里那一段：根目录冒出新 .py 而没进 APP_FILES，
    # 打出来的包就是「源码直接跑一切正常、装起来一开就哑」。这条放在这里，
    # 三个打包器（mac / 源码 zip / Windows zip）就都看得见，不用各自再写一份。
    listed = set(out["APP_FILES"])
    drift = sorted(p.name for p in ROOT.glob("*.py") if p.name not in listed)
    if drift:
        sys.exit("根目录这些 .py 没进 APP_FILES（运行期模块必须进包）：" + "、".join(drift))
    return out["APP_FILES"], out["APP_DIRS"]


# 内容体检规则：模式 → 说明。全是通用形状，不写具体值，免得规则本身带信息。
# 判据与 tests/check_privacy.py 保持一致：邮箱必须「@ 后是字母、结尾是字母 TLD」，
# 否则 marked@0.3.6、icon_512x512@2x.png 这些形状会被当成邮箱，每次打包刷一屏假警报，
# 到真泄漏那天就没人看了。
PATTERNS = [
    (re.compile(r"/Users/[A-Za-z0-9._-]+|[A-Za-z]:\\Users\\|[A-Za-z]:\\[A-Za-z0-9._-]+\\", re.I),
     "写死的家目录绝对路径"),
    (re.compile(r"[\w.+-]+@[A-Za-z][\w-]*(?:\.[\w-]+)*\.[A-Za-z]{2,}"), "邮箱"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "手机号"),
    (re.compile(r"(wr_vid|wr_skey|wr_gid|api[_-]?key|authorization)\s*[:=]\s*[\"'][0-9a-zA-Z_\-]{8,}", re.I),
     "像是真凭证的赋值"),
    (re.compile(r"(?<!\d)\d{9,13}(?!\d)"), "像真实书号的长数字"),
]
# 认下来的公开形状与占位示例；判据与 tests/check_privacy.py 的 ALLOW 对齐。
ALLOW = re.compile(r"git@github\.com|users\.noreply\.github\.com|example\.com|your-|xxx|"
                   r"@2x\.|<[^>]*>|localhost|127\.0\.0\.1")
# 结构性豁免：文件路径片段 → 免除的规则（"*" 表示全免），每条都要写清为什么。
WAIVERS = [
    ("tests/check_privacy.py", "*"),                  # 扫描器的规则里本来就写着这些形状
    ("tools/package_source_zip.py", "*"),             # 同上，本文件的 PATTERNS 自身
    # 三方原样 vendored 的库，不归我们清洗 —— 判据与 tests/check_privacy.py 的 WAIVERS
    # 保持一致（那边也是 vendor/ 全免）。里面本来就有上游作者的邮箱（fabric 的
    # package.json）和成串的长数字（压缩后的 js）。这里必须和门禁同一个口径：
    # 打包器喊狼、门禁不喊，喊久了就没人看了。
    ("vendor/", "*"),
]


def waived(who, name):
    for frag, only in WAIVERS:
        if frag in who and (only == "*" or only == name):
            return True
    return False


def scan(zf):
    """逐行扫包内文本，命中就报「文件:行号 → 类型 + 那一行的前 60 字」。"""
    hits = []
    for info in zf.infolist():
        if info.is_dir() or info.filename.endswith((".png", ".jpg", ".ico", ".woff")):
            continue
        try:
            text = zf.read(info.filename).decode("utf-8")
        except (UnicodeDecodeError, KeyError):
            continue
        for no, line in enumerate(text.splitlines(), 1):
            if ALLOW.search(line):
                continue
            for pat, why in PATTERNS:
                if not waived(info.filename, why) and pat.search(line):
                    hits.append(f"{info.filename}:{no} → {why}：{line.strip()[:60]}")
    return hits


def build(out=None):
    """打包，返回 (路径, 文件数)。out 不给就落桌面、文件名带当天日期。"""
    files, dirs = manifest()
    include = files + EXTRA_FILES
    extra_dirs = dirs + EXTRA_DIRS

    if out is None:
        out = pathlib.Path.home() / f"Desktop/归藏-源码-{datetime.date.today():%Y%m%d}.zip"
    out = pathlib.Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)   # 没有桌面目录的机器（Linux/CI）不该在这一步炸

    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in include:
            f = ROOT / name
            if f.exists():
                z.write(f, f"归藏源码/{name}"); n += 1
            elif name not in files:      # 外围文件（比如只做了 mac 的启动脚本）可以缺
                print("  跳过（没有这个文件）:", name)
            else:
                sys.exit(f"清单点名了 {name}，仓库里却没有 —— 先查是不是被误删了")
        for d in extra_dirs:
            for f in sorted((ROOT / d).rglob("*")):
                if f.is_dir() or f.suffix in SKIP_SUFFIX or any(p in SKIP_PARTS for p in f.parts):
                    continue
                z.write(f, f"归藏源码/{f.relative_to(ROOT)}"); n += 1
    return out, n


def report(out):
    """打完一体检：夹带了个人数据文件没有、包内文本有没有不该出现的形状。"""
    with zipfile.ZipFile(out) as z:
        leaks = [i.filename for i in z.infolist()
                 if any(p in i.filename for p in ("cache/", "output/", "browser_profile",
                                                  "config.json", "notes_index"))]
        print("夹带个人数据文件:", leaks or "无")
        for line in scan(z):
            print("  待核实", line)


def main():
    out, n = build()
    print(f"{out} · {n} 个文件 · {out.stat().st_size//1024} KB")
    report(out)


if __name__ == "__main__":
    main()
