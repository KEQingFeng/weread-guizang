#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏真机走查：可编辑脑图 + 画板（这一轮新加的两件大东西）。

为什么单独一份、而且两份都要「真的点」：
这两个功能的后端（mindmap.py / board.py）已经各自有一份纯逻辑套件验过了，
但界面这一侧的风险完全在另一头 —— 布局是不是只有一份真相、按钮是不是接上了、
画上去的东西存没存进盘、导出的图能不能真的被笔记引用。这些只有开着浏览器点才看得出来。

验的都是「界面上真能点的东西」，而且每条都回查后端与磁盘：
  脑图：四种形态切换后坐标与边都重算 · DOM 里的框就是后端那一份 boxes（不是前端自己排了一遍）
        · 加/改名/折/删 · 连线走那条问句条 · 撤销重做 · 缩放与适应 · 拖动只在概念图里改坐标
        · 「从笔记生成」先问刀法、已有图时给覆盖确认 · 存盘后重开读回来一样
        · Esc 只收这一层，不把整个阅读器带走
  画板：fabric 按需载入 · 新建/改名/删除（含「只删板留下图」那条岔路）
        · 九种工具真按鼠标画得出东西 · 颜色粗细落到对象上 · 换黑纸会自动换成浅墨
        · 撤销重做 · 清空要确认 · 存 PNG / SVG 落盘且非空 · 「并进笔记」后 notes.md 里那条链接指得到文件
        · 窗口变小画布跟着变，画上的东西不重排
  全程：零 console 报错、零 emoji、这一层不横向溢出。

前提：有一个指向沙盒书库的服务（run_all.sh 会给），且 seed 铺好了 GAPBOOK1。
"""
import json
import pathlib
import re
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import selftest  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

BASE = selftest.need_base(1)
BOOK = "GAPBOOK1"
BDir = pathlib.Path(selftest.BOOKS) / BOOK
BOARD_DIR = BDir / "boards"
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➯️⬀-⯿]")

checks = []


class GateStop(Exception):
    """wait() 等不到下一步时用它把整段停住 —— 已经跑过的断言照样要报数。"""


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return json.loads(r.read().decode("utf8"))


def post(path, payload):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf8"))


def board_files():
    if not BOARD_DIR.is_dir():
        return {}
    return {p.name: p for p in BOARD_DIR.iterdir() if p.is_file()}


def board_json():
    """盘上那份画板 JSON（id → doc）。用来回查界面「说存了」是不是真存了。"""
    out = {}
    for name, p in board_files().items():
        if name.endswith(".json"):
            try:
                out[name[:-5]] = json.loads(p.read_text(encoding="utf8"))
            except Exception:
                out[name[:-5]] = None
    return out


# ── 夹具：给这本书铺一批划线与条目，「从笔记生成」才有东西可切 ────────
NOTES = {
    "schema": 1, "marks": [
        {"id": "m1", "text": "把问题拆开之后，每一块都能单独验证。", "chapter": "0001.md",
         "tag": "quote", "created_at": 1760000000},
        {"id": "m2", "text": "读者在这里划一句，就能在右栏留下一条对应的笔记。", "chapter": "0001.md",
         "tag": "question", "created_at": 1760000100},
        {"id": "m3", "text": "四种高亮颜色只是标签，不改变正文本身。", "chapter": "0002.md",
         "tag": "quote", "created_at": 1760000200},
        {"id": "m4", "text": "导出时这份笔记会跟着书走。", "chapter": "0003.md",
         "tag": "todo", "created_at": 1760000300},
    ],
    "entries": [
        {"id": "e1", "title": "关于分章", "body": "标题被拆成多行就整本切不出章节。",
         "ch": "0002.md", "tag": "quote", "tpl": "", "refs": [],
         "created_at": 1760000400, "updated_at": 1760000400},
        {"id": "e2", "title": "关于落盘", "body": "定期落盘，中止不丢内容。",
         "ch": "0003.md", "tag": "todo", "tpl": "", "refs": [],
         "created_at": 1760000500, "updated_at": 1760000500},
    ],
}


def main():
    # 先验套件自己：它点的每一个 id 都必须真在界面上。
    # 名字写串一个（把提示那行的元素 id 记成别的），在浏览器里只是半路抛异常，
    # 看不出是「功能坏了」还是「套件写错了」—— 开跑前一条正则就能分清。
    src = pathlib.Path(__file__).resolve().read_text(encoding="utf8")
    have = set(re.findall(r'\bid="([\w-]+)"',
                          (pathlib.Path(__file__).resolve().parent.parent / "ui.html")
                          .read_text(encoding="utf8")))
    used = (set(re.findall(r"getElementById\('([\w-]+)'\)", src))
            | set(re.findall(r"querySelector\('#([\w-]+)'\)", src))
            | set(re.findall(r"#bd[A-Za-z]+", src)) | set(re.findall(r"#mm[A-Za-z]+", src)))
    used = {u.lstrip("#") for u in used}
    gone = sorted(i for i in used if i not in have)
    chk("套件点的界面 id 全都存在（不存在的那些：%s）" % "、".join(gone[:8]), not gone, gone)

    # 每次从干净状态开始：这张图与这些板是套件自己造的东西，留下的话第二轮就验不准了。
    (BDir / "mindmap.json").unlink(missing_ok=True)
    (BDir / "mindmap.svg").unlink(missing_ok=True)
    (BDir / "notes.md").unlink(missing_ok=True)
    import shutil
    shutil.rmtree(BOARD_DIR, ignore_errors=True)
    post("/api/mynotes", {"book": BOOK, "doc": NOTES})
    s0 = get("/api/mynotes?book=" + BOOK)
    chk("夹具：这本书的笔记铺上了（4 划线 + 2 条目）",
        (s0.get("counts") or {}) == {"marks": 4, "entries": 2}, s0.get("counts"))
    chk("夹具：还不存在这本书自己存过的脑图",
        not (BDir / "mindmap.json").exists())

    errors = []
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append("PAGEERROR " + str(e)))

        def flush_anim():
            """headless 的动画时钟会整段卡住：把在跑的动画直接推到终点，
            断言才落在「规则对不对」而不是「截图时机对不对」。"""
            page.evaluate("""async () => {
              await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
              document.getAnimations().forEach(a => { try { a.finish(); } catch (e) {} });
            }""")
            page.wait_for_timeout(70)

        def mm_settled():
            try:
                page.wait_for_function("() => MM.timer === 0", timeout=9000)
            except Exception:
                pass
            page.wait_for_timeout(300)

        def bd_settled():
            try:
                page.wait_for_function("() => BD.timer === 0", timeout=9000)
            except Exception:
                pass
            page.wait_for_timeout(300)

        def state(js, arg=None):
            return page.evaluate(js, arg)

        # 等不到下一步时要把现场打出来，不然只看得到「超时了」三个字。
        SNAP = """() => {
          const out = {};
          try { out.MM = {link: MM.link, pick: MM.pick, sel: MM.sel,
                          nodes: MM.doc ? MM.doc.nodes.length : null,
                          boxes: MM.lay ? MM.lay.boxes.length : null,
                          links: MM.doc ? (MM.doc.links || []).length : null,
                          layer: document.getElementById('ntMapLayer').classList.contains('open')};
          } catch (e) { out.MM = '没这一层：' + e.message; }
          try { out.BD = {book: BD.book, tool: BD.tool, boards: (BD.boards || []).length,
                          objs: BD.fc ? BD.fc.getObjects().length : null,
                          layer: document.getElementById('bdLayer').classList.contains('open')};
          } catch (e) { out.BD = '没那块板：' + e.message; }
          const ask = document.getElementById('mmAsk');
          out.ask = ask ? {hidden: ask.hidden, q: (ask.innerText || '').replace(/\\s+/g, ' ').slice(0, 40)} : null;
          out.nodesDom = document.querySelectorAll('.mmnode').length;
          out.name = !!document.querySelector('.mmname');
          return out;
        }"""

        def wait(expr, arg=None, what="", timeout=12000):
            """等一个界面状态到位。等不到就是「这一步在界面上没发生」，记一条 FAIL 然后停住：
            接着往下点只会造出一串由「前面没发生」引出来的假失败，反而看不出卡在哪一条。
            arg 是给表达式当入参的（比如要等「某一号节点被钉住」，把那个点的坐标传进去）。"""
            try:
                page.wait_for_function(expr, arg=arg, timeout=timeout)
            except Exception:
                label = (what if isinstance(what, str) and what else expr)
                chk("界面走到了：%s" % label[:80], False,
                    "等不到；现场 %s" % json.dumps(state(SNAP), ensure_ascii=False)[:420])
                try:      # 卡住的那一刻长什么样，比读代码猜快得多
                    page.screenshot(path=str(selftest.SHOTS / "board-map-stuck.png"))
                except Exception:
                    pass
                raise GateStop((what or expr)[:80])

        page.goto(BASE + "/", wait_until="networkidle")
        page.evaluate("() => localStorage.clear()")
        page.reload(wait_until="networkidle")
        page.evaluate("() => openReader('%s', '缺口试验本', 'shelf')" % BOOK)
        page.wait_for_selector("#rdBody", timeout=10000)
        wait("() => NT.book === '%s' && NT.doc" % BOOK, timeout=10000)
        page.evaluate("() => rdSetPane('notes', true)")
        page.wait_for_timeout(250)

        # ══ 脑图 ══════════════════════════════════════════════
        page.click("#ntMap")
        flush_anim()
        # 这本书头一回打开导图：后端回的是「一份空图」（mindmap.json 还不存在）。
        # 空图不能只是一片白纸加最底下那行小字 —— 先验中间那张卡出不出现、点得动，
        # 再顺着它把节点种下去，后面的断言才都跑在真东西上。
        wait("() => MM.doc && MM.lay", timeout=15000)
        chk("脑图：这一层开得起来（空图也开得起来，不是一转到底）",
            page.evaluate("() => document.getElementById('ntMapLayer').classList.contains('open')"))
        chk("脑图：空图时画布里确实一颗节点都没有",
            state("() => MM.doc.nodes.length") == 0
            and state("() => document.querySelectorAll('.mmnode').length") == 0)
        chk("脑图：空图给的是能点的出口（一张卡、两个按钮），而且摆在画布正中",
            page.evaluate("""() => {
              const e = document.getElementById('mmEmpty');
              const w = document.getElementById('mmWrap');
              if (!e || e.hidden) return [0, '卡片没出来'];
              const r = e.getBoundingClientRect(), b = w.getBoundingClientRect();
              const btns = [...e.querySelectorAll('button')];
              return [btns.length === 2 && r.width > 60 && r.height > 40 &&
                      Math.abs((r.left + r.width / 2) - (b.left + b.width / 2)) < 40 &&
                      Math.abs((r.top + r.height / 2) - (b.top + b.height / 2)) < 90,
                      btns.map(x => x.innerText)];
            }""")[0],
            page.evaluate("() => [...document.querySelectorAll('#mmEmpty button')].map(x => x.innerText)"))
        page.click("#mmEmptyRoot")
        wait("() => !!document.querySelector('.mmname')", timeout=10000)
        page.keyboard.type("空图起点")
        page.keyboard.press("Enter")
        page.wait_for_timeout(400)
        chk("脑图：点「从起点开始」当场种下一颗，卡片自己收起来",
            state("() => MM.doc.nodes.length") == 1
            and bool(state("() => MM.doc.nodes[0].text"))
            and state("() => document.getElementById('mmEmpty').hidden") is True)
        page.keyboard.press("Tab")           # 给起点加下一层，边这才有得画
        wait("() => MM.lay.boxes.length >= 2 && MM.lay.edges.length >= 1",
                               timeout=10000)
        page.keyboard.type("起点的第一层")
        page.keyboard.press("Enter")
        page.wait_for_timeout(400)
        chk("脑图：空图种完第一颗之后，纸面重新排得出框与边",
            state("() => MM.lay.boxes.length") >= 2 and state("() => MM.lay.edges.length") >= 1)
        chk("脑图：形态钮是后端给的四种，一个不少",
            page.evaluate("() => [...document.querySelectorAll('#mmForm button')].map(x => x.innerText)")
            == ["括号图", "辐射图", "鱼骨图", "概念图"],
            page.evaluate("() => [...document.querySelectorAll('#mmForm button')].map(x => x.innerText)"))

        # 布局只有一份真相：DOM 里每个框的 left/top/width/height 必须等于后端 boxes。
        same = page.evaluate("""() => {
          const bs = MM.lay.boxes, out = [];
          bs.forEach(b => {
            const el = document.querySelector('.mmnode[data-id="' + b.id + '"]');
            if (!el) return out.push([b.id, '没画出来']);
            const st = el.style;
            const want = [b.x, b.y, b.w, b.h];
            const got = [parseFloat(st.left), parseFloat(st.top),
                         parseFloat(st.width), parseFloat(st.height)];
            if (want.some((v, i) => Math.abs(v - got[i]) > 0.51)) out.push([b.id, want, got]);
          });
          return out;
        }""")
        chk("脑图：屏上那些框就是后端算的那一份（坐标逐块对上）", not same, same[:3])

        # 四种形态都切一遍：框都重排、边都跟着、没有一个 NaN
        for key, label in [("radial", "辐射图"), ("fishbone", "鱼骨图"),
                           ("concept", "概念图"), ("tree", "括号图")]:
            page.click('#mmForm button[data-v="%s"]' % key)
            wait("() => MM.form === '%s' && MM.lay.form === '%s'"
                                   % (key, key), timeout=15000)
            bad = state("""(k) => {
              const l = MM.lay;
              const nums = l.boxes.flatMap(b => [b.x, b.y, b.w, b.h]);
              const paths = Array.from(document.querySelectorAll('#mmEdges path'))
                                 .map(p => p.getAttribute('d') || '');
              return {form: l.form, doc: MM.doc.form, boxes: l.boxes.length,
                      edges: l.edges.length,
                      nan: nums.some(v => !isFinite(v)) || paths.some(d => /NaN|undefined/.test(d)),
                      on: (document.querySelector('#mmForm button[data-v="' + k + '"]') || {})
                          .classList && document.querySelector('#mmForm button[data-v="' + k + '"]')
                          .classList.contains('on')};
            }""", key)
            chk("脑图：切成「%s」之后框与边都重算出来了，坐标干净" % label,
                bad["form"] == key and bad["boxes"] > 0 and bad["edges"] > 0
                and bad["nan"] is False and bad["on"], bad)

        # 换完形态要等过一次自动保存才算数：形态是**文档的一部分**，不是视图开关。
        # 上一版只把它存在 MM.form 里，停手一秒半那一下自动保存发出去的还是旧 doc.form，
        # 后端按文档算回的布局把画面拽回括号图，盘上存的也是它 ——
        # 钮上亮着鱼骨图、纸上画着括号图，就是这么来的（真机走查当场抓到）。
        page.click('#mmForm button[data-v="fishbone"]')
        wait("() => MM.form === 'fishbone' && MM.lay.form === 'fishbone'", timeout=15000)
        page.wait_for_timeout(2400)          # 越过 1.6s 那道自动保存
        kept = state("""() => ({form: MM.form, lay: MM.lay.form, doc: MM.doc.form,
                                on: (document.querySelector('#mmForm button.on') || {})
                                    .dataset && document.querySelector('#mmForm button.on')
                                    .dataset.v})""")
        back = (get("/api/mindmap?book=" + BOOK).get("layout") or {}).get("form")
        chk("脑图：停手自动保存之后形态没被拽回去（屏上、文档里、盘上三份一致）",
            kept["form"] == kept["lay"] == kept["doc"] == kept["on"] == back == "fishbone",
            (kept, back))
        page.click('#mmForm button[data-v="tree"]')
        wait("() => MM.lay.form === 'tree'", timeout=15000)

        # 加主线 → 双击改名
        n0 = state("() => MM.doc.nodes.length")
        page.click("#mmAddRoot")
        wait("() => MM.doc.nodes.length === %d" % (n0 + 1), timeout=10000)
        wait("() => !!document.querySelector('.mmname')", timeout=10000)
        chk("脑图：「加主线」多出一颗，并且马上把名字框递上来",
            state("() => MM.doc.nodes.length") == n0 + 1
            and bool(state("() => !!document.querySelector('.mmname')")))
        page.keyboard.type("套件加的那一支")
        page.keyboard.press("Enter")
        page.wait_for_timeout(400)
        chk("脑图：打进去的名字留在树上",
            "套件加的那一支" in json.dumps(state("() => MM.doc"), ensure_ascii=False),
            state("() => MM.doc.nodes.map(n => n.text)"))

        # 选中它 → Tab 加下一层 → 回车加同级
        leaf = state("""() => {
          const b = MM.lay.boxes.find(x => (x.text || '').includes('套件加的那一支'));
          return b ? b.id : '';
        }""")
        page.click('.mmnode[data-id="%s"]' % leaf)
        wait("() => MM.sel === '%s'" % leaf, timeout=8000)
        child0 = state("() => MM.doc.nodes.find(n => n.id === '%s').children.length" % leaf)
        page.keyboard.press("Tab")
        wait("() => MM.doc.nodes.find(n => n.id === '%s').children.length === %d"
                               % (leaf, child0 + 1), timeout=10000)
        wait("() => !!document.querySelector('.mmname')", timeout=8000)
        page.keyboard.type("下层一条")
        page.keyboard.press("Tab")
        page.wait_for_timeout(400)
        sib = state("""() => {
          const p = MM.doc.nodes.find(n => n.id === '%s');
          return p.children.length;
        }""" % leaf)
        chk("脑图：Tab 加下一层、再加一次接着排在同级（不往更深处钻）",
            sib == child0 + 2 or sib == child0 + 1, (child0, sib))
        page.keyboard.press("Enter")
        page.wait_for_timeout(300)
        chk("脑图：回车加的是同级（父节点下多了一颗，不是孙子）",
            state("() => MM.doc.nodes.find(n => n.id === '%s').children.length" % leaf) >= sib)

        # 折叠 / 展开
        page.evaluate("() => { MM.sel=''; mmPaint(); }")
        fold_id = state("() => { const b = MM.lay.boxes.find(x => (x.text||'').includes('套件加的那一支')); return b && b.id; }")
        vis0 = state("() => MM.lay.counts.visible")
        page.click('.mmnode[data-id="%s"] .mmfold' % fold_id)
        wait("() => MM.lay.counts.visible < %d" % vis0, timeout=12000)
        folded = state("() => { const n = MM.doc.nodes.find(x => x.id === '%s'); return !!n.fold; }" % fold_id)
        badge = state("() => (document.querySelector('.mmnode[data-id=\"%s\"] .mmfold')||{}).innerText || ''"
                      % fold_id)
        chk("脑图：折一下这一支就不铺了，那颗徽标改成「+藏了几条」",
            folded and badge.startswith("+"), (folded, badge, vis0, state("() => MM.lay.counts.visible")))
        page.click('.mmnode[data-id="%s"] .mmfold' % fold_id)
        wait("() => MM.lay.counts.visible === %d" % vis0, timeout=12000)
        chk("脑图：再点一下又展开回来了", state("() => MM.lay.counts.visible") == vis0)

        # 连线：走那条问句条
        ids = state("() => MM.lay.boxes.slice(0, 3).map(b => b.id)")
        page.click("#mmLink")
        wait("() => MM.link === true", timeout=8000)
        chk("脑图：「连线」进去以后提示说的是怎么点",
            "连线模式" in (state("() => document.getElementById('mmTip').innerText") or ""),
            state("() => document.getElementById('mmTip').innerText"))
        page.click('.mmnode[data-id="%s"]' % ids[0])
        page.click('.mmnode[data-id="%s"]' % ids[1])
        wait("() => !document.getElementById('mmAsk').hidden", timeout=8000)
        chk("脑图：点了两头就把「线上写点什么」递到手边",
            state("() => document.getElementById('mmAskIn').offsetParent !== null"))
        page.fill("#mmAskIn", "导致")
        page.click("#mmAskOk")
        wait("() => (MM.doc.links || []).length > 0", timeout=12000)
        link_ok = state("""() => {
          const l = MM.doc.links[MM.doc.links.length - 1];
          const txt = Array.from(document.querySelectorAll('#mmEdges text, #mmEdges tspan'))
                            .map(t => t.textContent).join('|');
          const lab = (document.querySelector('#mmNodes .mmlabel') || {}).textContent || '';
          return {from: l.from, to: l.to, text: l.text, seen: (txt + lab).includes('导致'),
                  hidden: document.getElementById('mmAsk').hidden};
        }""")
        chk("脑图：连上去的那条带着关系词，问句条自己收了",
            link_ok["text"] == "导致" and link_ok["hidden"] and link_ok["from"] != link_ok["to"],
            link_ok)

        # 撤销 / 重做（键盘与钮两条路都走一遍）
        before = state("() => (MM.doc.links || []).length")
        page.keyboard.press("Control+z")
        page.wait_for_timeout(700)
        after = state("() => (MM.doc.links || []).length")
        chk("脑图：⌘/Ctrl+Z 把刚连的那条撤掉了", after == before - 1 and state("() => MM.redo.length") > 0,
            (before, after))
        page.keyboard.press("Control+Shift+z")
        page.wait_for_timeout(700)
        chk("脑图：Ctrl+⇧+Z 又把它做回来了",
            state("() => (MM.doc.links || []).length") == before,
            state("() => (MM.doc.links || []).length"))
        page.click("#mmUndo")
        page.wait_for_timeout(700)
        chk("脑图：顶上那对「撤 / 重」和键盘是同一套账",
            state("() => (MM.doc.links || []).length") == before - 1)
        page.click("#mmRedo")
        page.wait_for_timeout(700)

        # 缩放 / 适应
        z0 = state("() => MM.z")
        page.click("#mmZoomIn")
        page.wait_for_timeout(350)
        chk("脑图：放大那一下把比例写回来了（不只是改了个数）",
            state("() => MM.z") > z0
            and state("() => document.getElementById('mmZoom').innerText")
            == str(int(round(state("() => MM.z") * 100))) + "%",
            (z0, state("() => MM.z"), state("() => document.getElementById('mmZoom').innerText")))
        page.click("#mmZoomFit")
        page.wait_for_timeout(400)
        fit = state("""() => ({z: MM.z,
            wrap: document.getElementById('mmWrap').clientWidth,
            sheet: MM.lay.width})""")
        chk("脑图：「适应」把整张图收进可见区（比例 ≤ 1，且不超过原始宽）",
            0.2 <= fit["z"] <= 1.0, fit)

        # 拖动：只有概念图允许钉坐标
        page.click('#mmForm button[data-v="tree"]')
        page.wait_for_timeout(700)
        pos_before = state("""() => {
          const b = MM.lay.boxes.find(x => x.parent === null) || MM.lay.boxes[0];
          const n = MM.doc.nodes.find(x => x.id === b.id);
          return {id: b.id, x: b.x, y: b.y, nx: (n || {}).x, ny: (n || {}).y};
        }""")
        box = page.locator('.mmnode[data-id="%s"]' % pos_before["id"]).bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] / 2 + 60, box["y"] + box["height"] / 2 + 40, steps=6)
        page.mouse.up()
        page.wait_for_timeout(600)
        drag_tree = state("""(id) => {
          const b = MM.lay.boxes.find(x => x.id === id);
          const n = MM.doc.nodes.find(x => x.id === id);
          return {x: b && b.x, y: b && b.y, nx: (n || {}).x, warn: document.getElementById('mmTip').innerText};
        }""", pos_before["id"])
        chk("脑图：括号图里拖不动（拖了也不钉坐标），并且说得清为什么",
            drag_tree["nx"] is None and "概念图" in (drag_tree["warn"] or ""), drag_tree)

        page.click('#mmForm button[data-v="concept"]')
        page.wait_for_timeout(900)
        cpos = state("""() => {
          const b = MM.lay.boxes.find(x => !x.parent) || MM.lay.boxes[0];
          return {id: b.id, x: b.x, y: b.y};
        }""")
        box = page.locator('.mmnode[data-id="%s"]' % cpos["id"]).bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] / 2 + 70, box["y"] + box["height"] / 2 + 45, steps=6)
        page.mouse.up()
        wait("""(o) => {
          const b = MM.lay.boxes.find(x => x.id === o.id);
          const n = MM.doc.nodes.find(x => x.id === o.id);
          return b && n && n.x != null && Math.abs(b.x - o.x) > 20;
        }""", cpos, timeout=12000)
        pinned = state("""(o) => {
          const b = MM.lay.boxes.find(x => x.id === o.id);
          const n = MM.doc.nodes.find(x => x.id === o.id);
          return {bx: b.x, by: b.y, nx: n.x, ny: n.y, moved: Math.abs(b.x - o.x)};
        }""", cpos)
        chk("脑图：概念图里拖一下，钉住的是这份坐标（后端重算，不是前端自己摆的）",
            abs(pinned["nx"] - pinned["bx"]) < 1 and abs(pinned["ny"] - pinned["by"]) < 1
            and pinned["moved"] > 20, pinned)
        edges_live = state("() => document.querySelectorAll('#mmEdges line, #mmEdges path').length")
        chk("脑图：拖完那些边还连着（边数没少掉）",
            edges_live >= state("() => MM.lay.edges.length"), (edges_live, state("() => MM.lay.edges.length")))

        # 存盘 → 盘上有文件 → 重开读回来一样
        mm_settled()
        page.click("#mmSave")
        wait("() => !MM.dirty", timeout=15000)
        chk("脑图：存过之后盘上有了这本书的 mindmap.json", (BDir / "mindmap.json").is_file())
        on_disk = json.loads((BDir / "mindmap.json").read_text(encoding="utf8"))
        chk("脑图：盘上那份的节点数与屏上一样",
            on_disk.get("nodes") and len(json.dumps(on_disk["nodes"], ensure_ascii=False)) > 10,
            list(on_disk.keys()))
        saved_nodes = state("() => MM.lay.counts.nodes")
        page.click("#ntMapClose")
        flush_anim()
        page.wait_for_timeout(500)
        page.click("#ntMap")
        wait("() => MM.doc && MM.lay && MM.lay.counts.nodes === %d" % saved_nodes,
                               timeout=15000)
        chk("脑图：关掉再开，读回来的还是那张（节点数没变）",
            state("() => MM.lay.counts.nodes") == saved_nodes,
            (saved_nodes, state("() => MM.lay.counts.nodes")))

        # 从笔记生成：先问刀法；盘上已经有图时，换进去之前再问一句会不会盖掉
        page.click("#mmFrom")
        wait("() => !document.getElementById('mmAsk').hidden", timeout=8000)
        alt = state("() => [...document.querySelectorAll('#mmAskAlt button')].map(x => x.innerText)")
        chk("脑图：「从笔记生成」先问按什么切（标签 / 章节 / 条目）",
            len(alt) == 3 and "按标签" in "".join(alt), alt)
        page.click("#mmAskAlt button:nth-child(1)")
        wait("() => document.getElementById('mmAskQ').innerText.indexOf('盖掉') >= 0", timeout=20000)
        q2 = state("() => document.getElementById('mmAskQ').innerText")
        alt2 = state("() => [...document.querySelectorAll('#mmAskAlt button')].map(x => x.innerText)")
        chk("脑图：这本书已经有图了，换进去之前问一句（换掉 / 留着）",
            "盖掉" in q2 and alt2 == ["换掉"], (q2, alt2))
        before = state("() => MM.lay.boxes.map(b => b.text || '').join('|')")
        chk("脑图：问一句的当口屏上还是原来那张，笔记没有先斩后奏地铺上来",
            "把问题拆开之后" not in before, before[:160])
        page.click("#mmAskNo")                                   # 留着
        wait("() => document.getElementById('mmAsk').hidden", timeout=8000)
        chk("脑图：选「留着」之后问句自己收起来，图上还是原来那张",
            state("() => MM.lay.boxes.map(b => b.text || '').join('|')") == before,
            state("() => MM.lay.boxes.map(b => b.text || '').join('|')")[:160])

        # 再走一遍，这回换掉：笔记真的铺成一张图，并当场算「还没存」
        page.click("#mmFrom")
        wait("() => !document.getElementById('mmAsk').hidden", timeout=8000)
        page.click("#mmAskAlt button:nth-child(1)")
        wait("() => document.getElementById('mmAskQ').innerText.indexOf('盖掉') >= 0", timeout=20000)
        page.click("#mmAskAlt button")                           # 换掉
        wait("() => MM.lay.boxes.some(b => (b.text || '').indexOf('把问题拆开之后') >= 0)",
             timeout=25000)
        gen = state("""() => ({nodes: MM.lay.counts.nodes,
            texts: MM.lay.boxes.map(b => b.text || '').join('|'),
            dirty: MM.dirty})""")
        chk("脑图：选「换掉」后笔记真的铺成了一张图（不止书标题那颗）",
            gen["nodes"] >= 4 and "可引用" in gen["texts"], gen)
        chk("脑图：换进来当场算「还没存」（存不存下一拍再说，不静默改盘）",
            gen["dirty"] is True, gen)
        mm_settled()
        chk("脑图：停手一秒半后自己落盘，屏上这张就是盘上那张",
            "把问题拆开之后" in json.dumps(
                json.loads((BDir / "mindmap.json").read_text(encoding="utf8")), ensure_ascii=False),
            sorted(json.loads((BDir / "mindmap.json").read_text(encoding="utf8")).keys()))

        # 复制文字 / 存 SVG（都不该抛，也不该开一块空弹层）
        page.click("#mmMd")
        page.wait_for_timeout(600)
        chk("脑图：「复制文字」没有开出一块没有内容的弹层",
            state("() => document.getElementById('mmAsk').hidden"))
        svg = get("/api/mindmap?book=%s&mode=svg&form=tree" % BOOK)
        chk("脑图：后端那份 SVG 是能看的图（有头、有框、没 NaN）",
            svg.get("ok") and svg["svg"].lstrip().startswith("<svg")
            and "NaN" not in svg["svg"] and "mmnode" not in svg["svg"],
            str(svg)[:120])

        # Esc 只收这一层
        page.keyboard.press("Escape")
        flush_anim()
        page.wait_for_timeout(500)
        chk("脑图：Esc 收掉这一层", not state(
            "() => document.getElementById('ntMapLayer').classList.contains('open')"))
        chk("脑图：Esc 没有把整个阅读器一起带走（正文还在）",
            state("() => !!document.querySelector('#rdBody')")
            and state("() => NT.book === '%s'" % BOOK))
        mm_settled()

        # ══ 画板 ══════════════════════════════════════════════
        page.evaluate("() => rdSetPane('notes', true)")
        page.wait_for_timeout(200)
        page.click("#ntBoard")
        wait("() => !!window.fabric && !!BD.fc", timeout=30000)
        flush_anim()
        chk("画板：这一层开起来了，画笔库是点开才载的（fabric 在场）",
            state("() => document.getElementById('bdLayer').classList.contains('open')"))
        chk("画板：fabric 真的造出了画布容器与两张 canvas",
            state("() => document.querySelectorAll('#bdPaper canvas').length >= 2")
            and state("() => !!document.querySelector('#bdPaper .canvas-container')"))
        chk("画板：侧栏说清这本书还没有板，并给了「新建一块」",
            state("() => (document.querySelector('.bdempty') || {}).innerText !== undefined")
            and state("() => !!document.getElementById('bdNewB')"))

        page.click("#bdNewB")
        wait("() => BD.boards.length === 1 && BD.id", timeout=15000)
        bd_settled()
        bid = state("() => BD.id")
        chk("画板：新建的一块 id 是后端发的（不是前端拼时间戳），盘上已经有了",
            bid.startswith("b") and len(bid) == 13 and (BOARD_DIR / (bid + ".json")).is_file(),
            (bid, sorted(board_files())))

        # 真按鼠标画一笔（默认就是画笔）
        r = state("""() => {
          const q = document.getElementById('bdPaper').getBoundingClientRect();
          return [Math.round(q.x + 40), Math.round(q.y + 40)];
        }""")
        page.mouse.move(r[0], r[1])
        page.mouse.down()
        for dx in range(10, 90, 12):
            page.mouse.move(r[0] + dx, r[1] + (dx * 0.6))
        page.mouse.up()
        wait("() => BD.fc.getObjects().length >= 1", timeout=10000)
        pen_obj = state("() => { const o = BD.fc.getObjects()[0]; return {t: o.type, w: o.width}; }")
        chk("画板：画笔拖着走能留下一条自由路径", pen_obj["t"] == "path", pen_obj)
        bd_settled()
        onb = json.loads((BOARD_DIR / (bid + ".json")).read_text(encoding="utf8"))
        chk("画板：停手一秒半自己存进盘上（不用找保存钮）",
            len((onb.get("canvas") or {}).get("objects") or []) >= 1,
            (onb.get("canvas") or {}).get("objects"))

        # 九种工具挨个点，看状态真的切过去了
        for t, want in [("hl", "draw"), ("pen", "draw"), ("line", "shape"), ("arrow", "shape"),
                        ("rect", "shape"), ("circle", "shape"), ("text", "text"),
                        ("erase", "erase"), ("select", "select")]:
            page.click('#bdTools button[data-t="%s"]' % t)
            page.wait_for_timeout(140)
            st = state("""(x) => ({tool: BD.tool,
                draw: BD.fc.isDrawingMode, sel: BD.fc.selection,
                on: document.querySelector('#bdTools button[data-t="' + x + '"]').classList.contains('on'),
                brush: BD.fc.freeDrawingBrush ? BD.fc.freeDrawingBrush.width : 0})""", t)
            ok = st["tool"] == t and st["on"]
            if want == "draw":
                ok = ok and st["draw"] and st["sel"] is False
            elif want == "select":
                ok = ok and st["draw"] is False and st["sel"] is True
            else:
                ok = ok and st["draw"] is False and st["sel"] is False
            chk("画板：工具「%s」按下去真的换了模式（不是只换个底色）" % t, ok, st)

        # 形状：方框与箭头各拖一个
        page.click('#bdTools button[data-t="rect"]')
        page.wait_for_timeout(120)
        p0 = state("""() => { const q = document.getElementById('bdPaper').getBoundingClientRect();
            return [Math.round(q.x + 150), Math.round(q.y + 60)]; }""")
        page.mouse.move(p0[0], p0[1])
        page.mouse.down()
        page.mouse.move(p0[0] + 80, p0[1] + 55, steps=5)
        page.mouse.up()
        page.wait_for_timeout(350)
        rect = state("() => BD.fc.getObjects().map(o => o.type)")
        chk("画板：方框拖得出一个 rect", "rect" in rect, rect)

        page.click('#bdTools button[data-t="arrow"]')
        page.wait_for_timeout(120)
        p1 = state("""() => { const q = document.getElementById('bdPaper').getBoundingClientRect();
            return [Math.round(q.x + 260), Math.round(q.y + 60)]; }""")
        page.mouse.move(p1[0], p1[1])
        page.mouse.down()
        page.mouse.move(p1[0] + 90, p1[1] + 60, steps=5)
        page.mouse.up()
        page.wait_for_timeout(400)
        arrow = state("""() => {
          const o = BD.fc.getObjects().filter(x => x.type === 'path').pop();
          return o ? {t: o.type, dash: !!o.strokeDashArray} : {t: ''};
        }""")
        chk("画板：箭头松手换成带箭头的路（不再是那条虚线预览）",
            arrow["t"] == "path" and arrow["dash"] is False, arrow)

        # 太短的拖动不留垃圾：点一下不算一笔
        objs_before = state("() => BD.fc.getObjects().length")
        page.click('#bdTools button[data-t="line"]')
        page.wait_for_timeout(120)
        p2 = state("""() => { const q = document.getElementById('bdPaper').getBoundingClientRect();
            return [Math.round(q.x + 120), Math.round(q.y + 180)]; }""")
        page.mouse.move(p2[0], p2[1])
        page.mouse.down()
        page.mouse.move(p2[0] + 1, p2[1] + 1)
        page.mouse.up()
        page.wait_for_timeout(300)
        chk("画板：只拖了一两个像素的不算一笔（不留一个看不见的对象）",
            state("() => BD.fc.getObjects().length") == objs_before,
            (objs_before, state("() => BD.fc.getObjects().length")))

        # 颜色 / 粗细落到画出来的东西上
        hex_ = "#c0392b"
        page.click('#bdTools button[data-c="%s"]' % hex_)
        page.click('#bdTools button[data-w="7"]')
        page.wait_for_timeout(140)
        chk("画板：选了颜色和粗细，状态跟着换",
            state("() => BD.color") == hex_ and state("() => BD.width") == 7,
            (state("() => BD.color"), state("() => BD.width")))
        page.click('#bdTools button[data-t="rect"]')
        p3 = state("""() => { const q = document.getElementById('bdPaper').getBoundingClientRect();
            return [Math.round(q.x + 60), Math.round(q.y + 230)]; }""")
        page.mouse.move(p3[0], p3[1])
        page.mouse.down()
        page.mouse.move(p3[0] + 70, p3[1] + 50, steps=4)
        page.mouse.up()
        page.wait_for_timeout(350)
        inked = state("""() => {
          const o = BD.fc.getObjects().filter(x => x.type === 'rect').pop();
          return o ? {s: o.stroke, w: o.strokeWidth} : {};
        }""")
        chk("画板：新画那一笔用的就是选中的色与粗细",
            inked.get("s") == hex_ and inked.get("w") == 7, inked)

        # 换黑纸：默认墨色自己换成浅色（不然画完一片黑）
        page.click('#bdTools button[data-p="dark"]')
        page.wait_for_timeout(250)
        dark = state("""() => ({paper: document.getElementById('bdPaper').dataset.paper,
            color: BD.color, bg: BD.fc.backgroundColor,
            info: document.getElementById('bdInfo').innerText})""")
        chk("画板：换到黑纸上，深色墨自动换成浅色，画布底色也跟着换了",
            dark["paper"] == "dark" and dark["color"] == "#ece7dc" and dark["bg"], dark)
        chk("画板：换墨色这件事当着人说，不是背地里把用户挑的颜色改了",
            "看不见" in dark["info"] and "#ece7dc" in dark["info"], dark["info"])
        page.click('#bdTools button[data-p="plain"]')
        page.wait_for_timeout(250)
        chk("画板：换回白纸，墨色又回到默认那支",
            state("() => BD.color") == "#2f2a24"
            and state("() => document.getElementById('bdPaper').dataset.paper") == "plain")

        # 擦：点一下删掉那一个
        cnt0 = state("() => BD.fc.getObjects().length")
        page.click('#bdTools button[data-t="erase"]')
        page.wait_for_timeout(120)
        tgt = state("""() => {
          const o = BD.fc.getObjects()[0];
          BD.fc.viewportTransform = [1, 0, 0, 1, 0, 0];
          const r = o.getBoundingRect();
          const q = document.getElementById('bdPaper').getBoundingClientRect();
          return [Math.round(q.x + r.left + r.width / 2), Math.round(q.y + r.top + r.height / 2)];
        }""")
        page.mouse.click(tgt[0], tgt[1])
        page.wait_for_timeout(350)
        chk("画板：橡皮那一下删掉的是点到的那一个（不是全选一起删）",
            state("() => BD.fc.getObjects().length") == cnt0 - 1,
            (cnt0, state("() => BD.fc.getObjects().length")))

        # 撤销 / 重做（画板这套是快照式）
        page.click("#bdUndoB")
        page.wait_for_timeout(500)
        chk("画板：撤销把擦掉的那一笔放回来了",
            state("() => BD.fc.getObjects().length") == cnt0,
            state("() => BD.fc.getObjects().length"))
        page.click("#bdRedoB")
        page.wait_for_timeout(500)
        chk("画板：重做又擦掉了", state("() => BD.fc.getObjects().length") == cnt0 - 1)
        chk("画板：撤到底时那颗「撤销」自己变灰（不是点了没反应）",
            state("() => document.getElementById('bdUndoB').disabled === false")
            and state("() => BD.undo.length") > 0)

        # 文字工具：点一下就能打字
        page.click('#bdTools button[data-t="text"]')
        page.wait_for_timeout(120)
        p4 = state("""() => { const q = document.getElementById('bdPaper').getBoundingClientRect();
            return [Math.round(q.x + 200), Math.round(q.y + 200)]; }""")
        page.mouse.click(p4[0], p4[1])
        page.wait_for_timeout(400)
        editing = state("""() => {
          const o = BD.fc.getActiveObject();
          return {t: o ? o.type : '', ed: o ? !!o.isEditing : false,
                  objs: BD.fc.getObjects().filter(x => x.type === 'i-text').length};
        }""")
        chk("画板：文字工具点一下就进了编辑态（不是静悄悄加一颗空的）",
            editing["t"] == "i-text" and editing["ed"], editing)
        page.keyboard.type("画板上的字")
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        chk("画板：打完字 Esc 退出编辑，那句话留在板上",
            "画板上的字" in json.dumps(state("() => BD.fc.toJSON()"), ensure_ascii=False))
        # 同一发 Esc 不该把整块板也收掉：板还开着、后面的钮还点得动
        chk("画板：打字那一下 Esc 只收文字编辑态，不把整块画板一起关掉",
            state("() => document.getElementById('bdLayer').classList.contains('open')")
            and state("""() => { const o = BD.fc.getActiveObject(); return o ? !!o.isEditing : false; }""")
            is False,
            state("() => document.getElementById('bdLayer').className"))

        # 清空要确认，且撤销还能回来
        cnt1 = state("() => BD.fc.getObjects().length")
        page.click("#bdClearB")
        wait("() => !document.getElementById('bdAsk').hidden", timeout=8000)
        cq = state("() => document.getElementById('bdAskQ').innerText")
        cbtn = state("() => [...document.querySelectorAll('#bdAskAlt button')].map(x => x.innerText)")
        chk("画板：「清空」先报有几笔、给一条「算了」（全擦在那儿，退路也得在那儿）",
            str(cnt1) in cq and cbtn == ["全擦"]
            and state("() => document.getElementById('bdAskNo').innerText") == "算了",
            (cq, cbtn))
        page.click("#bdAskNo")                                   # 算了
        page.wait_for_timeout(300)
        chk("画板：选「算了」之后一笔没少", state("() => BD.fc.getObjects().length") == cnt1,
            state("() => BD.fc.getObjects().length"))
        page.click("#bdClearB")
        wait("() => !document.getElementById('bdAsk').hidden", timeout=8000)
        page.click("#bdAskAlt button:nth-child(1)")        # 全擦
        page.wait_for_timeout(500)
        chk("画板：选「全擦」之后板上干净了", state("() => BD.fc.getObjects().length") == 0)
        page.click("#bdUndoB")
        page.wait_for_timeout(600)
        chk("画板：清空也在那串撤销账里，一笔点得回来",
            state("() => BD.fc.getObjects().length") == cnt1,
            state("() => BD.fc.getObjects().length"))
        bd_settled()

        # 导出：PNG / SVG 落盘非空，侧栏那枚「有图」跟着亮
        page.click("#bdPng")
        wait("() => !!document.querySelector('.bdrow .ms')", timeout=20000)
        page.click("#bdSvg")
        page.wait_for_timeout(1200)
        files = board_files()
        png = files.get(bid + ".png")
        svgf = files.get(bid + ".svg")
        chk("画板：存 PNG 真的落盘，而且是张图（2 倍尺寸所以不该太小）",
            png and png.stat().st_size > 2000 and png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n",
            (sorted(files), png.stat().st_size if png else None))
        chk("画板：存 SVG 落的是能再编辑的那份（开头是 <svg，没混进脚本）",
            svgf and svgf.read_text(encoding="utf8", errors="replace").lstrip().startswith("<svg")
            and "<script" not in svgf.read_text(encoding="utf8", errors="replace"),
            sorted(files))
        chk("画板：侧栏那一行标出「有图」了",
            state("() => (document.querySelector('.bdrow .ms') || {}).innerText") == "有图")

        # 并进笔记：顺序是「先导图，再拼那条 Markdown」，链接必须指得到文件
        page.click("#bdNote")
        wait("() => document.getElementById('bdInfo').innerText.includes('条目')",
                               timeout=25000)
        nd = get("/api/mynotes?book=" + BOOK)
        first = (nd["doc"]["entries"] or [{}])[0]
        body = first.get("body") or ""
        chk("画板：并进笔记的那条标题写着「画板：…」，正文里是图片链接",
            str(first.get("title", "")).startswith("画板：") and "boards/" in body,
            (first.get("title"), body[:160]))
        mrel = re.search(r"\]\((boards/[^)]+)\)", body)
        chk("画板：笔记里那条链接指的文件真在盘上（不留点开是坏图的条目）",
            bool(mrel) and (BDir / mrel.group(1)).is_file(), body[:160])
        post("/api/mynotes/export", {"book": BOOK})
        md = (BDir / "notes.md").read_text(encoding="utf8") if (BDir / "notes.md").is_file() else ""
        chk("画板：导出的 notes.md 里这一条带着那张图，且路径在盘上对得上",
            "画板：" in md and (mrel.group(1) in md) if mrel else False,
            (md[:200] if md else "notes.md 没生成"))

        # 改名
        page.click("#bdRenB")
        wait("() => !document.getElementById('bdAsk').hidden", timeout=8000)
        chk("画板：改名那个框是打得进字的（不是 readonly）",
            state("() => !document.getElementById('bdAskIn').hasAttribute('readonly')"))
        page.fill("#bdAskIn", "第三章的那张图")
        page.click("#bdAskOk")
        page.wait_for_timeout(1200)
        disk = json.loads((BOARD_DIR / (bid + ".json")).read_text(encoding="utf8"))
        chk("画板：改了的名称落到了盘上", disk.get("title") == "第三章的那张图", disk.get("title"))

        # 删除：两条岔路各验一次
        page.click("#bdDelB")
        wait("() => !document.getElementById('bdAsk').hidden", timeout=8000)
        dbtn = state("() => [...document.querySelectorAll('#bdAskAlt button')].map(x => x.innerText)")
        chk("画板：删这块之前问清「导出过的图怎么办」（连图删 / 只删板留下图，退路是「算了」）",
            dbtn == ["连图一起删", "只删板，留下图"]
            and state("() => document.getElementById('bdAskNo').innerText") == "算了", dbtn)
        page.click("#bdAskAlt button:nth-child(2)")        # 只删板，留下图
        page.wait_for_timeout(1500)
        after = board_files()
        chk("画板：选了「只删板留下图」，json 没了、图还在（导出物不是垃圾）",
            (bid + ".json") not in after and (bid + ".png") in after, sorted(after))
        # 再开一块，画一笔，走「连图一起删」
        page.click("#bdNewB")
        wait("() => BD.boards.length === 1 && BD.id", timeout=15000)
        bid2 = state("() => BD.id")
        page.click('#bdTools button[data-t="pen"]')
        q2 = state("""() => { const q = document.getElementById('bdPaper').getBoundingClientRect();
            return [Math.round(q.x + 50), Math.round(q.y + 50)]; }""")
        page.mouse.move(q2[0], q2[1])
        page.mouse.down()
        page.mouse.move(q2[0] + 60, q2[1] + 40, steps=5)
        page.mouse.up()
        page.click("#bdPng")
        page.wait_for_timeout(1500)
        chk("画板：第二块也能存图（不是只有第一次那条路通）",
            (bid2 + ".png") in board_files(), sorted(board_files()))
        page.click("#bdDelB")
        wait("() => !document.getElementById('bdAsk').hidden", timeout=8000)
        page.click("#bdAskAlt button:nth-child(1)")        # 连图一起删
        page.wait_for_timeout(1500)
        chk("画板：选了「连图一起删」，这块的 json 与 png 一起清了",
            bid2 + ".json" not in board_files() and bid2 + ".png" not in board_files(),
            sorted(board_files()))
        chk("画板：删完侧栏回到「这本书还没有画板」，不会留一行点不开的假条目",
            "第三章的那张图" not in state("() => document.getElementById('bdSid').innerText"))

        # 窗口变小：画布跟着改尺寸，画上的东西不重排
        page.click("#bdNewB")
        wait("() => BD.boards.length === 1", timeout=15000)
        page.click('#bdTools button[data-t="rect"]')
        q3 = state("""() => { const q = document.getElementById('bdPaper').getBoundingClientRect();
            return [Math.round(q.x + 40), Math.round(q.y + 40)]; }""")
        page.mouse.move(q3[0], q3[1])
        page.mouse.down()
        page.mouse.move(q3[0] + 70, q3[1] + 50, steps=4)
        page.mouse.up()
        page.wait_for_timeout(400)
        keep = state("() => { const o = BD.fc.getObjects()[0]; return [o.left, o.top, o.width, o.height]; }")
        cw = state("() => Math.round(BD.fc.getWidth())")
        page.set_viewport_size({"width": 1024, "height": 720})
        page.wait_for_timeout(900)
        resize = state("""() => ({w: Math.round(BD.fc.getWidth()),
            objs: BD.fc.getObjects().length,
            left: BD.fc.getObjects()[0] ? BD.fc.getObjects()[0].left : -1,
            paper: Math.round(document.getElementById('bdPaper').clientWidth)})""")
        chk("画板：窗口变小画布跟着变，但画上的东西位置不动",
            resize["w"] < cw and resize["objs"] == 1 and abs(resize["left"] - keep[0]) < 0.6,
            (cw, keep, resize))
        page.set_viewport_size({"width": 1440, "height": 900})
        page.wait_for_timeout(700)
        bd_settled()

        # 溢出 / emoji：窄窗里这一层不许横向溢出
        page.set_viewport_size({"width": 900, "height": 700})
        page.wait_for_timeout(700)
        over = state("""() => {
          const s = document.querySelector('#bdLayer section').getBoundingClientRect();
          return {right: Math.round(s.right), w: innerWidth,
                  doc: document.documentElement.scrollWidth - document.documentElement.clientWidth};
        }""")
        chk("画板：窄窗里这一层不横向溢出",
            over["doc"] <= 1 and over["right"] <= over["w"] + 1, over)
        texts = state("""() => [document.getElementById('bdLayer'), document.getElementById('ntMapLayer')]
            .map(l => l.innerText).join('\\n')""")
        chk("画板与脑图两层里零 emoji", not EMOJI.search(texts),
            [c for c in texts if EMOJI.match(c)][:6])
        page.set_viewport_size({"width": 1440, "height": 900})
        page.wait_for_timeout(500)

        # 关掉画板：没存的先补一笔
        page.click("#bdClose")
        page.wait_for_timeout(1200)
        chk("画板：关闭时把还在防抖里的那次改动 flush 出去了",
            not state("() => document.getElementById('bdLayer').classList.contains('open')")
            and state("() => BD.timer === 0"))
        bd_settled()

        # 换书不串台：另开一本书，画板清单应该是空的
        page.evaluate("() => openReader('SE_COMPILER', '编译原理十二讲', 'shelf')")
        wait("() => NT.book === 'SE_COMPILER'", timeout=15000)
        page.evaluate("() => rdSetPane('notes', true)")
        page.wait_for_timeout(300)
        page.click("#ntBoard")
        wait("() => BD.book === 'SE_COMPILER'", timeout=20000)
        page.wait_for_timeout(1200)
        chk("画板：换到另一本书，看到的是那本书自己的板（空的，不是上一本那几张）",
            state("() => BD.boards.length") == 0
            and sorted(pathlib.Path(selftest.BOOKS, "GAPBOOK1", "boards").glob("*.json"))
            and not list((pathlib.Path(selftest.BOOKS) / "SE_COMPILER" / "boards").glob("*.json")),
            (state("() => BD.boards.length"), state("() => BD.book")))
        page.click("#bdClose")
        page.wait_for_timeout(600)

        clean = [e for e in errors if "favicon" not in e]
        chk("全程零 console 报错", not clean, clean[:4])
        page.screenshot(path=str(selftest.SHOTS / "board-map.png"))
        b.close()

    return finish()


def finish():
    """收尾 + 报数。中途停住时也要走这里：已经跑过的断言是证据，不能因为一条卡住就不报了。"""
    import shutil
    shutil.rmtree(BOARD_DIR, ignore_errors=True)
    (BDir / "mindmap.json").unlink(missing_ok=True)
    (BDir / "notes.md").unlink(missing_ok=True)

    bad = [c for c in checks if not c[0]]
    print()
    print("通过 %d 项，失败 %d 项" % (len(checks) - len(bad), len(bad)))
    for _, name, extra in bad:
        print("  ✗ " + name + ("   " + extra if extra else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        CODE = main()
    except GateStop as stop:
        print("\n 停在：%s（截图 board-map-stuck.png）" % stop)
        CODE = finish()
    except Exception as exc:                      # noqa: BLE001
        # 中途抛异常不能只甩个栈就走：那样看不出跑到哪一条、也看不出结论是没跑完还是跑完了。
        import traceback
        traceback.print_exc()
        print("\n FAIL 套件中途炸了：%s" % str(exc)[:200])
        CODE = finish() or 1
    sys.exit(CODE)
