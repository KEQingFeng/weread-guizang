# -*- coding: utf-8 -*-
"""0.9.8 头条修复的取证：中止取书后不许再谎称「已抓取」，且必须能清掉重取。

三段：
  A 进程内单测 —— _reader 的 done 只在引擎说出「全书导出完成」时才置位；
                   reset_book_output 只清产物，不碰书名与笔记。
  B 真机（一次性沙盒 + Chromium）—— 半本 / 谎称取全 / 真取全 三种书，
                   卡片与详情页给出的动作各是什么。
  C 详情页那颗「清掉重取」按下去，确认文案里的章数对不对（不真删，看完就收）。
"""
import atexit
import io
import json
import os
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import selftest  # noqa: E402

sys.path.insert(0, str(selftest.REPO))    # 这份套件要在进程内 import ui_server 验后端
BOOKS = str(selftest.export_sandbox_env())   # 必须在 import ui_server 之前设好环境变量
BASE = selftest.need_base(1)                 # 命令行可传被测服务地址，默认走 GUIZANG_TEST_URL
FAIL = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if not cond:
        FAIL.append(name)


def make_book(bid, chapters, total, done, notes=None):
    d = os.path.join(BOOKS, bid)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(os.path.join(d, "chapters"))
    os.makedirs(os.path.join(d, "images"))
    os.makedirs(os.path.join(d, "raw"))
    for i in range(chapters):
        io.open(os.path.join(d, "chapters", "%03d.md" % i), "w", encoding="utf-8").write(
            "# 第%d章\n\n正文内容。\n" % (i + 1))
    io.open(os.path.join(d, "images", "p1.jpg"), "wb").write(b"\x00")
    io.open(os.path.join(d, "raw", "r1.json"), "w", encoding="utf-8").write("{}")
    json.dump([{"chapterTitle": "第%d章" % (i + 1)} for i in range(total)],
              io.open(os.path.join(d, "_catalog.json"), "w", encoding="utf-8"))
    # 引擎被强杀时 _progress.json 会留下 running:true —— 顺手把这个陈标记也带上
    json.dump({"running": True, "pages": 12, "chapters": chapters},
              io.open(os.path.join(d, "_progress.json"), "w", encoding="utf-8"))
    json.dump({"title": "取证书" + bid, "author": "某人", "done": done,
               "source": "weread", "chars": 500},
              io.open(os.path.join(d, "meta.json"), "w", encoding="utf-8"))
    if notes is not None:
        json.dump(notes, io.open(os.path.join(d, "notes.json"), "w", encoding="utf-8"))
    return d


# ── A. 进程内 ──────────────────────────────────────────
HALF = make_book("RF_HALF", 2, 5, False)
LIE = make_book("RF_LIE", 2, 5, True)
RESET = make_book("RF_RESET", 2, 5, True, {"marks": [{"text": "这句要留"}], "entries": []})


def _drop_fixtures():
    """取证书不许留在书架上过夜：崩了也要收走，否则下一套件的「第一张卡」会点到空书，
    报出一条根本不属于它的失败。"""
    for bid in ("RF_HALF", "RF_LIE", "RF_RESET", "RF_FULL"):
        shutil.rmtree(os.path.join(BOOKS, bid), ignore_errors=True)


atexit.register(_drop_fixtures)

import ui_server  # noqa: E402


class FakeProc:
    """引擎的一个进程：按行吐 stdout（readline 到底返回空串），然后结束。"""

    def __init__(self, lines, code):
        self.lines = [l + "\n" for l in lines]
        self.returncode = code
        outer = self

        def readline():
            return outer.lines.pop(0) if outer.lines else ""

        self.stdout = type("S", (), {"readline": staticmethod(readline)})()

    def wait(self):
        return self.returncode


def run_reader(lines, code, book):
    ui_server.TASK.update({"running": False, "kind": "export", "book": book,
                           "exit_code": None})
    ui_server._reader(FakeProc(lines, code))
    return json.load(io.open(os.path.join(BOOKS, book, "meta.json"), encoding="utf-8"))


m = run_reader(["📖 取证书RF_HALF — 某人", "第1章 已写入", "第2章 已写入"], -9, "RF_HALF")
chk("中止（没有完成行）不把 done 写成真", m.get("done") is False, m)

m2 = run_reader(["📖 取证书RF_HALF — 某人", "全书导出完成"], 0, "RF_HALF")
chk("引擎报了完成才算取全", m2.get("done") is True, m2)

m3 = run_reader(["翻到第 40 页"], 124, "RF_HALF")
chk("退出码非零也没有谎称取全", m3.get("done") is False, m3)
chk("书名读不到时不覆盖成空", m3.get("title") == "取证书RF_HALF", m3)

r = ui_server.reset_book_output("RF_RESET")
chk("清稿有应答", r.get("ok") is True, r)
chk("残稿删干净了",
    not os.path.exists(os.path.join(RESET, "chapters"))
    and not os.path.exists(os.path.join(RESET, "raw"))
    and not os.path.exists(os.path.join(RESET, "images"))
    and not os.path.exists(os.path.join(RESET, "_catalog.json"))
    and not os.path.exists(os.path.join(RESET, "_progress.json")))
mp = json.load(io.open(os.path.join(RESET, "meta.json"), encoding="utf-8"))
chk("清稿把 done 打回假", mp.get("done") is False, mp)
chk("书名作者留着（不然卡片退化成书号）",
    mp.get("title") == "取证书RF_RESET" and mp.get("author") == "某人", mp)
chk("自己的笔记不受牵连",
    os.path.exists(os.path.join(RESET, "notes.json"))
    and json.load(io.open(os.path.join(RESET, "notes.json"), encoding="utf-8"))["marks"])
chk("报清楚删了几个文件", r.get("removed", 0) >= 5, r)

# 陈旧的 running:true 不能让书永远显示「正在取」
shelf = ui_server.list_books()
half = [b for b in shelf if b["id"] == "RF_HALF"]
chk("中止留下的 running:true 被改判为陈旧",
    half and half[0]["progress"].get("running") is False
    and half[0]["progress"].get("stale") is True, half and half[0]["progress"])

# ── B/C. 真机 ──────────────────────────────────────────
from playwright.sync_api import sync_playwright  # noqa: E402


def flush(page):
    page.evaluate("""async () => {
      await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
      document.getAnimations().forEach(a => { try { a.finish(); } catch (e) {} });
    }""")
    page.wait_for_timeout(60)


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []      # JS 自己的错，一条都不该有
    missing = []     # 子资源 404：取证书是没有封面的假书，图片请求落空是有意的
    page.on("console", lambda m: errors.append(m.text) if m.type == "error"
            and "Failed to load resource" not in m.text else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("response", lambda r: missing.append(r.url)
            if r.status == 404 and "/api/cover" not in r.url else None)
    page.goto(BASE + "/", wait_until="networkidle")
    page.wait_for_timeout(1200)
    page.evaluate("() => document.querySelector('[data-v=local]').click()")
    page.wait_for_timeout(600)
    flush(page)

    full = page.evaluate("""() => {
      const has = id => !!local[id];
      return {
        half: has('RF_HALF'), lie: has('RF_LIE'),
        halfFull: bookFull(local.RF_HALF), lieFull: bookFull(local.RF_LIE),
        lieShort: bookShort(local.RF_LIE),
        cards: [...document.querySelectorAll('.vpane:not([hidden]) .wcard')]
                 .map(c => c.textContent).filter(t => /取证书/.test(t)).length,
      };
    }""")
    chk("三本取证书都落进了 local", full["half"] and full["lie"], full)
    chk("半本不当取全", full["halfFull"] is False, full)
    chk("done=true 但章数不够照样不当取全（第二道锁）", full["lieFull"] is False, full)
    chk("短了这件事本身认得出来", full["lieShort"] is True, full)
    chk("本地书库里看得见这两本", full["cards"] >= 2, full)

    # 详情页：半本书必须同时给「接着取」和「清掉重取」
    detail = page.evaluate("""async () => {
      await renderDetailView('RF_LIE', '', '取证书RF_LIE');
      const bar = document.querySelector('.dacts');
      return bar ? [...bar.querySelectorAll('button')].map(b => b.textContent.trim()) : [];
    }""")
    chk("详情页给了接着取", "接着取" in detail, detail)
    chk("详情页给了清掉重取", "清掉重取" in detail, detail)
    chk("半本书不再出现「开始抓取」", "开始抓取" not in detail, detail)

    # 按下去：确认文案要报出章数，且不动我的笔记
    page.evaluate("""() => {
      const b = [...document.querySelectorAll('.dacts button')]
        .find(x => x.textContent.trim() === '清掉重取');
      b.click();
    }""")
    page.wait_for_timeout(500)
    sheet = page.evaluate("""() => {
      const s = document.querySelector('.sheet');
      return s ? {text: s.innerText, btns: [...s.querySelectorAll('button')]
                   .map(b => b.textContent.trim())} : null;
    }""")
    chk("确认层写清了会删掉多少章", sheet and "2 章" in sheet["text"], sheet)
    chk("确认层同时留着「接着取」这条退路", sheet and "接着取" in sheet["btns"],
        sheet and sheet["btns"])
    chk("确认层说了笔记不受影响", sheet and "笔记不受影响" in sheet["text"], sheet)
    selftest.SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(selftest.SHOTS / "refetch.png"))

    # 已取全的那本：不该再出现「清掉重取」
    make_book("RF_FULL", 2, 2, True)
    page.wait_for_timeout(900)
    detail2 = page.evaluate("""async () => {
      await drawState(true);
      await renderDetailView('RF_FULL', '', '取证书RF_FULL');
      const bar = document.querySelector('.dacts');
      return bar ? [...bar.querySelectorAll('button')].map(b => b.textContent.trim()) : [];
    }""")
    chk("取全的那本不再提示清稿", "清掉重取" not in detail2, detail2)

    chk("全程没有 JS 报错", not errors, errors[:3])
    chk("除封面外没有 404", not missing, missing[:3])
    # 没封面的书：详情页那颗 img 靠 onerror 自己摘掉，不该留一个破图洞
    hole = page.evaluate("""() => {
      const img = document.querySelector('.dtop .cv img');
      return img ? img.getBoundingClientRect().height : -1;
    }""")
    chk("封面取不到时不留破图", hole in (-1, 0) or hole > 40, hole)
    browser.close()

shutil.rmtree(os.path.join(BOOKS, "RF_FULL"), ignore_errors=True)

print()
print("取书修复取证：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
