# -*- coding: utf-8 -*-
"""取书引擎自己那三个「读章节」的口：野文件名 / 坏字节不许带走整趟取书。

为什么单独一份
    `export_precise.py` 是取书引擎，它有两处**每一轮会话都要问**的读章口，加上收尾
    那次合并：

      get_last_chapter_title(md_dir)  → 「上次读到哪一章」，main() 每轮开头问一次
      load_used_titles(md_dir)        → 「哪些章已经写出来了」，切章去重与收尾核对用
      merge_chapters(md_dir, …)       → 把各章拼成 output/<书名>.md 那份整本

    三处原来都是裸 `open(..., encoding="utf-8")`，而 chapters/ 是用户能直接编辑的
    目录（手改、另存、别的工具写回都会落进来）。实测两种脏输入各是一种坏法：

      · **野文件名**：随手放的 `notes.md`、旧版本备份改名成的 `第3章.md` 排在数字章
        后面（字母比数字大），`int(files[-1].replace(".md",""))` 抛
        `ValueError: invalid literal for int() with base 10: 'notes'`。
        main() 里这一句在**还没打开浏览器**时就跑，于是整趟取书被一句英文报错结束。
      · **坏字节**：一章被另存成 GBK、或写到一半断电，裸 `open` 抛
        UnicodeDecodeError。load_used_titles 在每轮切章前跑 → 「这本书取不动」；
        merge_chapters 在全书取完的最后一秒跑 → 「书明明取完了，合并稿没出来」。
        界面取正文 / EPUB / PDF 那几条路早就换成了 `book_export.read_chapter_file`
        这把尺子（坏字节换成替代符、读不到才回 None），引擎自己这三处漏了。

这一条钉三件事，全部离线（一个请求不发、不开浏览器）：

  A 真跑：三个口对「正常目录」的行为一个字不改（标题 / 编号 / 集合 / 合并稿字节）；
  B 真跑：野文件名不再让「上次读到哪」这个口抛，坏字节只换成替代符、正文其余部分
    与别的章照旧，合并稿照样落得下；
  C 接线：三处都走 `book_export.read_chapter_file` 这同一把尺子，不再各写一个
    裸 open —— 谁把哪一处改回去，这里就红。

引擎 import 时就要 playwright。本套件只打纯函数、不碰浏览器，所以没装 playwright
时用占位模块顶住那一个 import（**真装了就用真的**），并把这句说明打出来 —— 不把
「没测」装成「通过」。要 node 的那几条无关，这条只用 Python。

跑法：.venv/bin/python tests/check_engine_chapter_read.py
"""
import inspect
import os
import pathlib
import shutil
import sys
import tempfile
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 先把数据目录指进临时沙盒：引擎 import 时会按 GUIZANG_DATA / GUIZANG_OUTPUT 推路径，
# 设晚了就可能摸到用户真实的 output/。
SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-engine-chapters-"))
os.environ["GUIZANG_DATA"] = str(SB / "cache")
os.environ["GUIZANG_BOOKS"] = str(SB / "books")
os.environ["GUIZANG_OUTPUT"] = str(SB / "output")

FAIL = []
PASSED = [0]
NOTES = []


def chk(name, cond, extra=""):
    if cond:
        PASSED[0] += 1
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:300]))
    if not cond:
        FAIL.append(name)


def load_engine():
    """import export_precise；没装 playwright 时用占位模块顶住那一个 import。"""
    try:
        import export_precise as E          # noqa: E402
        return E, "real"
    except Exception as first:
        if "playwright" not in str(first):
            print("import export_precise 失败（不是 playwright 的事）：%r" % (first,))
            return None, "broken"
        pw = types.ModuleType("playwright")
        aa = types.ModuleType("playwright.async_api")
        aa.async_playwright = lambda *a, **k: None
        pw.async_api = aa
        sys.modules.setdefault("playwright", pw)
        sys.modules.setdefault("playwright.async_api", aa)
        try:
            import export_precise as E      # noqa: E402
        except Exception as e:
            print("占位 playwright 之后仍起不来：%r" % (e,))
            return None, "broken"
        return E, "stub"


def write(path, text, encoding="utf-8"):
    with open(path, "w", encoding=encoding) as f:
        f.write(text)


def write_raw(path, data):
    with open(path, "wb") as f:
        f.write(data)


def fresh(name):
    d = SB / name
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return d


E, HOW = load_engine()
if HOW == "stub":
    NOTES.append("没装 playwright，纯函数部分用占位模块顶住 import（真装了会用真的）")
elif HOW == "broken":
    print("\n取书引擎读章口：引擎起不来，这一条不算通过")
    sys.exit(1)

# ── C 接线：三处都走同一把尺子 ──────────────────────────────────────
import book_export as BX  # noqa: E402


def uses_ruler(fn):
    return "book_export.read_chapter_file(" in inspect.getsource(fn)


chk("接线：get_last_chapter_title 走 book_export.read_chapter_file",
    uses_ruler(E.get_last_chapter_title))
chk("接线：load_used_titles 走 book_export.read_chapter_file",
    uses_ruler(E.load_used_titles))
chk("接线：merge_chapters 走 book_export.read_chapter_file",
    uses_ruler(E.merge_chapters))
chk("接线：那把尺子确实按「认不出的字节换成替代符」读（errors=replace）",
    'errors="replace"' in inspect.getsource(BX.read_chapter_file))

# ── A 正常目录：口径一个字不改 ──────────────────────────────────────
d = fresh("normal")
write(d / "0001.md", "# 第一章\n正文一\n")
write(d / "0002.md", "# 第二章\n正文二\n")

chk("正常：最后一章报回标题与编号", E.get_last_chapter_title(d) == ("第二章", 2),
    E.get_last_chapter_title(d))
chk("正常：已写出标题集合一个不少",
    E.load_used_titles(d) == {"第一章", "第二章"}, E.load_used_titles(d))
chk("正常：目录不存在回 (None, 0) 而不是抛",
    E.get_last_chapter_title(SB / "nope") == (None, 0))
chk("正常：空目录回 (None, 0) 而不是抛",
    E.get_last_chapter_title(fresh("empty")) == (None, 0))
chk("正常：空目录已写出集合是空的", E.load_used_titles(fresh("empty2")) == set())

# ── B1 野文件名：忽略，不许抛 ───────────────────────────────────────
d = fresh("stray")
write(d / "0001.md", "# 第一章\n正文一\n")
write(d / "0002.md", "# 第二章\n正文二\n")
write(d / "notes.md", "随手记的东西，不是章节\n")
write(d / "第3章.md", "# 第三章（手工改名，不是引擎写的编号）\n")
write(d / ".hidden.md", "# 隐藏文件\n")
write(d / "0003.md.bak", "# 这不是 .md\n")

got = None
try:
    got = E.get_last_chapter_title(d)
except Exception as e:
    got = "RAISED %s: %s" % (type(e).__name__, e)
chk("野文件名：最后一个数字章照报，不抛", got == ("第二章", 2), got)
# load_used_titles 的契约一直是「任何 .md 的首行以 `# ` 开头就算一个已写出的标题」，
# 不看文件名（它在续传里只当去重路标）。这一条只钉「野文件名不再让它抛」，并守住
# 「首行不是标题的文件不会被收进来」这个原有的边界。
used = None
try:
    used = E.load_used_titles(d)
except Exception as e:
    used = "RAISED %s: %s" % (type(e).__name__, e)
chk("野文件名：读标题集合不抛，正常的章都在",
    isinstance(used, set) and {"第一章", "第二章"} <= used, used)
chk("野文件名：首行不是 `# 标题` 的文件不会被收成章节",
    isinstance(used, set) and "随手记的东西，不是章节" not in used, used)

# 编号不是字典序：999 与 1000 并存时要取 1000（旧写法 sorted[-1] 会取 "999.md"）
d = fresh("order")
write(d / "0999.md", "# 旧号\n")
write(d / "1000.md", "# 新号\n")
chk("编号按数值最大取（999 与 1000 并存取 1000）",
    E.get_last_chapter_title(d) == ("新号", 1000), E.get_last_chapter_title(d))

# ── B2 坏字节：只换替代符，不抛、不丢别的章 ─────────────────────────
d = fresh("badbytes")
write(d / "0001.md", "# 第一章\n正文一\n")
write(d / "0002.md", "# 第二章\n正文二\n")
# 标题按 utf-8 写，正文里塞一段 GBK 字节：另存错编码 / 写到一半断电都是这个形状。
BAD = "中文坏字节".encode("gbk")
write_raw(d / "0003.md", "# 第三章\n".encode("utf-8") + BAD)

# 对照：同一份字节用严格的 utf-8 解确实抛 —— 证明夹具不是空转。
strict_raised = False
try:
    with open(d / "0003.md", encoding="utf-8") as f:
        f.read()
except UnicodeDecodeError:
    strict_raised = True
chk("对照：这份坏字节用严格 utf-8 读确实抛（夹具不是空转）", strict_raised)

got = None
try:
    got = E.get_last_chapter_title(d)
except Exception as e:
    got = "RAISED %s: %s" % (type(e).__name__, e)
chk("坏字节：最后一章照报（坏字节不带走这个口）", got == ("第三章", 3), got)

got = None
try:
    got = E.load_used_titles(d)
except Exception as e:
    got = "RAISED %s: %s" % (type(e).__name__, e)
chk("坏字节：已写出集合三章都在，不抛", got == {"第一章", "第二章", "第三章"}, got)

text3 = BX.read_chapter_file(str(d / "0003.md"))
chk("坏字节：正文其余部分照旧，坏的那一段换成替代符",
    text3 is not None and "第三章" in text3 and "\ufffd" in text3, text3)

# ── B3 merge_chapters：坏字节 / 野文件名不许把整本合并打回 ──────────
out = fresh("out")
write(d / "notes.md", "随手记的东西\n")
merged = None
try:
    merged = E.merge_chapters(d, str(out), "样张", "书名", "作者")
except Exception as e:
    merged = "RAISED %s: %s" % (type(e).__name__, e)
chk("合并：坏字节章存在也能落下合并稿，不抛",
    isinstance(merged, str) and os.path.isfile(merged), merged)

if isinstance(merged, str) and os.path.isfile(merged):
    body = pathlib.Path(merged).read_text(encoding="utf-8")
    chk("合并：书名 / 作者 / 三章正文都在",
        "书名" in body and "作者" in body
        and all(x in body for x in ("正文一", "正文二", "第三章")), body[:200])
    chk("合并：坏字节没有把整份打回（替代符在，别的字照旧）",
        "\ufffd" in body and "正文一" in body)

# 正常书：合并稿字节与旧口径逐字相同（回归钉子）
d = fresh("merge-normal")
write(d / "0001.md", "# 第一章\n正文一\n")
write(d / "0002.md", "# 第二章\n正文二\n")
out2 = fresh("out2")
merged2 = E.merge_chapters(d, str(out2), "样张", "书名", "作者")
expect = ("# 书名\n\n**作者**\n\n---\n\n"
          + "# 第一章\n正文一\n" + "\n\n---\n\n"
          + "# 第二章\n正文二\n" + "\n\n---\n\n")
chk("合并：正常书的字节与旧口径一字不差",
    pathlib.Path(merged2).read_text(encoding="utf-8") == expect)

# ── 收尾 ────────────────────────────────────────────────────────────
for n in NOTES:
    print("NOTE  " + n)
print()
print("取书引擎读章口：通过 %d 项，失败 %d 项" % (PASSED[0], len(FAIL)))
if FAIL:
    print("未过：" + "、".join(FAIL))
shutil.rmtree(SB, ignore_errors=True)
sys.exit(1 if FAIL else 0)
