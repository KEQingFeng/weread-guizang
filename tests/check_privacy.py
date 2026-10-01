"""发版前的安全体检：源码里不许留个人信息与凭证。只读，不改文件。

扫的范围就是「会被推上 GitHub 的那批文件」：优先问 git 要（已跟踪 + 未跟踪但没被
.gitignore 挡掉的），拿不到 git 才退回遍历工作树。这样 output/、cache/、dist/
这些用户数据天然不进扫描，也不会误报成「源码里泄漏了书里的邮箱」。

命中不等于一定要改：确实是公开信息或第三方原样的部分，写进 WAIVERS 并留下理由。
豁免是显式声明，不是把规则调松 —— 新增一条必须同时说明为什么。
"""
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import selftest  # noqa: E402

# 默认扫本仓库；想扫别的目录当第一个参数传进来。
ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else str(selftest.REPO))
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", "dist", "安装包",
             "cache", ".cache", "data", "books", "output", ".playwright"}
EXTS = {".py", ".js", ".mjs", ".html", ".css", ".md", ".json", ".sh", ".command",
        ".swift", ".txt", ".yaml", ".yml", ".toml", ".plist"}

RULES = [
    ("本机用户名路径", re.compile(r"/Users/[A-Za-z0-9_.-]+|C:\\\\Users\\\\|/home/[a-z0-9_.-]+")),
    ("GitHub 身份", re.compile(r"KEQingFeng|129739497")),
    # 域名首字符必须是字母，否则 icon_16x16@2x.png 这种图标尺寸名会被当成邮箱。
    ("邮箱", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z][A-Za-z0-9.-]*\.[A-Za-z]{2,}")),
    ("微信读书凭证值", re.compile(r"wr_(?:vid|skey)\s*[=:]\s*['\"]?[0-9A-Za-z_-]{8,}")),
    ("API Key 形态", re.compile(r"(?:sk|pk|key|token|secret)[-_][0-9A-Za-z]{16,}")),
    ("Bearer 令牌", re.compile(r"[Bb]earer\s+[0-9A-Za-z._-]{16,}")),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("身份证", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("私钥块", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("网盘口令字段", re.compile(r"(?:password|passwd|pwd)\s*[=:]\s*['\"][^'\"]{4,}['\"]")),
]
# 文档与注释里出现「wr_skey」「password」这类字段名是必要的说明，只有配上「像真值的长度」才算泄漏。
ALLOW = re.compile(r"example\.com|@users\.noreply\.github\.com|git@github\.com|your-|xxx|<[^>]*>|127\.0\.0\.1|localhost")

# 显式豁免：(路径前缀, 规则名)，'*' 表示所有规则。每条都必须是「看了就知道为什么」的公开信息。
WAIVERS = [
    # 三语 README 的 clone 地址 = 本仓库自己的公开地址，读者就是要用它。
    ("README", "GitHub 身份"),
    # 这份体检表本身写着这些特征的匹配模式，不可能不匹配自己。
    ("tests/check_privacy.py", "*"),
    # 第三方原样 vendored 的库（markdown-it），不归我们清洗。
    ("vendor/", "*"),
]


def waived(rel, rule):
    for prefix, name in WAIVERS:
        if (rel == prefix or rel.startswith(prefix)) and name in (rule, "*"):
            return True
    return False


def files():
    """优先用 git 报「会进版本库的文件」，这样扫描范围 = 推送范围。"""
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                             cwd=str(ROOT), capture_output=True, text=True, timeout=20)
        if out.returncode == 0:
            names = [l for l in out.stdout.splitlines() if l.strip()]
            return [(Path(ROOT) / n, n.replace(os.sep, "/")) for n in names
                    if Path(n).suffix.lower() in EXTS]
    except Exception:
        pass
    got = []
    for dirpath, dirnames, names in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".git")]
        for n in names:
            if Path(n).suffix.lower() in EXTS:
                p = Path(dirpath) / n
                got.append((p, str(p.relative_to(ROOT)).replace(os.sep, "/")))
    return sorted(got, key=lambda x: x[1])


hits = {}
total = 0
for f, rel in files():
    try:
        text = f.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue
    total += 1
    for lineno, line in enumerate(text.splitlines(), 1):
        for name, pat in RULES:
            m = pat.search(line)
            if m and not ALLOW.search(line) and not waived(rel, name):
                hits.setdefault(name, []).append(f"{rel}:{lineno}  {line.strip()[:110]}")

print(f"扫描 {total} 个源文件（范围＝会被推送的文件）")
if not hits:
    print("干净：没有个人信息、凭证或本机路径泄漏")
    sys.exit(0)
for name, rows in hits.items():
    print(f"\n[{name}] {len(rows)} 处")
    for r in rows:
        print("   " + r)
print("\n要么删掉/换成占位，要么在 WAIVERS 里写明为什么这是公开信息。")
sys.exit(1)
