#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剪藏那一屏的真机走查：贴链接 → 解析 → 预览 → 入库 → 打开阅读 → 返回。

只做用户会做的那套动作，然后断言界面状态（不是断言后端返回）。
公众号链接会过期，所以这一份不吃仓库里的固定数据：链接表由 GUIZANG_CLIP_LINKS 指路
（默认找沙盒目录下的 clip_links.txt），没有现成链接表时整个套件自己跳过，不算失败。
"""
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import selftest  # noqa: E402
from playwright.sync_api import sync_playwright

BASE = selftest.need_base(1)
OUT = selftest.SANDBOX / "clip"
OUT.mkdir(parents=True, exist_ok=True)
LINKS = Path(os.environ.get("GUIZANG_CLIP_LINKS", str(selftest.SANDBOX / "clip_links.txt")))
PANE = 'div[data-pane="clip"]'
errors, results = [], []

if not LINKS.exists():
    print("SKIP  剪藏真机走查：没有现成的公众号链接表（链接会过期，不入库）。")
    print("      想跑就准备一份 txt，export GUIZANG_CLIP_LINKS=/path/to/links.txt 再来。")
    sys.exit(0)
links = [x.strip() for x in LINKS.read_text(encoding="utf-8").splitlines() if x.strip()]

urllib.request.urlopen(f"{BASE}/api/state", timeout=5).read()

# 每行的状态 chip 文本，按列表顺序
ROWS = """() => {
  const p = document.querySelector('%s');
  return [...p.querySelectorAll('.cliprow')].map(r => ({
    nm: (r.querySelector('.nm') || {}).textContent || '',
    sub: (r.querySelector('.sub') || {}).textContent || '',
    st: (r.querySelector('.st') || {}).textContent || '',
    acts: [...r.querySelectorAll('.acts button')].map(b => b.textContent + (b.disabled ? '(锁)' : '')),
  }));
}""" % PANE


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(("  ok   " if cond else "  FAIL ") + name + ("  ← " + str(extra)[:150] if extra else ""))


def shots(page, tag):
    page.screenshot(path=str(OUT / f"{tag}.png"))


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900},
                            device_scale_factor=2)
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_selector("#nav button", timeout=15000)
    page.wait_for_timeout(700)

    # 1. 进剪藏那一屏
    page.click('#nav button[data-v="clip"]')
    page.wait_for_selector(f'{PANE} #cTa', timeout=8000)
    page.wait_for_timeout(400)
    cnt0 = page.inner_text(f'{PANE} #cCnt')
    clip_total = page.inner_text('#bgClip')
    check("进剪藏：粘贴框在位", page.is_visible(f'{PANE} #cTa'))
    check("进剪藏：空列表有话说明", "还没贴链接" in cnt0, cnt0)
    # 空态只在「书库里确实一篇剪来的都没有」时才该出现；沙盒里之前跑剩下的，不算 bug
    if clip_total.strip() in ("", "0"):
        check("进剪藏：一篇没有时空态在位", page.is_visible(f'{PANE} #cShelf .emptyline'))
    else:
        check("进剪藏：已有剪藏就铺卡片而不是空态",
              page.eval_on_selector_all(f'{PANE} #cShelf .wcard', "els => els.length") > 0,
              f"徽章 {clip_total} 张卡片")
    shots(page, "01-empty")

    # 2. 贴链接（含一行噪声文本，验证只认 http）
    noise = "顺手贴的一句说明文字"
    page.fill(f'{PANE} #cTa', "\n".join([noise] + links))
    page.wait_for_timeout(700)
    hint = page.inner_text(f'{PANE} #cCnt')
    check("贴上去就先数出条数", f"认出 {len(links)} 条" in hint, hint)

    # 3. 解析正文
    page.click(f'{PANE} #cParse')
    page.wait_for_function(
        "() => {const p=document.querySelector('%s');"
        "return [...p.querySelectorAll('.cliprow .st')].every(s => !/待抓|抓取中/.test(s.textContent));}"
        % PANE, timeout=120000)
    rows = page.evaluate(ROWS)
    print("  行：%s" % [(r["st"], r["nm"][:18]) for r in rows])
    check("解析后行数=链接数（噪声行被丢掉）", len(rows) == len(links), len(rows))
    check("解析出真标题", sum(1 for r in rows if r["nm"].startswith("http")) == 0,
          [r["nm"][:20] for r in rows])
    check("状态 chip 落到终态", all(r["st"] in ("已解析", "没抓到", "已入库") for r in rows),
          [r["st"] for r in rows])
    check("解析完那一行给三个动作",
          all(len(r["acts"]) == 3 for r in rows), [r["acts"] for r in rows])
    check("解析后能预览", any("预览" in " ".join(r["acts"]) for r in rows))
    shots(page, "02-parsed")

    # 4. 预览弹窗：确认正文确实抓到了
    first_ready = page.locator(f'{PANE} .cliprow', has_text="已解析").first
    first_ready.locator("button", has_text="预览").click()
    page.wait_for_selector("#veil.open", timeout=6000)
    head = page.input_value("#sheetBody")
    title = page.inner_text("#sheetTitle") if page.is_visible("#sheetTitle") else ""
    check("预览弹窗有正文", len(head) > 200, len(head))
    shots(page, "03-preview")

    # 5. 弹窗里直接入库
    page.locator("#sheetActs button", has_text="收进书库").click()
    page.wait_for_timeout(9000)
    rows = page.evaluate(ROWS)
    done = [r for r in rows if r["st"] == "已入库"]
    check("弹窗点入库后该行变已入库", len(done) >= 1, [r["st"] for r in rows])
    check("入库后动作换成阅读/原文", done and ("阅读" in done[0]["acts"][0]
          and "原文" in done[0]["acts"][1]), done[0]["acts"] if done else "")

    # 6. 最近剪的：卡片与徽章
    badge = page.inner_text("#bgClip")
    cards = page.locator(f'{PANE} #cShelf .wcard').count()
    check("侧边栏徽章有数字", badge.strip().isdigit() and int(badge) >= 1, badge)
    check("最近剪的出现卡片", cards >= 1, cards)
    shots(page, "04-shelf")

    # 7. 剩下的整批入库
    page.click(f'{PANE} #cAll')
    page.wait_for_function(
        "() => {const p=document.querySelector('%s');"
        "const a=[...p.querySelectorAll('.cliprow .st')];"
        "return a.length && a.every(s => /已入库|没抓到/.test(s.textContent));}" % PANE,
        timeout=180000)
    rows = page.evaluate(ROWS)
    check("整批入库跑完（没有卡在抓取中）",
          all(r["st"] in ("已入库", "没抓到") for r in rows), [r["st"] for r in rows])
    shots(page, "05-all-in")

    # 8. 从卡片进阅读器，读完回得来
    page.locator(f'{PANE} #cShelf .wcard .mt .n').first.click()
    page.wait_for_selector('div[data-pane="read"] .md', timeout=20000)
    page.wait_for_timeout(900)
    md = page.inner_text('div[data-pane="read"] .md')
    check("剪藏的文章能读正文", len(md) > 300, len(md))
    imgs = page.eval_on_selector_all('div[data-pane="read"] .md img',
                                     "ns => ns.map(n => ({src: n.src.slice(0,40), ref: n.referrerPolicy, w: n.naturalWidth}))")
    ext = [i for i in imgs if i["ref"] == "no-referrer"]
    check("外链图都设了 no-referrer", len(ext) == len([i for i in imgs if i["src"].startswith("http")]),
          f"{len(ext)}/{len(imgs)}")
    shots(page, "06-reader")
    page.click('div[data-pane="read"] #rdBack')
    page.wait_for_timeout(900)
    check("返回回到剪藏那一屏", page.is_visible(f'{PANE} #cTa'))

    # 9. 移除一条：只删列表项，不动书库
    before = page.locator(f'{PANE} .cliprow').count()
    page.locator(f'{PANE} .cliprow').first.locator("button", has_text="移除").click()
    page.wait_for_timeout(500)
    after = page.locator(f'{PANE} .cliprow').count()
    check("移除少一行", after == before - 1, f"{before}→{after}")

    # 10. 本地书库能筛出剪藏的
    page.click('#nav button[data-v="local"]')
    page.wait_for_selector('div[data-pane="local"] #lSeg', timeout=8000)
    page.wait_for_timeout(600)
    page.locator('div[data-pane="local"] #lSeg button', has_text="剪藏的").click()
    page.wait_for_timeout(800)
    lcards = page.locator('div[data-pane="local"] #lWrap .wcard').count()
    tags = [t.strip() for t in page.locator('div[data-pane="local"] #lWrap .wcard .tag').all_inner_texts()]
    check("本地书库按「剪藏的」筛得出", lcards >= 1, lcards)
    check("剪藏的卡片格式签是「文章」", all(t == "文章" for t in tags), tags[:4])
    shots(page, "07-local-clip")

    # 11. 溢出 / emoji / 控制台
    ov = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
    check("本地书库没有横向溢出", ov <= 0, ov)
    page.click('#nav button[data-v="clip"]')
    page.wait_for_timeout(700)
    ov2 = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
    check("剪藏屏没有横向溢出", ov2 <= 0, ov2)
    emo = page.evaluate("""() => {
      const t = document.body.innerText || '';
      return (t.match(/[\\u{1F000}-\\u{1FAFF}\\u{2600}-\\u{27BF}\\u{FE0F}\\u{2B00}-\\u{2BFF}]/gu) || []).join('');
    }""")
    check("界面零 emoji", not emo, emo)
    shots(page, "08-final")

    browser.close()

print("\n控制台错误：%d" % len(errors))
for e in errors[:6]:
    print("  ! " + e[:180])
bad = [r for r in results if not r[1]]
print("结果：%s" % ("全部通过（%d 项）" % len(results) if not bad
                   else "%d/%d 失败" % (len(bad), len(results)))
      + ("" if not errors else "，另有 %d 条控制台错误" % len(errors)))
sys.exit(1 if (bad or errors) else 0)
