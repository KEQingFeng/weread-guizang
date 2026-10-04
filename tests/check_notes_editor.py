#!/usr/bin/env python3
"""归藏真机验证：条目编辑器（Markdown 工具条 / 表格发生器 / 引用）+ 模板 + 思维导图 + 导出。

前提：有一个指向沙盒书库的服务，且 seed 铺好了 GAPBOOK1（`python tests/seed.py`）。
服务地址走命令行第一个参数或 GUIZANG_TEST_URL，缺省 8770。
"""
import json
import pathlib
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import selftest  # noqa: E402
from seed import book_dir as where  # noqa: E402
from playwright.sync_api import sync_playwright

BASE = selftest.need_base(1)
BOOK = "GAPBOOK1"
# 书目录按 id 现找：1.0.1 起书库按模块分了文件夹，写死平铺路径的套件会指到空目录。
BDir = pathlib.Path(where(BOOK))
selftest.SHOTS.mkdir(parents=True, exist_ok=True)
SHOTS = [str(selftest.SHOTS / (n + ".png")) for n in ("notes-editor", "notes-template",
                                                "notes-mindmap", "notes-panel")]

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read().decode("utf8"))


def post(path, payload):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf8"))


# 在正文里挑一段干净的字，造一个 Range（不靠鼠标拖选：那条路在 #108 已经验过了）。
RANGE_JS = """([inside, at, len]) => {
  const root = document.querySelector(inside || '#rdBody');
  if (!root) return null;
  const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = w.nextNode())) {
    const raw = n.nodeValue || '';
    const first = raw.search(/\\S/);
    if (first < 0 || raw.trim().length < at + len) continue;
    const s = first + at, e = s + len;
    if (e > raw.length || /\\s/.test(raw.slice(s, e))) continue;
    const r = document.createRange();
    r.setStart(n, s); r.setEnd(n, e);
    const sel = window.getSelection();
    sel.removeAllRanges(); sel.addRange(r);
    return String(sel);
  }
  return null;
}"""


def main():
    post("/api/mynotes", {"book": BOOK, "doc": {"schema": 1, "marks": [], "entries": []}})
    (BDir / "notes.md").unlink(missing_ok=True)
    (BDir / "mindmap.svg").unlink(missing_ok=True)
    s0 = get("/api/mynotes?book=" + BOOK)
    chk("后端：清空后 counts 归零", (s0.get("counts") or {}) == {"marks": 0, "entries": 0}, s0.get("counts"))
    t0 = get("/api/note_tpl")
    official = [i for i in t0.get("items", []) if i.get("official")]
    chk("后端：官方模板 ≥ 4 套", len(official) >= 4, len(official))

    errors = []
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        def ta_value():
            return page.evaluate("() => document.getElementById('ntEdTa').value")

        def set_ta(text, a=None, bb=None):
            page.evaluate("""([t, a, b]) => {
              const ta = document.getElementById('ntEdTa');
              ta.focus(); ta.value = t;
              const s = a == null ? t.length : a, e = b == null ? s : b;
              ta.setSelectionRange(s, e);
              ta.dispatchEvent(new Event('input', {bubbles: true}));
            }""", [text, a, bb])
            page.wait_for_timeout(60)

        def tool(t):
            page.evaluate("""(x) => {
              const b = document.querySelector('#ntEdTools button[data-t="' + x + '"]');
              if (b) b.click();
            }""", t)
            page.evaluate("() => ntEdPreviewNow()")
            page.wait_for_timeout(200)

        def flush():
            """headless Chromium 的动画时钟会整段卡住：transition 建好了却不推进，
            读到的计算值还停在起始值。这里逼两帧，再把在跑的 transition 直接推到终点，
            断言才落在「CSS 规则本身对不对」上，而不是截图时机上。"""
            page.evaluate("""async () => {
              await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
              document.getAnimations().forEach(a => { try { a.finish(); } catch (e) {} });
            }""")
            page.wait_for_timeout(80)

        def settled():
            """存盘是 900ms 防抖 + 一次 POST：等 NT.timer 归零再问后端，别抢跑。"""
            try:
                page.wait_for_function("() => NT.timer === 0", timeout=6000)
            except Exception:
                pass
            page.wait_for_timeout(250)

        def pv():
            return page.evaluate("() => document.getElementById('ntEdPv').innerHTML")

        def layer(cls):
            flush()
            return page.evaluate("""(id) => {
              const l = document.getElementById(id);
              if (!l) return null;
              const s = l.querySelector('section');
              /* opacity 是 transition 属性，卡住的动画时钟会把它留在 0；
                 pointer-events 不走 transition —— 它才如实反映 .open 那条规则生效了。 */
              return {open: l.classList.contains('open'),
                      vis: getComputedStyle(l).pointerEvents === 'auto'
                           && getComputedStyle(l).visibility !== 'hidden',
                      shown: s ? s.getBoundingClientRect().width > 200 : false};
            }""", cls)

        page.goto(BASE + "/", wait_until="networkidle")
        page.evaluate("() => localStorage.clear()")
        page.reload(wait_until="networkidle")
        page.evaluate("() => openReader('%s', '缺口试验本', 'shelf')" % BOOK)
        page.wait_for_selector("#rdBody", timeout=8000)
        page.wait_for_function("() => NT.book === '%s' && NT.doc" % BOOK, timeout=8000)
        chk("前端：这本书的笔记读进来了", True)

        # ── 先落一道真划线，好在正文里点它 → 「引用这句」 ────────
        picked = page.evaluate(RANGE_JS, ["#rdBody", 0, 8])
        made = page.evaluate("""() => {
          const sel = window.getSelection();
          const txt = String(sel);
          if (!txt.trim()) return {ok: false, txt};
          return {ok: ntAdd(sel, txt, {kind: 'highlight', tag: 'quote',
            ch: (RD.chapters[RD.at] || {}).file || '',
            around: ntAround(document.querySelector('#rdBody'), sel)}), txt};
        }""")
        page.wait_for_timeout(500)
        chk("前端：正文里能划出一道线", made["ok"] is True and made["txt"] == picked, (made, picked))
        chk("前端：正文里出现了可点的划线",
            page.evaluate("() => document.querySelectorAll('#rdBody mark.gzmk').length") == 1)

        # ── 笔记栏里那道划线下的「写想法」────────────────────
        # 这颗钮是在 click 里同步把小条 show 出来的，而这一下 click 还会继续冒泡到
        # document 上「点别处就把小条收掉」那条监听 —— 同一个事件里刚摊开就被自己收走，
        # 表现就是「点写想法毫无反应」。这里用真鼠标点（事件照常冒泡），守住这条。
        #
        # 动它之前得先 hover 那一行：动作条平时是 max-height:0 收着的，按钮虽然
        # 有几何尺寸，却整条被裁掉、点不着 —— 直接按收起时的矩形去点会打空。
        #
        # 但只 hover 还不够。划线刚落下去时存盘防抖（900ms）还没跑完，它一落地清单就
        # 重铺一次、行会往上挪 —— 先按旧坐标 hover、隔 460ms 才断言，点的其实是已经挪走
        # 的那一行，动作条当场收回去，套件就报「写想法点不着」。这条假失败实测跑一次红
        # 一次绿，所以先把防抖等干净（settled），再量坐标、再 hover，最后把「点得着」
        # 做成有上限的轮询而不是固定一枪。
        page.evaluate("() => rdSetPane('notes', true)")
        page.wait_for_timeout(200)
        settled()
        row_q = page.evaluate("""() => {
          const r = document.querySelector('#ntList .ntrow[data-mk] .q').getBoundingClientRect();
          return [Math.round(r.x + 40), Math.round(r.y + r.height / 2)];
        }""")
        page.mouse.move(row_q[0], row_q[1])
        page.wait_for_timeout(460)                     # 等动作条浮出来（.18s 展开）
        try:
            page.wait_for_function("""() => {
              const b = document.querySelector('#ntList .ntrow[data-mk] button[data-act=memo]');
              if (!b) return false;
              const r = b.getBoundingClientRect();
              return document.elementFromPoint(Math.round(r.x + r.width / 2),
                                               Math.round(r.y + r.height / 2)) === b;
            }""", timeout=2500)
        except Exception:
            pass                                       # 没等到也照原样断言，让失败如实报出来
        memo_btn = page.evaluate("""() => {
          const b = document.querySelector('#ntList .ntrow[data-mk] button[data-act=memo]');
          if (!b) return null;
          const r = b.getBoundingClientRect();
          const hit = document.elementFromPoint(Math.round(r.x + r.width / 2),
                                                Math.round(r.y + r.height / 2));
          return [Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2),
                  b.textContent.trim(), hit === b];
        }""")
        chk("笔记栏：划到那一行上，下面浮出可点的「写想法」",
            bool(memo_btn) and memo_btn[2] == "写想法" and memo_btn[3], memo_btn)
        pop = None
        if memo_btn:
            page.mouse.click(memo_btn[0], memo_btn[1])
            page.wait_for_timeout(320)
            pop = page.evaluate("""() => {
              const el = document.getElementById('selpop');
              if (!el || !el.classList.contains('show')) return null;
              const r = el.getBoundingClientRect();
              const ta = el.querySelector('.selnote textarea');
              return {q: (el.querySelector('.selnote .q') || {}).textContent || '',
                      val: ta ? ta.value : null,
                      focused: ta ? document.activeElement === ta : false,
                      onscreen: r.width > 120 && r.left >= 0 && r.top >= 0
                                && r.right <= innerWidth + 1 && r.bottom <= innerHeight + 1};
            }""")
        chk("写想法：点一下真摊出小条（没被「点别处」当场收走）", bool(pop), pop)
        chk("写想法：小条上带着这句原文", bool(pop) and pop["q"] == picked, pop)
        chk("写想法：光标已经落进输入框", bool(pop) and pop["focused"], pop)
        chk("写想法：小条整个落在视口里", bool(pop) and pop["onscreen"], pop)

        # 小条底下那排「格子」：七颗标签钮 + 取消 + 记下，全挤在一条 flex 行里会被压到
        # 只剩 27px 宽 —— 两个字的标签只好一个字一行地竖排下来，最右那颗还越出边被
        # overflow:hidden 裁掉。这条量的是真几何：按钮里的文字只许排一行，且右边缘不许
        # 越出父容器的内容盒。量的是几何不是截图，改回挤压态立刻红。
        if pop:
            grid = page.evaluate("""() => {
              const note = document.querySelector('#selpop .selnote');
              return [...note.querySelectorAll('.kinds button, .cancel, .go')].map(el => {
                const r = el.getBoundingClientRect();
                const p = el.parentElement.getBoundingClientRect();
                const rng = document.createRange();
                rng.selectNodeContents(el);
                return {t: el.textContent.trim(), lines: rng.getClientRects().length,
                        over: Math.round(r.right - p.right),
                        clipped: el.scrollWidth > el.clientWidth + 1};
              });
            }""")
            bad = [g for g in grid if g["lines"] > 1 or g["over"] > 1 or g["clipped"]]
            chk("写想法：标签与取消/记下各占一格、都排一行（没竖排、没裁掉）",
                bool(grid) and not bad, bad)

        if pop:
            page.fill("#selpop .selnote textarea", "这一句是要点，回头写进卡片。")
            page.evaluate("() => document.querySelector('#selpop .selnote .go').click()")
            page.wait_for_timeout(300)
            settled()
            chk("写想法：存完小条自己收起",
                page.evaluate("() => { const e = document.getElementById('selpop');"
                              "return !e || !e.classList.contains('show'); }"))
            chk("写想法：那一行上显出这句想法",
                page.evaluate("""() => {
                  const m = document.querySelector('#ntList .ntrow[data-mk] .memo');
                  return m ? m.textContent : '';
                }""") == "这一句是要点，回头写进卡片。")
            chk("写想法：钮改口叫「改想法」",
                page.evaluate("""() => {
                  const b = document.querySelector('#ntList .ntrow[data-mk] button[data-act=memo]');
                  return b ? b.textContent.trim() : '';
                }""") == "改想法")
            mks = ((get("/api/mynotes?book=" + BOOK).get("doc") or {}).get("marks") or [])
            chk("写想法：跟着这一笔落盘到书文件夹",
                bool(mks) and mks[0].get("memo") == "这一句是要点，回头写进卡片。", mks[:1])

            # 再走一遍「改想法」：这回得把已经存下的那句捞回输入框
            page.mouse.move(row_q[0], row_q[1])
            page.wait_for_timeout(460)
            again = page.evaluate("""() => {
              const b = document.querySelector('#ntList .ntrow[data-mk] button[data-act=memo]');
              const r = b.getBoundingClientRect();
              return [Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2),
                      b.textContent.trim()];
            }""")
            page.mouse.click(again[0], again[1])
            page.wait_for_timeout(320)
            chk("写想法：钮确实改口叫「改想法」", again[2] == "改想法", again)
            chk("写想法：改的时候把存过的那句捞回输入框",
                page.evaluate("""() => {
                  const ta = document.querySelector('#selpop .selnote textarea');
                  return ta ? ta.value : '';
                }""") == "这一句是要点，回头写进卡片。")
            # 草稿没存就滚一下：正文一滚原本会无条件把小条收走，写到一半的那句当场没了。
            # 现在认「正在小条里输入/鼠标还压在小条上」= 别收（selPopBusy）。
            page.evaluate("() => document.querySelector('#selpop .selnote textarea').focus()")
            # 这时候正文来一发 scroll（宽窗是正文自己滚、窄窗是整页滚，两种都发一遍）。
            # 要验的是「滚一下就把草稿收走」那个无条件 selPopHide，跟滚没滚得动无关。
            page.evaluate("""() => {
              const sc = document.querySelector('#rdScroll');
              if (sc) { sc.scrollTop = sc.scrollTop + 220; sc.dispatchEvent(new Event('scroll')); }
              (document.scrollingElement || document.documentElement)
                .dispatchEvent(new Event('scroll'));
            }""")
            page.wait_for_timeout(400)
            kept = page.evaluate("""() => {
              const el = document.getElementById('selpop');
              const ta = el && el.querySelector('.selnote textarea');
              return {open: !!(el && el.classList.contains('show')),
                      val: ta ? ta.value : ''};
            }""")
            chk("写想法：滚一下正文不会把写了一半的那句收走",
                kept["open"] and kept["val"] == "这一句是要点，回头写进卡片。", kept)
            page.evaluate("() => selPopHide()")
        else:
            chk("写想法：小条都没开，后面几条存盘断言一并记失败", False, "no popup")

        # ── 键盘与撤销：把笔记行当列表用 ──────────────────
        # 行是 tabindex="0" 的，Tab 能停在它上面；停上去按什么都不能是白按。
        # 回车跳回正文那一句、E 改想法、Delete 删除 —— 三条都得真有效果。
        # 每条都等「效果真的出现」再断言：满负载跑整轮门禁时，固定 sleep 会不够
        # （小条与提示都是进场动画，元素在动画走完之前还点不着、也读不到）。
        def wait_js(expr, timeout=8000):
            try:
                page.wait_for_function(expr, timeout=timeout)
                return True
            except Exception:
                return False

        mkid = page.evaluate("() => document.querySelector('#ntList .ntrow[data-mk]').dataset.mk")
        page.evaluate("""() => { NTLAST.mark = ''; NTLAST.text = '';
          document.querySelector('#ntList .ntrow[data-mk]').focus(); }""")
        page.keyboard.press("Enter")
        wait_js("() => NTLAST.mark === '%s'" % mkid)
        chk("笔记栏：行上按回车 = 跳回正文那一句",
            page.evaluate("() => NTLAST.mark") == mkid, page.evaluate("() => NTLAST.mark"))
        page.evaluate("""() => { selPopHide();
          document.querySelector('#ntList .ntrow[data-mk]').focus(); }""")
        page.keyboard.press("e")
        wait_js("() => { const e = document.getElementById('selpop');"
                " return !!(e && e.classList.contains('show')"
                " && e.querySelector('.selnote textarea')); }")
        kpop = page.evaluate("""() => {
          const el = document.getElementById('selpop');
          const ta = el && el.querySelector('.selnote textarea');
          return {open: !!(el && el.classList.contains('show')), val: ta ? ta.value : null};
        }""")
        chk("笔记栏：行上按 E = 改想法（小条带着旧想法开出来）",
            kpop["open"] and kpop["val"] == "这一句是要点，回头写进卡片。", kpop)

        # 删完给一次反悔：右下角那条提示上挂颗「撤销」，点一下原样放回。
        page.evaluate("() => selPopHide()")
        n0 = page.evaluate("() => (NT.doc.marks || []).length")
        page.evaluate("() => document.querySelector('#ntList .ntrow[data-mk]').focus()")
        page.keyboard.press("Delete")
        wait_js("() => { const b = document.querySelector('.whisper .wa');"
                " return !!b && (NT.doc.marks || []).length === %d; }" % (n0 - 1))
        ds = page.evaluate("""() => {
          const b = document.querySelector('.whisper .wa');
          return {rows: document.querySelectorAll('#ntList .ntrow[data-mk]').length,
                  marks: (NT.doc.marks || []).length, label: b ? b.textContent.trim() : ''};
        }""")
        chk("笔记栏：行上按删除 = 删掉这条，且给一次「撤销」",
            ds["marks"] == n0 - 1 and ds["rows"] == 0 and ds["label"] == "撤销", ds)
        # 先把这一刀的存盘等干净（900ms 防抖 + 一次往返）再量坐标。不等的话，防抖一过
        # 请求就出发、清单在回执里重铺一次，撤销钮会跟着挪位 —— 整轮门禁里这条偶发红
        # 过一次（marks/rows/ink 全 0）。回执在半路把撤销抹掉这件事另有一条专门的断言
        # （往下几条），这里不该靠运气区分两种原因。
        settled()
        # 那颗钮得真的点得着：.whisper 是淡入进场（visibility 参与过渡），动画还没走完
        # 的时候 elementFromPoint 会从它身上穿过去。所以等它可命中了再点。
        hittable = wait_js("""() => {
          const b = document.querySelector('.whisper .wa');
          if (!b) return false;
          const r = b.getBoundingClientRect();
          return document.elementFromPoint(Math.round(r.x + r.width / 2),
                                           Math.round(r.y + r.height / 2)) === b;
        }""")
        ub = page.evaluate("""() => {
          const b = document.querySelector('.whisper .wa');
          if (!b) return null;
          const r = b.getBoundingClientRect();
          const hit = document.elementFromPoint(Math.round(r.x + r.width / 2),
                                               Math.round(r.y + r.height / 2));
          return {ok: hit === b, over: r.top < 0 || r.bottom > innerHeight};
        }""")
        chk("撤销钮是真能点到的（没被 whisper 的 pointer-events 穿过去）",
            hittable and bool(ub) and ub["ok"] and not ub["over"], ub)
        if ub and ub["ok"]:
            box = page.evaluate("""() => {
              const r = document.querySelector('.whisper .wa').getBoundingClientRect();
              return [Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2)];
            }""")
            page.mouse.click(box[0], box[1])
            wait_js("() => (NT.doc.marks || []).length === %d" % n0)
        back = page.evaluate("""() => ({marks: (NT.doc.marks || []).length,
          rows: document.querySelectorAll('#ntList .ntrow[data-mk]').length,
          ink: document.querySelectorAll('#rdBody mark.gzmk').length})""")
        chk("笔记栏：点「撤销」把划线与正文里的墨迹都放回去",
            back["marks"] == n0 and back["rows"] == 1 and back["ink"] == 1, back)

        # ── 撤销撞上「还在路上的存盘回执」──────────────────────────
        # 删掉那一刀过 900ms 会发一次存盘，回执里带的是出发那一刻的快照（里面没这道线）。
        # 早先的写法拿回执无条件覆盖本地，于是「回执在半路落地、撤销在它之后点下去」这一串
        # 时序会把刚放回来的线又抹掉：界面上凭空少一条，运气差时后一笔存盘还会把「没有」
        # 写进书文件夹。现在按版本号认回执。这里把那一笔的回执人为压住 1.5 秒（延迟放在
        # 回执这一侧 —— 请求必须照原样先出发，否则出去的那一份已经带着撤销之后的线，
        # 就测不出这回事了），把竞态固定摆出来，不再靠整轮里偶发。
        page.evaluate("""() => {
          window.__held = 0;
          const of = window.fetch;
          window.fetch = function (...a) {
            const body = (a[1] || {}).body;
            if (String(a[0]).indexOf('/api/mynotes') < 0 || !body || window.__held) return of(...a);
            window.__held = 1;
            const p = of.apply(this, a);
            return new Promise(res => setTimeout(() => res(p), 1500));
          };
        }""")
        page.evaluate("() => document.querySelector('#ntList .ntrow[data-mk]').focus()")
        page.keyboard.press("Delete")
        sent = wait_js("() => window.__held === 1", 6000)       # 这一刀的存盘已出发、回执还悬着
        box2 = page.evaluate("""() => {
          const b = document.querySelector('.whisper .wa');
          if (!b) return null;
          const r = b.getBoundingClientRect();
          return [Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2)];
        }""")
        if sent and box2:
            page.mouse.click(box2[0], box2[1])
        just = page.evaluate("() => (NT.doc && NT.doc.marks || []).length")
        page.wait_for_timeout(2200)                            # 让压住的那份回执落地
        after = page.evaluate("""() => ({
          marks: (NT.doc && NT.doc.marks || []).length,
          rows: document.querySelectorAll('#ntList .ntrow[data-mk]').length,
          ink: document.querySelectorAll('#rdBody mark.gzmk').length})""")
        settled()
        server = get("/api/mynotes?book=" + BOOK).get("counts") or {}
        chk("撤销不会被半路的存盘回执抹掉（回执按版本号认）",
            bool(sent) and bool(box2) and just == n0 and after["marks"] == n0
            and after["rows"] == 1 and after["ink"] == 1 and server.get("marks") == n0,
            {"sent": sent, "just": just, "after": after, "server": server})

        # ── 开编辑器 ─────────────────────────────────────
        page.evaluate("() => document.querySelector('#rdBody mark.gzmk').click()")
        page.wait_for_timeout(260)
        last = page.evaluate("() => ({t: NTLAST.text, m: NTLAST.mark})")
        chk("前端：点正文那道线就记住了「这句」", last["t"] == picked and last["m"], last)

        page.evaluate("() => document.querySelector('.nttabs button[data-tab=entries]').click()")
        page.wait_for_timeout(200)
        page.evaluate("() => document.getElementById('ntNew').click()")
        page.wait_for_timeout(420)
        st = layer("ntEdLayer")
        chk("前端：「写条目」开出编辑浮层", st and st["open"] and st["vis"] and st["shown"], st)
        chk("前端：浮层在最上面（盖住 #veil）",
            page.evaluate("() => +getComputedStyle(document.getElementById('ntEdLayer')).zIndex") > 60)
        chk("前端：工具条一排钮都摆出来了",
            page.evaluate("() => document.querySelectorAll('#ntEdTools button').length") >= 18)
        mtop = page.evaluate("""() => [...document.querySelectorAll('#ntEdTools button[data-m]')]
            .map(b => Math.round(b.getBoundingClientRect().top))""")
        chk("工具：三种看法没被换行拆开", len(set(mtop)) == 1 and len(mtop) == 3, mtop)
        seps = page.evaluate("""() => [...document.querySelectorAll('#ntEdTools .sep')]
            .map(s => Math.round(s.getBoundingClientRect().width))""")
        chk("工具：分组分隔是一根细线，不是一块灰",
            seps and max(seps) <= 2, seps)
        chk("前端：套上编辑器时正文仍是这本书",
            page.evaluate("() => NT.book === '%s' && NT.lock === true" % BOOK))
        seeded = page.evaluate("() => ({n: NTED.refs.length, t: (NTED.refs[0]||{}).text || ''})")
        chk("前端：刚点的那句自动引了进来", seeded["n"] == 1 and seeded["t"] == picked, seeded)
        chk("前端：引用条显示在编辑框下面",
            page.evaluate("() => document.querySelectorAll('#ntEdRefs .edref').length") == 1)
        set_ta("先写一行占位，看看编辑器和工具条排版。")
        page.wait_for_timeout(120)
        page.screenshot(path=SHOTS[3])

        # ── Markdown 工具条 ───────────────────────────────
        set_ta("神经网络与深度学习", 0, 4)
        tool("bold")
        chk("工具：加粗把选中的字包住", ta_value().startswith("**神经网络**"), ta_value())
        tool("bold")
        chk("工具：再点一次取消加粗", ta_value() == "神经网络与深度学习", ta_value())
        tool("ital"); tool("ital")
        tool("strike")
        chk("工具：删除线 ~~ ~~", "~~神经网络~~" in ta_value(), ta_value())
        tool("strike")
        tool("code")
        chk("工具：行内代码 ` `", "`神经网络`" in ta_value(), ta_value())
        tool("code")
        for n in range(1, 6):
            tool("h%d" % n)
            chk("工具：H%d 加在行首" % n, ta_value().startswith("#" * n + " "), ta_value())
            tool("h%d" % n)
        chk("工具：五级标题都能取消回原样", ta_value() == "神经网络与深度学习", ta_value())

        set_ta("第一行\n第二行\n第三行", 0, 9)
        tool("bull")
        chk("工具：一次给三行加列表号",
            ta_value() == "- 第一行\n- 第二行\n- 第三行", repr(ta_value()))
        tool("bull")
        set_ta("甲\n乙", 0, 3)
        tool("num")
        chk("工具：编号按 1. 2. 递增", ta_value() == "1. 甲\n2. 乙", repr(ta_value()))
        tool("quote")
        chk("工具：行首从编号换成引用号（不会两个都留着）",
            ta_value() == "> 甲\n> 乙", repr(ta_value()))
        tool("quote")
        chk("工具：再点一次取消引用号", ta_value() == "甲\n乙", repr(ta_value()))
        tool("bull")
        chk("工具：无序列表 - ", ta_value() == "- 甲\n- 乙", repr(ta_value()))
        tool("bull")
        chk("工具：几种行首来回切不留残渣", ta_value() == "甲\n乙", repr(ta_value()))

        tool("hr")
        chk("工具：分隔线整段插入", "---" in ta_value().split("\n"), repr(ta_value()))
        set_ta("", 0, 0)
        tool("fence")
        chk("工具：代码块 ``` 包裹",
            ta_value().strip().startswith("```") and ta_value().strip().endswith("```"),
            repr(ta_value()))
        set_ta("点这里", 0, 3)
        tool("link")
        chk("工具：链接 []()", ta_value() == "[点这里](https://)", ta_value())

        # 列表回车续行
        set_ta("- 一", 3, 3)
        page.keyboard.press("Enter")
        page.keyboard.type("二")
        page.wait_for_timeout(120)
        chk("工具：回车自动接着上一项列表", ta_value() == "- 一\n- 二", repr(ta_value()))
        page.keyboard.press("Enter")
        page.wait_for_timeout(100)
        chk("工具：续行时行首自己补上", ta_value() == "- 一\n- 二\n- ", repr(ta_value()))
        page.keyboard.press("Enter")
        page.wait_for_timeout(150)
        chk("工具：空项再回车收掉列表（不留双空行）",
            ta_value().rstrip() == "- 一\n- 二" and not ta_value().endswith("\n\n"),
            repr(ta_value()))

        # ── 表格发生器 ───────────────────────────────────
        set_ta("", 0, 0)
        tool("table")
        pop = page.evaluate("""() => {
          const p = document.querySelector('#ntEdTools .edpop');
          return p ? {open: p.classList.contains('open'), cells: p.querySelectorAll('.tb i').length} : null;
        }""")
        chk("表格：点「表格」弹出 m×n 格子", pop and pop["open"] and pop["cells"] == 48, pop)
        page.evaluate("""() => {
          const cells = document.querySelectorAll('#ntEdTools .edpop .tb i');
          cells[(3 - 1) * 8 + (2 - 1)].dispatchEvent(new MouseEvent('mouseenter'));
        }""")
        page.wait_for_timeout(80)
        cap = page.evaluate("() => document.querySelector('#ntEdTools .edpop .cap').textContent")
        chk("表格：光标指哪儿就报几乘几", "3" in cap and "2" in cap, cap)
        page.evaluate("""() => {
          const cells = document.querySelectorAll('#ntEdTools .edpop .tb i');
          cells[(3 - 1) * 8 + (2 - 1)].click();
        }""")
        page.wait_for_timeout(220)
        v = ta_value()
        chk("表格：插进的是 3 行 × 2 列的 Markdown",
            v.count("\n") >= 2 and v.split("\n")[0].count("|") == 3, repr(v))
        chk("表格：弹出层收起",
            page.evaluate("() => { const p=document.querySelector('#ntEdTools .edpop'); return p && !p.classList.contains('open'); }"))
        ths = page.evaluate("() => document.querySelectorAll('#ntEdPv table th').length")
        trs = page.evaluate("() => document.querySelectorAll('#ntEdPv table tr').length")
        chk("表格：预览区渲染成 3 行 × 2 列的 <table>",
            "<table>" in pv() and ths == 2 and trs == 3, (ths, trs, pv()[:120]))

        # ── 预览 / 分栏 ──────────────────────────────────
        set_ta("# 标题\n\n正文**加粗**\n\n- 一\n- 二\n")
        page.evaluate("() => document.querySelector('#ntEdTools button[data-m=split]').click()")
        page.wait_for_timeout(320)
        flush()
        split = page.evaluate("""() => {
          const body = document.getElementById('ntEdBody');
          const g = getComputedStyle(body).gridTemplateColumns.split(' ');
          return {cls: body.className, a: g[0], b: g[1]};
        }""")
        chk("预览：边写边看时两栏都占地方",
            "split" in split["cls"] and float(split["b"].replace("px", "")) > 200, split)
        chk("预览：Markdown 真渲染出 h1 / strong / ul",
            all(x in pv() for x in ["<h1>", "<strong>", "<li>"]), pv()[:200])
        fill = page.evaluate("""() => {
          const box = document.getElementById('ntEdBody').getBoundingClientRect();
          const ta = document.getElementById('ntEdTa').getBoundingClientRect();
          const pv = document.getElementById('ntEdPv').getBoundingClientRect();
          return {box: Math.round(box.height), ta: Math.round(ta.height), pv: Math.round(pv.height)};
        }""")
        chk("版式：两栏铺满浮层，不是缩在顶上的一小截",
            fill["ta"] >= fill["box"] * 0.9 and fill["pv"] >= fill["box"] * 0.9, fill)
        page.evaluate("() => document.querySelector('#ntEdTools button[data-m=view]').click()")
        page.wait_for_timeout(650)
        flush()
        chk("预览：只看时输入栏让位",
            page.evaluate("""() => {
              const b = document.getElementById('ntEdBody');
              return b.classList.contains('view')
                     && getComputedStyle(b).gridTemplateColumns.split(' ')[0] === '0px';
            }"""))
        page.evaluate("() => document.querySelector('#ntEdTools button[data-m=split]').click()")
        page.wait_for_timeout(200)

        # ── 标签色 + 引用这句 ─────────────────────────────
        page.evaluate("() => document.querySelectorAll('#ntEdTools .dot')[1].click()")
        page.wait_for_timeout(120)
        chk("标签：点第二支笔就归类了",
            page.evaluate("() => NTED.tag") and page.evaluate(
                "() => document.querySelectorAll('#ntEdTools .dot.on').length") == 1,
            page.evaluate("() => NTED.tag"))
        chk("标签：无标那颗钮在排里",
            page.evaluate("() => !!document.querySelector('#ntEdTools button[data-t=tag0]')"))

        # ── 存条目 ───────────────────────────────────────
        page.fill("#ntEdTitle", "关于神经网络的一条")
        page.evaluate("() => document.getElementById('ntEdSave').click()")
        page.wait_for_timeout(500)
        settled()
        closed = layer("ntEdLayer")
        chk("保存：浮层收起来了", closed and not closed["open"], closed)
        s1 = get("/api/mynotes?book=" + BOOK)
        chk("保存：counts.entries = 1", (s1.get("counts") or {}).get("entries") == 1, s1.get("counts"))
        ent = (s1.get("doc") or {}).get("entries") or []
        chk("保存：盘上 notes.json 里真有这条",
            bool(ent) and ent[0]["title"] == "关于神经网络的一条"
            and len(ent[0].get("refs") or []) == 1
            and ent[0]["refs"][0]["text"] == picked, ent[:1])
        chk("保存：条目带上了标签", bool(ent and ent[0].get("tag")), ent[:1])
        rows = page.evaluate("""() => [...document.querySelectorAll('#ntList .ntrow')].map(el => ({
              en: el.dataset.en || '',
              title: (el.querySelector('.q')||{}).textContent || '',
              peek: (el.querySelector('.pvs')||{}).innerHTML || '',
              rf: el.querySelectorAll('.rf').length,
              acts: [...el.querySelectorAll('.nacts button')].map(b => b.dataset.act)}))""")
        chk("列表：条目行渲染出 Markdown 预览",
            bool(rows) and "<h1>" in rows[0]["peek"], rows[:1])
        chk("列表：条目行上有引用 chip 和「接着写」",
            rows and rows[0]["rf"] == 1 and "edit" in rows[0]["acts"], rows[:1])
        chk("列表：条目计数写进页签",
            page.evaluate("() => document.getElementById('ntCE').textContent") == "1")

        # 引用 chip 点一下翻回正文
        page.evaluate("() => document.querySelector('#ntList .ntrow .rf').click()")
        page.wait_for_timeout(600)
        chk("互跳：点条目的引用 chip 定位到正文那句",
            page.evaluate("() => !!document.querySelector('#rdBody mark.gzmk.flash')"))

        # 接着写
        page.evaluate("() => document.querySelector('#ntList .ntrow button[data-act=edit]').click()")
        page.wait_for_timeout(420)
        re_open = page.evaluate("""() => ({on: NTED.on, id: NTED.id,
              title: document.getElementById('ntEdTitle').value,
              body: document.getElementById('ntEdTa').value.slice(0, 6),
              refs: NTED.refs.length, tag: NTED.tag})""")
        chk("编辑：「接着写」把原文捞回来",
            re_open["on"] and re_open["title"] == "关于神经网络的一条"
            and re_open["body"].startswith("# 标题") and re_open["refs"] == 1
            and re_open["tag"], re_open)

        # 脏稿保护
        page.evaluate("() => { const ta=document.getElementById('ntEdTa'); ta.value += '\\n改了一下'; "
                      "ta.dispatchEvent(new Event('input',{bubbles:true})); }")
        page.evaluate("() => document.getElementById('ntEdClose').click()")
        page.wait_for_timeout(220)
        chk("关闭：没存的稿子第一下不放走",
            page.evaluate("() => NTED.on")
            and "还没存" in page.evaluate(
                "() => (document.querySelector('.whisper')||{}).textContent || ''"),
            page.evaluate("() => (document.querySelector('.whisper')||{}).textContent"))
        page.evaluate("() => document.getElementById('ntEdClose').click()")
        page.wait_for_timeout(260)
        chk("关闭：再点一次才真的不收这篇了", page.evaluate("() => !NTED.on"))

        # ── 模板 ─────────────────────────────────────────
        page.evaluate("() => document.getElementById('ntNew').click()")
        page.wait_for_timeout(300)
        page.evaluate("() => document.querySelector('#ntEdTools button[data-t=tpl]').click()")
        page.wait_for_timeout(700)
        ts = layer("ntTplLayer")
        chk("模板：抽屉开出来了", ts and ts["open"] and ts["vis"], ts)
        page.screenshot(path=SHOTS[1])
        tplrows = page.evaluate("""() => [...document.querySelectorAll('#ntTplList .tplrow')].map(r => ({
            name: (r.querySelector('.tn')||{}).textContent || '',
            badge: (r.querySelector('.badge')||{}).textContent || '',
            del: !!r.querySelector('.del')}))""")
        chk("模板：官方五套都列着且没有删除钮",
            len(tplrows) == len(official) and all(r["badge"] == "官方" and not r["del"] for r in tplrows),
            tplrows)

        page.evaluate("""() => {
          const rows = [...document.querySelectorAll('#ntTplList .tplrow')];
          (rows.find(r => r.textContent.indexOf('费曼') >= 0) || rows[0]).click();
        }""")
        page.wait_for_timeout(800)
        applied = page.evaluate("""() => ({on: NTED.on, tpl: NTED.tpl,
              body: document.getElementById('ntEdTa').value,
              title: document.getElementById('ntEdTitle').value})""")
        chk("模板：点一下就插进编辑框",
            "它是什么" in applied["body"] or applied["body"].strip(), applied["body"][:80])
        chk("模板：套过哪一套记在条目上",
            applied["tpl"] and applied["tpl"].startswith("official-"), applied["tpl"])
        chk("模板：抽屉收了、编辑器还在",
            page.evaluate("() => !document.getElementById('ntTplLayer').classList.contains('open')")
            and applied["on"])

        # 存为自己的模板
        page.evaluate("() => document.getElementById('ntTplNew').click()")
        page.wait_for_timeout(300)
        chk("模板：「存为模板」表单开在抽屉里",
            page.evaluate("() => !!document.querySelector('#ntTplList .tplform')"))
        page.fill("#tplName", "我的回炉模板")
        page.fill("#tplDesc", "把没懂的再走一遍")
        page.evaluate("() => document.getElementById('tplGo').click()")
        page.wait_for_timeout(900)
        t1 = get("/api/note_tpl")
        mine = [i for i in t1["items"] if not i.get("official")]
        chk("模板：存完在列表里能看到「我的」",
            bool(mine) and mine[0]["name"] == "我的回炉模板", mine[:1])
        chk("模板：存完列表自动重铺",
            page.evaluate("() => [...document.querySelectorAll('#ntTplList .badge')]"
                          ".some(b => b.textContent === '我的')"))
        page.evaluate("""() => { const d = document.querySelector('#ntTplList .tplrow .del'); if (d) d.click(); }""")
        page.wait_for_timeout(900)
        t2 = get("/api/note_tpl")
        chk("模板：删掉自定义那套（官方的不许删）",
            not [i for i in t2["items"] if not i.get("official")]
            and len(t2["items"]) == len(official), t2["items"])
        page.evaluate("() => document.getElementById('ntTplClose').click()")
        page.wait_for_timeout(200)

        # 存这条套了模板的条目
        page.fill("#ntEdTitle", "费曼四步：反向传播")
        page.evaluate("() => document.getElementById('ntEdSave').click()")
        page.wait_for_timeout(400)
        settled()
        s2 = get("/api/mynotes?book=" + BOOK)
        chk("保存：第二条条目也落盘（共 2 条）",
            (s2.get("counts") or {}).get("entries") == 2, s2.get("counts"))

        # ── 思维导图 ─────────────────────────────────────
        # #153 把这一层从「只读一张 SVG」换成了可编辑画布：框是 div、边是 svg，
        # 旧断言里的 ntMapImg / ntMapCut / 打开就自动落盘的 mindmap.svg 都不存在了。
        # 加节点、连线、四种形态这些动作归 tests/check_board_map_ui.py 精查，
        # 这里只守笔记栏这条入口：开得出画布、后端仍然出得了图、开一层不许动盘。
        settled()
        map_files = lambda: sorted(p.name for p in BDir.glob("mindmap.*"))  # noqa: E731
        before = map_files()
        page.evaluate("() => document.getElementById('ntMap').click()")
        page.wait_for_timeout(1500)
        ms = layer("ntMapLayer")
        chk("导图：浮层开出来了", ms and ms["open"] and ms["vis"], ms)
        canvas = page.evaluate("""() => ({
          forms: [...document.querySelectorAll('#mmForm button')].map(b => b.dataset.f),
          box: !!document.getElementById('mmNodes'),
          edges: !!document.getElementById('mmEdges'),
          dl: !!document.getElementById('ntMapDl'),
          info: document.getElementById('ntMapInfo').textContent,
        })""")
        chk("导图：开出来的是能编辑的那张画布（四种形态 + 框层 + 边层 + 存 SVG 的钮）",
            len(canvas["forms"]) == 4 and canvas["box"] and canvas["edges"]
            and canvas["dl"], canvas)
        svg = get("/api/mindmap?book=" + BOOK + "&mode=svg")
        chk("导图：后端那份 SVG 是能看的图（有头、画了东西、没 NaN）",
            svg.get("ok") and "<svg" in (svg.get("svg") or "")
            and "NaN" not in (svg.get("svg") or ""), str(svg)[:160])
        page.screenshot(path=SHOTS[2])
        chk("导图：光打开这一层不该往书文件夹里写东西（存不存由用户那一拍决定）",
            map_files() == before, (before, map_files()))
        page.evaluate("() => document.getElementById('ntMapClose').click()")
        page.wait_for_timeout(220)
        chk("导图：关掉后浮层收干净",
            page.evaluate("() => !document.getElementById('ntMapLayer').classList.contains('open')"))

        # ── 导出 notes.md ────────────────────────────────
        page.evaluate("() => document.getElementById('ntExp').click()")
        page.wait_for_timeout(1800)
        md = BDir / "notes.md"
        chk("导出：notes.md 落在书文件夹里", md.exists())
        txt = md.read_text(encoding="utf8") if md.exists() else ""
        chk("导出：划线和条目都进去了",
            picked in txt and "关于神经网络的一条" in txt and "费曼四步：反向传播" in txt,
            txt[:160])
        chk("导出：状态栏用人话说清了去处",
            "已导出" in page.evaluate("() => document.getElementById('ntStatus').textContent"),
            page.evaluate("() => document.getElementById('ntStatus').textContent"))
        chk("导出：后端记下导出过（exported 有值）",
            bool(get("/api/mynotes?book=" + BOOK).get("exported")))

        # ── Esc 分层 / 溢出 / emoji ──────────────────────
        page.evaluate("() => document.getElementById('ntNew').click()")
        page.wait_for_timeout(300)
        page.evaluate("() => document.getElementById('ntMap').click()")
        page.wait_for_timeout(700)
        page.keyboard.press("Escape")
        page.wait_for_timeout(320)
        chk("Esc：先关最上面那层（导图），编辑器不受惊",
            page.evaluate("() => !document.getElementById('ntMapLayer').classList.contains('open')")
            and page.evaluate("() => NTED.on"))
        page.keyboard.press("Escape")
        page.wait_for_timeout(320)
        chk("Esc：再按一下收编辑器（空稿没改过，一次就走）",
            page.evaluate("() => !NTED.on")
            and page.evaluate("() => !document.getElementById('ntEdLayer').classList.contains('open')"))

        page.screenshot(path=SHOTS[0], full_page=False)
        dom_emo = page.evaluate("""() => {
          const t = document.body.innerText || '';
          return [...t].filter(c => {
            const p = c.codePointAt(0);
            return (p >= 0x1F000 && p <= 0x1FAFF) || (p >= 0x2600 && p <= 0x27BF);
          }).slice(0, 8);
        }""")
        chk("界面：屏幕上一个 emoji 也没有", not dom_emo, dom_emo)
        of = page.evaluate("""() => {
          const de = document.documentElement;
          return {w: de.scrollWidth, cw: de.clientWidth,
                  bad: [...document.querySelectorAll('.ntlay')].map(l => l.getBoundingClientRect().width)};
        }""")
        chk("版式：整页没有横向溢出", of["w"] <= of["cw"] + 1, of)
        chk("运行期：控制台没有报错", not errors, errors[:3])
        page.wait_for_timeout(400)
        b.close()

    # ── 收尾：源码级检查 ────────────────────────────────
    src = (selftest.REPO / "ui.html").read_text(encoding="utf8")
    # 源码里唯一允许出现 emoji 的地方是剪藏正文的清洗正则（STRIP），界面上一颗都不该有。
    body = "\n".join(l for l in src.split("\n") if "STRIP = " not in l)
    emo = [c for c in body if 0x1F000 <= ord(c) <= 0x1FAFF or 0x2600 <= ord(c) <= 0x27BF]
    chk("界面：UI 源码零 emoji", not emo, emo[:10])
    dupes = {}
    for name in ["ntEdPreviewNow", "ntEntryRow", "ntEdOpen", "ntEdClose", "ntMapOpen",
                 "ntExportNotes", "ntFloatsWire", "edTool", "edWrap", "ntLastSeed",
                 "ntEntryPeek", "ntTplRefresh", "ntTplPaint"]:
        dupes[name] = src.count("function " + name + "(")
    chk("代码：关键函数没有重名覆盖", all(v == 1 for v in dupes.values()),
        {k: v for k, v in dupes.items() if v != 1})

    bad = [c for c in checks if not c[0]]
    print("\n%d/%d 通过" % (len(checks) - len(bad), len(checks)))
    for _, n, x in bad:
        print("  ✗ " + n + ("  | " + x if x else ""))
    print("截图：" + " ".join(SHOTS))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
