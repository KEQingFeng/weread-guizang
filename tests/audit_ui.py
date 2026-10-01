"""控件体检：重复 id、引用了不存在的 id、定义了却没人绑定的 id。

默认查本仓库的 ui.html，路径也可以当第一个参数传。
"""
import collections
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import selftest  # noqa: E402

target = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else selftest.REPO / "ui.html"
src = target.read_text(encoding="utf-8")

html_ids = re.findall(r'\bid="([^"]+)"', src)
js_ids = re.findall(r"""getElementById\(['"]([^'"]+)['"]\)""", src)
dollar_ids = re.findall(r"""\$\(['"]#([A-Za-z0-9_-]+)['"]\)""", src)
qsa = re.findall(r"""querySelector(?:All)?\(['"]#([A-Za-z0-9_-]+)""", src)

referenced = set(js_ids) | set(dollar_ids) | set(qsa)
defined = set(html_ids) | set(re.findall(r"""\.id\s*=\s*['"]([^'"]+)['"]""", src))

dupes = [k for k, v in collections.Counter(html_ids).items() if v > 1]
missing = sorted(r for r in referenced if r not in defined)
orphan = sorted(d for d in defined if d not in referenced)

print("重复 id:", dupes or "无")
print("引用但不存在的 id:", missing or "无")
print("定义了但没被 $()/querySelector 引用的 id:", orphan or "无")

# 动态按钮必须有点击处理：createElement('button') 之后就近找 onclick/addEventListener
btn_defs = [m.start() for m in re.finditer(r"createElement\(['\"]button['\"]\)", src)]
loose = []
for pos in btn_defs:
    window = src[pos:pos + 700]
    if not re.search(r"\.onclick|addEventListener\(['\"]click", window):
        loose.append(src[:pos].count("\n") + 1)
print("创建后 700 字符内没绑定点击的 button 行号:", loose or "无")
