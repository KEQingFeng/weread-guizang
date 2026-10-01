#!/usr/bin/env python3
"""把 ui.html 里的内联 <script> 抽出来交给 node --check：语法错在浏览器之前先撞出来。

默认查本仓库的 ui.html，也可以把文件路径当第一个参数传进来。
"""
import pathlib
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import selftest  # noqa: E402

target = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else selftest.REPO / "ui.html"
src = target.read_text(encoding="utf-8")
blocks = re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', src, re.S)
print("被检文件：%s" % target.name)
print("内联脚本块：%d" % len(blocks))
bad = 0
with tempfile.TemporaryDirectory(prefix="guizang-js-") as tmp:
    for i, b in enumerate(blocks):
        if not b.strip():
            continue
        p = pathlib.Path(tmp) / ("inline_%d.js" % i)
        p.write_text(b, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True)
        if r.returncode:
            bad += 1
            print("块 %d 语法错：%s" % (i, r.stderr.strip().splitlines()[-3:]))
        else:
            print("块 %d ok（%d 行）" % (i, b.count(chr(10))))
print("失败 %d" % bad)
sys.exit(1 if bad else 0)
