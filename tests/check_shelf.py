"""书架交互 + 叠卡模式 + 详情页瘦身的真机断言。

跑之前先 `python tests/seed.py` 铺好书，或者由 run_all.sh 代劳。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import selftest  # noqa: E402
from playwright.sync_api import sync_playwright

URL = selftest.need_base(1) + "/"
FAIL = []
selftest.SHOTS.mkdir(parents=True, exist_ok=True)
SHOTS = [str(selftest.SHOTS / (n + ".png")) for n in ("grid", "stack", "detail", "reader")]


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if not cond:
        FAIL.append(name)


def flush():
    """headless 的动画时钟会卡住：transition 不推进，读到的还是起始值。
    逼两帧再把在跑的动画推到终点，断言才落在 CSS 本身。"""
    page.evaluate("""async () => {
      await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
      document.getAnimations().forEach(a => { try { a.finish(); } catch (e) {} });
    }""")
    page.wait_for_timeout(80)


def blank_point():
    """找一个「点了不会碰到任何控件」的空白点。书架一屏被卡铺满，直接按坐标
    很容易误撞到页头的「去搜索找书」或某张卡 —— 那就不是在测「点别处收回」了。
    工具条那一排右边是空的，从那儿找最稳。"""
    return page.evaluate("""() => {
      const bar = document.querySelector('.vpane:not([hidden]) .stools');
      if (!bar) return null;
      const r = bar.getBoundingClientRect();
      for (let x = r.right - 8; x > r.left + 8; x -= 12) {
        const y = r.top + r.height / 2;
        const e = document.elementFromPoint(x, y);
        if (e === bar) return [Math.round(x), Math.round(y)];
      }
      return null;
    }""")


def rects(sel):
    flush()
    return page.evaluate("""(s) => [...document.querySelectorAll(s)].map(e => {
      const r = e.getBoundingClientRect();
      return {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
              w: Math.round(r.width), cls: e.className};
    })""", sel)


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(URL, wait_until="networkidle")
    page.wait_for_timeout(900)

    # ── 1. 瀑布那面：样式切换器在、默认是瀑布 ──────────────────
    chk("工具条：有瀑布 / 叠卡切换", page.locator("#sMode button").count() == 2)
    on = page.evaluate("""() => {
      const b = [...document.querySelectorAll('#sMode button')];
      return (b.find(x => x.classList.contains('on')) || {}).dataset?.m || '';
    }""")
    chk("默认样式是瀑布", on == "grid", on)

    cards = rects(".sgrid > .wcard")
    chk("瀑布：卡片排成网格", len(cards) >= 3, len(cards))
    tops = sorted({c["y"] for c in cards})
    chk("瀑布：多于一行（上下瀑布）", len(tops) >= 2, tops)
    page.screenshot(path=SHOTS[0])

    # ── 2. 掠过只给动效，点一下才出动作条 ─────────────────────
    first = cards[0]
    page.mouse.move(first["x"], first["y"])
    page.wait_for_timeout(420)
    st = page.evaluate("""() => {
      const c = document.querySelector('.sgrid > .wcard');
      const act = c.querySelector('.hoveract');
      const after = getComputedStyle(act, '::after');
      return {hover: c.matches(':hover'), sheen: after.animationName,
              lift: getComputedStyle(c).transform, strip: c.classList.contains('open')};
    }""")
    chk("掠过：卡片有抬起动效", st["lift"] != "none" and st["lift"] != "", st)
    chk("掠过：有一道扫光", st["sheen"] == "sheen", st)
    chk("掠过：动作条还没摊开", not st["strip"], st)

    page.mouse.click(first["x"], first["y"])
    page.wait_for_timeout(420)
    flush()
    opened = page.evaluate("""() => {
      const c = document.querySelector('.sgrid > .wcard.open');
      if (!c) return null;
      return [...c.querySelectorAll('.strip button')].map(b => b.textContent);
    }""")
    chk("点一下：摊出取书 + 查看详情", bool(opened) and "查看详情" in opened, opened)
    chk("掠过不给详情侧滑：书名的提示是「点开动作」",
        page.evaluate("() => document.querySelector('.sgrid > .wcard .mt .n').title") == "点开动作")

    spot = blank_point()
    chk("找得到一处真空白（不是撞在卡上）", bool(spot), spot)
    page.mouse.click(spot[0], spot[1])
    page.wait_for_timeout(320)
    chk("点别处：摊开的卡自己收回去",
        page.locator(".sgrid > .wcard.open").count() == 0)

    # 书名和封面是同一个开关：点它只摊动作条，不该再一步跳到详情页
    page.evaluate("() => document.querySelector('.sgrid > .wcard .mt .n').click()")
    page.wait_for_timeout(360)
    ttl = page.evaluate("""() => {
      const n = document.querySelector('.sgrid > .wcard .mt .n');
      const d = document.querySelector('.vpane[data-pane="detail"]');
      return {open: n.closest('.wcard').classList.contains('open'),
              detailShown: !!d && d.hidden !== true};
    }""")
    chk("点书名：摊开动作条", ttl["open"] is True, ttl)
    chk("点书名：没把详情面板换上来", ttl["detailShown"] is False, ttl)
    page.keyboard.press("Escape")
    page.wait_for_timeout(260)

    # ── 3. 切到叠卡 ──────────────────────────────────────────
    page.click("#sMode button[data-m='stack']")
    page.wait_for_timeout(600)
    flush()
    stack = rects(".sstage > .wcard")
    chk("叠卡：舞台里有卡", len(stack) >= 3, len(stack))
    front = page.evaluate("""() => {
      const f = document.querySelector('.sstage > .wcard.s0');
      const st = document.querySelector('.sstage');
      if (!f || !st) return null;
      const r = f.getBoundingClientRect(), b = st.getBoundingClientRect();
      return {w: Math.round(r.width), h: Math.round(r.height),
              cx: Math.round(r.x + r.width / 2), cy: Math.round(r.y + r.height / 2),
              sx: Math.round(b.x + b.width / 2)};
    }""")
    chk("叠卡：一次一本，放大了", front and front["w"] >= 260, front)
    # 居中是相对舞台量的，不是页面中线 —— 左边还有目录栏时两者差着一截。
    chk("叠卡：正面那张在舞台正中", front and abs(front["cx"] - front["sx"]) < 8, front)
    states = page.evaluate("""() => [...document.querySelectorAll('.sstage > .wcard')]
        .map(c => ['s0','s1','s2','s3','b1','b2','b3','sgone','saway']
          .find(k => c.classList.contains(k)) || '?')""")
    chk("叠卡：只有正面一张能翻", states.count("s0") == 1, states)
    chk("叠卡：右边按距离排开", states[:4] == ["s0", "s1", "s2", "s3"], states[:4])
    # 正面就是第一本时，左手边没有「翻过去的」，那几张槽位必须空着 ——
    # 留着半张卡会让人以为还能往后翻，而按 ← 其实什么都没发生。
    chk("叠卡：最前面时左边不留幽灵", not any(s in states for s in ("b1", "b2", "b3", "sgone")), states)
    xs = [r["x"] for r in stack[:4]]
    chk("叠卡：越靠后越往右", all(b > a for a, b in zip(xs, xs[1:])), xs)
    pos = page.text_content("#stPos")
    chk("叠卡：有页码", pos and "/" in pos, pos)
    chk("叠卡：翻页箭头在", page.locator("#stPrev").count() == 1 and page.locator("#stNext").count() == 1)
    chk("叠卡：不再挂「加载更多」", page.locator("#shelfWrap .more").count() == 0)
    chk("叠卡：样式记在本机", page.evaluate("() => localStorage.getItem('guizang_shelf_mode_v1')") == "stack")
    page.screenshot(path=SHOTS[1])

    # ── 4. 键盘 ← / → 翻书：翻过去之后左手边也要有书 ──────────
    def front_idx():
        return page.evaluate(
            "() => [...document.querySelectorAll('.sstage > .wcard')]"
            ".findIndex(c => c.classList.contains('s0'))")

    before = front_idx()
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(120)
    chk("→：翻到下一本", front_idx() == before + 1, before)
    # 折叠那一下是 transition，headless 的时钟不推进就还停在起点；推到底再看。
    flush()
    # 用户报的正是这一步：翻过去之后左手边是空的（旧版把 d<0 全写成 opacity:0 的
    # sgone），于是叠卡只能朝一个方向翻，往回只能靠 ← 或页头的箭头。
    gone = page.evaluate("""() => {
      const g = document.querySelector('.sstage > .wcard.b1');
      if (!g) return null;
      const st = document.querySelector('.sstage').getBoundingClientRect();
      const r = g.getBoundingClientRect();
      return {x: Math.round(r.x + r.width / 2), mid: Math.round(st.x + st.width / 2),
              op: +getComputedStyle(g).opacity, pe: getComputedStyle(g).pointerEvents,
              hid: g.getAttribute('aria-hidden'), tf: getComputedStyle(g).transform};
    }""")
    chk("→：刚翻走的那本摊在左手边", bool(gone) and gone["op"] > .5, gone)
    chk("→：左边那张在舞台中线以左", bool(gone) and gone["x"] < gone["mid"], gone)
    chk("→：左边那张能点、读屏也读得到",
        bool(gone) and gone["pe"] == "auto" and gone["hid"] == "false", gone)
    chk("→：带旋转（折叠屏那一下）", bool(gone) and "matrix3d" in gone["tf"], gone)

    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(700)
    flush()
    chk("→→：连着翻两本", front_idx() == before + 2, before)
    # 左右对称：同一档距离的两张，到舞台中线的水平距离应该一样（±6px 内算对称）。
    far = page.evaluate("""() => {
      const st = document.querySelector('.sstage').getBoundingClientRect();
      const m = st.x + st.width / 2;
      const q = k => {
        const c = document.querySelector('.sstage > .wcard.' + k);
        if (!c) return null;
        const r = c.getBoundingClientRect();
        return {dx: Math.round(r.x + r.width / 2 - m), op: +getComputedStyle(c).opacity};
      };
      return {b1: q('b1'), s1: q('s1'), b2: q('b2'), s2: q('s2')};
    }""")
    chk("叠卡：两侧都有第二、第三张",
        all(far[k] for k in ("b1", "s1", "b2", "s2")) and far["b2"]["op"] > .3, far)
    chk("叠卡：左右镜像摊开",
        all(far[k] and far[j] and abs(far[k]["dx"] + far[j]["dx"]) <= 6
            for k, j in (("b1", "s1"), ("b2", "s2"))), far)
    chk("叠卡：左右同样远近的档亮度一致",
        all(far[k] and far[j] and abs(far[k]["op"] - far[j]["op"]) < .05
            for k, j in (("b1", "s1"), ("b2", "s2"))), far)

    # 上面为了量对称多翻了一本，回来也得翻两次才回到起点。
    for _ in range(2):
        page.keyboard.press("ArrowLeft")
        page.wait_for_timeout(420)
    chk("←：翻回来", front_idx() == before, (before, front_idx()))
    page.keyboard.press("ArrowLeft")
    page.wait_for_timeout(400)
    chk("←：已经在最前面就不越界", front_idx() == 0)

    # 停在中间再看点击范围：光标在第 0 本时左边本来就没有卡，那样测不出「左侧能不能点」。
    for _ in range(3):
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(320)
    flush()
    pe = page.evaluate("""() => {
      const cs = [...document.querySelectorAll('.sstage > .wcard')];
      const k = c => ['s0','s1','s2','s3','b1','b2','b3','sgone','saway'].find(x => c.classList.contains(x));
      const m = {};
      cs.forEach(c => { const s = k(c); if (m[s] === undefined) m[s] = getComputedStyle(c).pointerEvents; });
      return m;
    }""")
    chk("叠卡：两侧摊开的都能点",
        "s0" in pe and "b1" in pe and all(pe[x] == "auto" for x in pe
                                          if x in ("s0", "s1", "s2", "s3", "b1", "b2", "b3")), pe)
    chk("叠卡：看不见的那几张不接点击",
        any(x in pe for x in ("sgone", "saway"))
        and all(pe.get(x) != "auto" for x in ("sgone", "saway")), pe)

    # 点扇形里两侧那几张：直接跳到它，不必一下一下按。
    # 扇形是层叠的，矩形几何中心往往压在别的卡上面 —— 用 elementFromPoint
    # 在卡面上扫一个「确实归它」的点，否则测的是「点到了 s1」而不是「点到了 s2」。
    # 往哪边扫由卡片在左还是在右决定：右半边那张要从外侧往里找，反了就一直落空。
    def click_state(cls, outer_left=False):
        xy = page.evaluate("""(a) => {
          const [k, left] = a;
          const c = document.querySelector('.sstage > .wcard.' + k);
          if (!c) return null;
          const r = c.getBoundingClientRect();
          for (let fy = .18; fy <= .9; fy += .12)
            for (let i = 0; i < 10; i++) {
              const fx = left ? .15 + i * .08 : .85 - i * .08;
              const x = Math.round(r.x + r.width * fx), y = Math.round(r.y + r.height * fy);
              const e = document.elementFromPoint(x, y);
              if (e && c.contains(e)) return [x, y];
            }
          return null;
        }""", [cls, outer_left])
        if not xy:
            return False
        page.mouse.click(xy[0], xy[1])
        page.wait_for_timeout(520)
        return True

    def idx_of(cls):
        return page.evaluate("""(k) => [...document.querySelectorAll('.sstage > .wcard')]
            .findIndex(c => c.classList.contains(k))""", cls)

    tgt = idx_of("s2")
    chk("点右侧那张：跳过去", click_state("s2") and front_idx() == tgt, (tgt, front_idx()))
    tgt = idx_of("b1")
    chk("点左侧那张：翻回去", click_state("b1", True) and front_idx() == tgt, (tgt, front_idx()))

    # 正面那张点封面：还是摊动作条（和瀑布一面同一套规矩）
    xy = page.evaluate("""() => {
      const f = document.querySelector('.sstage > .wcard.s0 .cv');
      const r = f.getBoundingClientRect();
      return [Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2)];
    }""")
    page.mouse.click(xy[0], xy[1])
    page.wait_for_timeout(420)
    chk("叠卡：点封面摊动作条", page.locator(".sstage > .wcard.s0.open").count() == 1)
    chk("叠卡：摊开的动作条里也有查看详情", page.evaluate("""() => {
      const c = document.querySelector('.sstage > .wcard.s0.open');
      return c ? [...c.querySelectorAll('.strip button')].map(b => b.textContent) : [];
    }""").count("查看详情") == 1)
    page.keyboard.press("Escape")
    page.wait_for_timeout(260)

    # ── 5. 切回瀑布：结构换得干净，不留叠卡的壳 ───────────────
    page.click("#sMode button[data-m='grid']")
    page.wait_for_timeout(600)
    flush()
    chk("切回瀑布：网格回来了", page.locator(".sgrid > .wcard").count() >= 3)
    chk("切回瀑布：叠卡的舞台清掉了", page.locator(".sstack").count() == 0)
    # 折叠面会关掉观察器；切回来必须重新挂上，否则瀑布永远停在「藏着」。
    # 注意别要求「每张都没有 pre」——屏幕下方的卡本来就该藏着，等滚到再摊。
    rev = page.evaluate("""() => {
      const g = document.querySelector('.sgrid');
      const cs = [...g.querySelectorAll('.wcard')];
      const h = window.innerHeight;
      const inView = cs.filter(c => c.getBoundingClientRect().top < h * .92);
      return {bound: typeof revealGrid !== 'undefined' && revealGrid === g,
              inView: inView.length,
              hidden: inView.filter(c => c.classList.contains('pre')).length,
              io: !!revealIO};
    }""")
    chk("切回瀑布：观察器重新盯着这张网格", rev["bound"] and rev["io"], rev)
    chk("切回瀑布：眼前这屏的卡都摊开了", rev["inView"] > 0 and rev["hidden"] == 0, rev)
    chk("瀑布：样式也记在本机", page.evaluate("() => localStorage.getItem('guizang_shelf_mode_v1')") == "grid")

    # ── 6. 详情页瘦身：只讲这本书，文件那五颗钮收进阅读器 ─────────
    page.evaluate("() => window.scrollTo(0, 0)")
    page.wait_for_timeout(200)
    entered, dacts = False, None
    for i in range(min(8, len(cards))):
        page.evaluate("(k) => document.querySelectorAll('.sgrid > .wcard')[k]"
                      ".querySelector('.cv').click()", i)
        page.wait_for_timeout(340)
        btns = page.evaluate("""(k) => {
          const c = document.querySelectorAll('.sgrid > .wcard')[k];
          return c ? [...c.querySelectorAll('.strip button')].map(b => b.textContent.trim()) : [];
        }""", i)
        if "查看详情" not in btns:
            page.keyboard.press("Escape"); page.wait_for_timeout(200); continue
        page.evaluate("""(k) => {
          const c = document.querySelectorAll('.sgrid > .wcard')[k];
          [...c.querySelectorAll('.strip button')].find(b => b.textContent.trim() === '查看详情').click();
        }""", i)
        page.wait_for_timeout(900)
        dacts = page.evaluate("""() => {
          const p = document.querySelector('.vpane[data-pane="detail"]:not([hidden])');
          if (!p) return null;
          return [...p.querySelectorAll('.dacts .deed')].map(b => b.textContent.trim());
        }""")
        if dacts is not None:
            entered = True
            break
    chk("从卡片能进到详情页", entered, dacts)
    chk("详情页不再有 完整包/正文/EPUB/PDF/目录",
        not [x for x in (dacts or []) if x in ("完整包", "正文", "EPUB", "PDF", "目录")], dacts)
    chk("详情页还留着取书那一档",
        any(k in (dacts or []) for k in ("开始抓取", "补全图片", "接着取")), dacts)
    strip = page.evaluate("""() => {
      const a = document.querySelector('.vpane:not([hidden]) .dacts');
      if (!a) return null;
      const r = a.getBoundingClientRect();
      return {sw: a.scrollWidth, cw: a.clientWidth, rows: Math.round(r.height / 40)};
    }""")
    chk("详情页动作条没横向溢出", strip and strip["sw"] <= strip["cw"] + 1, strip)

    cvgo = page.evaluate("""() => {
      const cv = document.querySelector('.vpane:not([hidden]) .dtop .cv');
      return cv ? {go: cv.classList.contains('go'), tip: cv.title} : null;
    }""")
    chk("详情页：封面自己就是入口", bool(cvgo) and cvgo["go"] and cvgo["tip"] == "打开阅读", cvgo)
    page.screenshot(path=SHOTS[2])

    # ── 7. 阅读器：文件功能全在这儿 ────────────────────────────
    page.evaluate("() => document.querySelector('.vpane:not([hidden]) .dtop .cv').click()")
    page.wait_for_timeout(1400)
    flush()
    rd = page.evaluate("""() => ({
      reading: document.body.classList.contains('reading'),
      shown: !!document.querySelector('.vpane[data-pane="read"]:not([hidden])'),
      dl: [...document.querySelectorAll('.rdfoot .rddl .deed')].map(b => b.textContent.trim()),
      dlk: [...document.querySelectorAll('.rdfoot .rddl .deed')].map(b => b.dataset.dl),
      page: [...document.querySelectorAll('.rdfoot .deed')].map(b => b.textContent.trim()),
      folder: rdDownload.toString().includes("'folder'"),
      zip: rdDownload.toString().includes("'zip'"),
    })""")
    chk("点封面进阅读器", rd["reading"] and rd["shown"], rd)
    chk("底栏五钮齐全（MD/EPUB/PDF/完整包/文件夹）",
        rd["dl"] == ["MD", "EPUB", "PDF", "完整包", "文件夹"], rd["dl"])
    chk("完整包 / 文件夹 在 rdDownload 里有分支", rd["folder"] and rd["zip"], rd)
    chk("翻章钮还在（上一章/下一章）", "上一章" in rd["page"] and "下一章" in rd["page"], rd["page"])

    foot = page.evaluate("""() => {
      const f = document.querySelector('.rdfoot');
      return {cw: f.clientWidth, sw: f.scrollWidth, pos: getComputedStyle(f.querySelector('.rdpos')).display};
    }""")
    chk("底栏不溢出（满栏宽）", foot["sw"] <= foot["cw"] + 1, foot)
    chk("栏宽够的时候百分比留着", foot["cw"] > 470 and foot["pos"] != "none", foot)
    page.keyboard.press("d")                      # 收起目录栏
    page.wait_for_timeout(700)
    flush()
    narrow = page.evaluate("""() => {
      const f = document.querySelector('.rdfoot');
      return {cw: f.clientWidth, sw: f.scrollWidth};
    }""")
    chk("底栏不溢出（收起目录栏后）", narrow["sw"] <= narrow["cw"] + 1, narrow)
    page.keyboard.press("d")
    page.wait_for_timeout(600)

    # 实测这排东西摊开要 443px，所以按量出来的宽度做三档降级，逐档验一遍：
    # ≤470 百分比让位、≤400 收掉迷你进度条、≤340 放开导出组收缩。每档都不许溢出。
    tiers = page.evaluate("""async () => {
      const f = document.querySelector('.rdfoot');
      const prev = f.style.width;
      const out = [];
      for (const w of [520, 468, 420, 386, 340, 320]) {
        f.style.width = w + 'px';
        await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
        document.getAnimations().forEach(a => { try { a.finish(); } catch (e) {} });
        const cs = getComputedStyle(f), d = f.querySelector('.rddl');
        const pad = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight);
        out.push({w, cw: f.clientWidth, sw: f.scrollWidth,
                  // 容器查询量的是内容盒：clientWidth 还含 padding，直接拿它比阈值会差一截。
                  inner: Math.round(f.clientWidth - pad),
                  ddl: Math.round(d.getBoundingClientRect().width),
                  shrk: getComputedStyle(d).flexShrink,
                  pos: getComputedStyle(f.querySelector('.rdpos')).display,
                  mini: getComputedStyle(f.querySelector('.rdbar-mini')).display});
      }
      f.style.width = prev;
      return out;
    }""")
    for t in tiers:
        chk(f"底栏 {t['w']}px 宽不溢出", t["sw"] <= t["cw"] + 1, t)
    chk("百分比只在栏宽不够那一档才让位",
        all((t["inner"] <= 470) == (t["pos"] == "none") for t in tiers), tiers)
    chk("进度条只在更窄那一档才收",
        all((t["inner"] <= 400) == (t["mini"] == "none") for t in tiers), tiers)
    # 容器查询改不动容器自己（gap / flex-wrap 会被丢掉），所以最后一档靠「导出组收缩」
    # 把宽度让出来 —— 查的就是这一档有没有真的把 flex-shrink 打开。
    chk("最窄那一档导出组放开收缩",
        all((t["shrk"] != "0") == (t["inner"] <= 340) for t in tiers), tiers)

    got = page.evaluate("""async () => {
      const b = encodeURIComponent(RD.book);
      const out = {};
      for (const [k, u] of [['md', '/api/md?book=' + b + '&name=x'],
                            ['zip', '/api/zip?book=' + b + '&json=1'],
                            ['epub', '/api/epub?book=' + b + '&json=1'],
                            ['pdf', '/api/pdf?book=' + b + '&json=1']]) {
        try {
          const r = await fetch(u);
          out[k] = {st: r.status, disp: (r.headers.get('Content-Disposition') || '') !== '',
                    js: u.includes('json=1') ? !!(await r.json()).ok : true};
        } catch (e) { out[k] = {err: String(e)}; }
      }
      return out;
    }""")
    chk("正文 MD 下得下来", got["md"].get("st") == 200 and got["md"].get("disp"), got)
    chk("完整包 打得开", got["zip"].get("st") == 200 and got["zip"].get("js"), got["zip"])
    chk("EPUB 导出有应答", got["epub"].get("st") in (200, 400) and "err" not in got["epub"], got["epub"])
    chk("PDF 导出有应答", got["pdf"].get("st") in (200, 400) and "err" not in got["pdf"], got["pdf"])
    page.screenshot(path=SHOTS[3])

    page.keyboard.press("Escape")
    page.wait_for_timeout(500)
    chk("退出阅读回到书架", not page.evaluate("() => document.body.classList.contains('reading')"))
    chk("全程没有报错", not errors, errors[:3])

    browser.close()

print()
print("叠卡 / 瀑布 交互：", "全部通过" if not FAIL else f"{len(FAIL)} 项失败 -> " + " | ".join(FAIL))
sys.exit(1 if FAIL else 0)
