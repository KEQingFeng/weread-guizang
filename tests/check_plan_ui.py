#!/usr/bin/env python3
"""归藏真机验证：每本书的阅读计划（1.0.6 · #214）。

「为每本书建一个独立的阅读计划，设置每日 / 每周目标，随时知道还剩多少页，读完留个交代」
是这一条的正题。离线那一半（页数折算 / 只增不减 / 跨天记账 / 读完留档）在 check_plan.py
里；这一套管的是「人在界面上点得动、点完看得见」：

  · 详情页铺出「阅读计划」那一块：没定过时给「全书 N 页 + 每天/每周 + 页数 + 开始计划」，
    换芯片会跟着改单位与那句「照这个节奏大约几天读完」。
  · 定下去之后读数就位：已读 / 全书 / 还剩 N 页 / 百分比 / 目标 / 今日 / 预计读完。
  · 阅读器底栏那条「还剩 N 页」实时跟着读到的位置走（位置由 rdSavePos 的 800ms 防抖上报）。
  · 调整目标不清零：换个页数，已读的那部分还在。
  · 结束计划是两下确认（第一下换成「再点一次就结束」，第二下才真结束），结束回到没定的样子。
  · 读完之后：读数变成「已读完全书」，并且「往期完成记录」里留着起止与当时的目标。
  · 最窄那一档不许横向溢出（「文字控制在边框内」的兜底）。

前提：有一个指向沙盒书库的服务（`bash tests/run_all.sh` 会起那一个），且 seed 铺好了
GAPBOOK1（10 章、第 3 章够长）。地址走命令行第一参数或 GUIZANG_TEST_URL；没给就退出。
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import selftest  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

BASE = selftest.need_base(1)
SHOTS = selftest.SHOTS
SHOTS.mkdir(parents=True, exist_ok=True)
SHOT = str(SHOTS / "readplan.png")

BOOK = "GAPBOOK1"

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


def plan_state(pg):
    """直接问后端：这一次读数里我们要断的那几个数。"""
    return pg.evaluate("""async () => {
      const r = await fetch('/api/plan?book=%s');
      const j = await r.json();
      return j && j.ok ? j.data : null;
    }""" % BOOK)


def detail(pg, seq):
    pg.evaluate("(s) => renderDetailView('%s', '%s', '计划试验本')" % (BOOK, BOOK))
    pg.wait_for_selector('.vpane[data-pane="detail"] .plan', timeout=6000)
    pg.wait_for_timeout(500)


def main():
    with sync_playwright() as pw:
        br = pw.chromium.launch(args=["--no-sandbox"])
        pg = br.new_page(viewport={"width": 1280, "height": 900})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)[:160]))
        pg.goto(BASE + "/", wait_until="domcontentloaded")
        pg.evaluate("() => localStorage.clear()")
        pg.reload(wait_until="domcontentloaded")
        pg.wait_for_timeout(1200)
        # 套件自成一体：先把这个书号的旧计划清掉，不受上一轮残留影响。
        pg.evaluate("""async () => { await fetch('/api/plan', {method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({act: 'clear', book: '%s'})}); }""" % BOOK)
        pg.wait_for_timeout(200)

        # ── 1. 详情页那块「阅读计划」：没定过时的样子 ──────────────
        detail(pg, 1)
        form = pg.evaluate("""() => {
          const b = document.querySelector('.vpane[data-pane="detail"] .plan');
          return {
            has: !!b,
            head: b ? b.querySelector('h3').textContent.trim() : '',
            sub: b ? (b.querySelector('.planhd .sub') || {}).textContent || '' : '',
            segs: [...b.querySelectorAll('#plSeg button')].map(x => x.textContent.trim()),
            save: (b.querySelector('#plSave') || {}).textContent || '',
            lead: (b.querySelector('#plLead') || {}).textContent || '',
            num: !!b.querySelector('#plNum'),
          };
        }""")
        chk("详情页铺出「阅读计划」", form["has"] and form["head"] == "阅读计划", form)
        chk("没定过时给「全书 N 页」", "全书 " in form["sub"] and "页" in form["sub"], form["sub"])
        chk("芯片是「每天 / 每周」", form["segs"] == ["每天", "每周"], form["segs"])
        chk("有一颗「开始计划」和页数输入框", form["save"] == "开始计划" and form["num"], form)
        chk("给出「照这个节奏大约几天读完」", "全书约" in form["lead"] and "天读完" in form["lead"],
            form["lead"])
        total = int("".join(c for c in form["sub"] if c.isdigit()) or 0)
        chk("全书页数是个正数", total > 0, form["sub"])

        # 换「每周」那颗芯片：单位与那句估算要跟着改
        pg.click(".vpane[data-pane='detail'] .plan #plSeg button[data-p='weekly']")
        pg.wait_for_timeout(120)
        unit = pg.evaluate(
            "() => document.querySelector(\".plan #plUnit\").textContent.trim()")
        chk("换「每周」单位改成「页 / 周」", unit == "页 / 周", unit)
        pg.click(".vpane[data-pane='detail'] .plan #plSeg button[data-p='daily']")
        pg.wait_for_timeout(120)

        # ── 2. 定一个每天 3 页的计划 ──────────────────────────────
        pg.fill(".vpane[data-pane='detail'] .plan #plNum", "3")
        pg.click(".vpane[data-pane='detail'] .plan #plSave")
        pg.wait_for_selector(".vpane[data-pane='detail'] .plan .big", timeout=6000)
        pg.wait_for_timeout(400)
        ro = pg.evaluate("""() => {
          const b = document.querySelector('.vpane[data-pane="detail"] .plan');
          const sks = [...b.querySelectorAll('.rowsk .sk')].map(
            s => [s.querySelector('.k').textContent.trim(),
                  s.querySelector('.v').textContent.trim()]);
          return {big: b.querySelector('.big').textContent.trim(),
                  sks: sks,
                  end: !!b.querySelector('#plEnd'),
                  edit: (b.querySelector('#plEdit') || {}).textContent || ''};
        }""")
        chk("定完就换成读数（已读 / 全书 / 还剩）",
            "已读" in ro["big"] and str(total) in ro["big"] and "还剩" in ro["big"], ro["big"])
        kv = dict(ro["sks"])
        chk("读数里目标写着「每天 3 页」", kv.get("目标") == "每天 3 页", kv)
        chk("读数里有今日与预计读完", "今日" in kv and "预计读完" in kv, kv)
        chk("定完之后有「调整目标 / 结束计划」", ro["edit"] == "调整目标" and ro["end"], ro)

        # ── 3. 阅读器底栏那条「还剩 N 页」跟着读到的位置走 ─────────
        pg.evaluate("() => setView('shelf')")
        pg.wait_for_timeout(300)
        pg.evaluate("() => openReader('%s', '计划试验本', 'shelf')" % BOOK)
        pg.wait_for_selector("#rdBody", timeout=9000)
        pg.wait_for_timeout(1200)
        foot0 = pg.evaluate("() => (document.querySelector('#rdPlan') || {}).textContent || ''")
        chk("开书后底栏摆出「还剩 N 页」", "还剩" in foot0 and "页" in foot0, foot0)
        chk("刚打开还没读到东西，账是 0", (plan_state(pg) or {}).get("read") == 0,
            plan_state(pg))

        # 翻到第 3 章（seed 特意写长了这一章）再滚一段，等 800ms 防抖把位置报上去
        pg.evaluate("() => rdGo(2)")
        pg.wait_for_timeout(700)
        pg.evaluate("""() => { const s = document.querySelector('#rdScroll');
          s.scrollTop = Math.min(1200, s.scrollHeight); s.dispatchEvent(new Event('scroll')); }""")
        pg.wait_for_timeout(1600)
        st = plan_state(pg) or {}
        chk("读到第 3 章后账往前走了（read > 0）", (st.get("read") or 0) > 0, st)
        chk("底栏那条跟着更新成新的「还剩」",
            ("还剩 " + str(st.get("left"))) in pg.evaluate(
                "() => (document.querySelector('#rdPlan') || {}).textContent || ''"), st)
        chk("进度没有越过全书（read <= pages）", (st.get("read") or 0) <= (st.get("pages") or 0), st)
        read_after = st.get("read") or 0
        pg.screenshot(path=SHOT)

        # ── 4. 调整目标不清零 ───────────────────────────────────
        detail(pg, 4)
        pg.click(".vpane[data-pane='detail'] .plan #plEdit")
        pg.wait_for_timeout(150)
        edit = pg.evaluate("""() => { const b = document.querySelector('.vpane[data-pane="detail"] .plan');
          return {save: (b.querySelector('#plSave') || {}).textContent || '',
                  cancel: !!b.querySelector('#plCancel'),
                  val: (b.querySelector('#plNum') || {}).value || ''}; }""")
        chk("「调整目标」把表单摆回来（保存目标 / 不改了）",
            edit["save"] == "保存目标" and edit["cancel"], edit)
        chk("调整时把原来的目标填进输入框", edit["val"] == "3", edit)
        pg.fill(".vpane[data-pane='detail'] .plan #plNum", "5")
        pg.click(".vpane[data-pane='detail'] .plan #plSave")
        pg.wait_for_selector(".vpane[data-pane='detail'] .plan .big", timeout=6000)
        pg.wait_for_timeout(400)
        st2 = plan_state(pg) or {}
        chk("改成每天 5 页", (st2.get("goal") or {}).get("pages") == 5, st2.get("goal"))
        chk("改目标不动已读的那部分（高水位还在）", (st2.get("read") or 0) == read_after,
            {"before": read_after, "after": st2.get("read")})

        # ── 5. 结束计划要两下确认 ───────────────────────────────
        pg.click(".vpane[data-pane='detail'] .plan #plEnd")
        pg.wait_for_timeout(150)
        armed = pg.evaluate(
            "() => (document.querySelector(\".plan #plEnd\") || {}).textContent || ''")
        chk("第一下只换成「再点一次就结束」，还没真结束",
            "再点一次" in armed and (plan_state(pg) or {}).get("has"), armed)
        pg.click(".vpane[data-pane='detail'] .plan #plEnd")
        pg.wait_for_timeout(700)
        chk("第二下才真结束", not (plan_state(pg) or {}).get("has"), plan_state(pg))
        back = pg.evaluate(
            "() => (document.querySelector(\".plan #plSave\") || {}).textContent || ''")
        chk("结束之后回到没定的样子", back == "开始计划", back)

        # ── 6. 读完留档 ─────────────────────────────────────────
        pg.fill(".vpane[data-pane='detail'] .plan #plNum", "4")
        pg.click(".vpane[data-pane='detail'] .plan #plSave")
        pg.wait_for_selector(".vpane[data-pane='detail'] .plan .big", timeout=6000)
        # 直接把位置推到尾声（at 越界一律当读完全书），不必真滚到底
        pg.evaluate("""async () => { await fetch('/api/plan', {method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({act: 'pos', book: '%s', at: 999, frac: 1})}); }""" % BOOK)
        pg.wait_for_timeout(400)
        detail(pg, 6)
        done = pg.evaluate("""() => {
          const b = document.querySelector('.vpane[data-pane="detail"] .plan');
          return {big: (b.querySelector('.big') || {}).textContent || '',
                  hist: (b.querySelector('.hist') || {}).textContent || '',
                  edit: (b.querySelector('#plEdit') || {}).textContent || '',
                  end: !!b.querySelector('#plEnd')};
        }""")
        chk("读完之后读数说「已读完全书」", "已读完全书" in done["big"], done)
        chk("留了「往期完成记录」", "往期完成记录" in done["hist"] and "读完" in done["hist"], done)
        chk("读完之后那颗钮是「重新开始」、没有「结束计划」",
            done["edit"] == "重新开始" and not done["end"], done)

        # ── 7. 最窄那一档不许横向溢出 ───────────────────────────
        pg.set_viewport_size({"width": 760, "height": 800})
        pg.wait_for_timeout(400)
        ovf = pg.evaluate("""() => ({sw: document.documentElement.scrollWidth,
                                     iw: window.innerWidth})""")
        chk("窄窗下详情页不横向溢出", ovf["sw"] <= ovf["iw"] + 1, ovf)

        chk("整场没有页面报错", not errs, errs)
        br.close()

    bad = [n for ok, n, _ in checks if not ok]
    print()
    if bad:
        print("阅读计划真机套件有 %d 处不对：%s" % (len(bad), "、".join(bad)))
    else:
        print("阅读计划这一路：详情页定 / 改 / 结束都在原地，底栏那条「还剩 N 页」跟着读到的位置走，读完留档。")
    sys.exit(len(bad))


if __name__ == "__main__":
    main()
