"""把源码打成桌面 zip：只装代码与文档，绝不带上 cache/output/凭证。

除了挑文件，还做一遍内容体检：包里的每一行都不该出现别人的家目录、邮箱、手机号、
cookie 值或书架里的书名 —— 这些是「代码能跑」之外必须守住的边界。
"""
import datetime, pathlib, re, zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
INCLUDE = ["ui.html", "ui_server.py", "export_precise.py", "download_images.py",
           "platform_compat.py", "login.py", "shelf_add.py",
           "启动归藏.command", "启动归藏.bat",
           "README.md", "部署说明.md", "requirements.txt"]
EXTRA_DIRS = ["tools", "mcp", "skills"]
SKIP_SUFFIX = {".pyc"}
SKIP_PARTS = {"__pycache__", ".git", ".venv", "cache", "output"}

# 内容体检规则：模式 → 说明。全是通用形状，不写具体值，免得规则本身带信息。
PATTERNS = [
    (re.compile(r"/Users/[A-Za-z0-9._-]+|[A-Za-z]:\\Users\\|[A-Za-z]:\\[A-Za-z0-9._-]+\\", re.I),
     "写死的家目录绝对路径"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}"), "邮箱"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "手机号"),
    (re.compile(r"(wr_vid|wr_skey|wr_gid|api[_-]?key|authorization)\s*[:=]\s*[\"'][0-9a-zA-Z_\-]{8,}", re.I),
     "像是真凭证的赋值"),
    (re.compile(r"(?<!\d)\d{9,13}(?!\d)"), "像真实书号的长数字"),
]


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
            for pat, why in PATTERNS:
                if pat.search(line):
                    hits.append(f"{info.filename}:{no} → {why}：{line.strip()[:60]}")
    return hits


out = pathlib.Path.home() / f"Desktop/归藏-源码-{datetime.date.today():%Y%m%d}.zip"
n = 0
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for name in INCLUDE:
        f = ROOT / name
        if f.exists():
            z.write(f, f"归藏源码/{name}"); n += 1
    for d in EXTRA_DIRS:
        for f in sorted((ROOT / d).rglob("*")):
            if f.is_dir() or f.suffix in SKIP_SUFFIX or any(p in SKIP_PARTS for p in f.parts):
                continue
            z.write(f, f"归藏源码/{f.relative_to(ROOT)}"); n += 1

print(f"{out} · {n} 个文件 · {out.stat().st_size//1024} KB")
with zipfile.ZipFile(out) as z:
    leaks = [i.filename for i in z.infolist()
             if any(p in i.filename for p in ("cache/", "output/", "browser_profile",
                                              "config.json", "notes_index"))]
    print("夹带个人数据文件:", leaks or "无")
    for line in scan(z):
        print("  待核实", line)
