#!/usr/bin/env python3
"""归藏真机验证：日间 / 夜间切换不许「闪」。

用户报的 bug 是「切换日间模式与夜间模式时页面闪烁」。2026-10-05 实测的根因不是
某一两处，而是全站：几乎每个组件都挂着一条给悬停 / 按压用的补间（按钮的
`background .16s, color .16s`、面板的 `background-color .34s`……）。主题一变，
`--ink-2` / `--paper` / `--glass` 这些变量从旧值跳到新值，几十个组件就各按各的
时长补过去 —— 有的 0.15s 就位、有的 0.34s 还在路上，满屏颜色对不齐，就是那一下闪。
（对照实验：把 transition 全关掉，混色帧从 10 降到 0；只关 animation 不关 transition
则照旧闪 —— 所以病根在 transition 那一层。）

治法：换肤那一帧给 <html> 挂上 `.theme-switching`，把全站补间压平，颜色一次性到位；
下一帧再摘掉，悬停 / 按压那些反馈补间照旧。

这一套就钉这条规矩，两个方向各走一遍（浅转深、深转浅）：
  · 点切换那一刻起，掐两个中途采样点（约 80ms / 200ms），任何可见元素的
    背景色 / 文字色 / 边框色都不许停在「既不旧、又不新」的中间值上 —— 那种元素
    就是正在补间的那一个，有一个算一个，都不许有。
  · 切换要真的发生（两次落定的颜色确实换了），免得「点了没反应」也混过这一条。
  · 落定之后 `<html>` 上不许留着 `.theme-switching` 那个类（留着会把全站补间永久掐死）。

前提：有一个指向沙盒书库的服务（`bash tests/run_all.sh` 会起那一个）。
地址走命令行第一参数或 GUIZANG_TEST_URL；没给就退出，不拿 8770 赌。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import selftest  # noqa: E402

BASE = selftest.need_base(1)
SHOTS_DIR = selftest.SHOTS
SHOTS_DIR.mkdir(parents=True, exist_ok=True)
SHOT = str(SHOTS_DIR / "theme-switch.png")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


# 采样器：把每个「看得见」的元素的背景 / 文字 / 边框色抄下来，按遍历序号对齐。
# 主题切换不增删元素，所以三次采样的序号一一对应。
SNAP_JS = r"""() => {
  const o = [];
  const add = (e) => {
    const r = e.getBoundingClientRect();
    if (r.width < 6 || r.height < 6) { o.push(null); return; }
    const cs = getComputedStyle(e);
    o.push({bg: cs.backgroundColor, fg: cs.color, bc: cs.borderTopColor,
            w: Math.round(r.width), h: Math.round(r.height),
            tag: e.tagName, cls: (e.className || '').toString().slice(0, 40)});
  };
  add(document.documentElement);
  add(document.body);
  document.querySelectorAll('body *').forEach(add);
  return o;
}"""

# 一次 evaluate 里把所有事做完：先拍起点，点切换，途中拍两下，最后拍落定。
# 放在页面里连着做，省掉「点击」与「采样」之间几次往返的网络抖动 —— 中途那两下
# 必须落在补间还没走完的窗口里（不修的话，最大那条补间是 0.34s）。
SWITCH_JS = r"""async (which) => {
  const snap = %s;
  const A = snap();
  document.querySelector('#themeSeg button[data-t="' + which + '"]').click();
  await new Promise(r => setTimeout(r, 80));
  const M1 = snap();
  await new Promise(r => setTimeout(r, 120));
  const M2 = snap();
  await new Promise(r => setTimeout(r, 700));
  const B = snap();
  return {A, M1, M2, B,
          cls: document.documentElement.classList.contains('theme-switching'),
          theme: document.documentElement.dataset.theme || 'auto'};
}""" % SNAP_JS


def mid_count(snap, m1, m2, final):
    """数「三次采样里，至少有一次既不是起点、也不是终点」的元素 —— 即在补间的那些。"""
    n = 0
    worst = []
    for i in range(min(len(snap), len(m1), len(m2), len(final))):
        a, p, q, b = snap[i], m1[i], m2[i], final[i]
        if a is None or p is None or q is None or b is None:
            continue
        hit = False
        for k in ("bg", "fg", "bc"):
            if a[k] == b[k]:
                continue                       # 这一次换肤它根本没变，跳过
            if p[k] != a[k] and p[k] != b[k]:
                hit = True
            if q[k] != a[k] and q[k] != b[k]:
                hit = True
        if hit:
            n += 1
            if len(worst) < 6:
                worst.append("%s.%s %dx%d bg %s->%s->%s"
                             % (a["tag"], a["cls"], a["w"], a["h"], a["bg"], p["bg"], b["bg"]))
    return n, worst


def changed_count(snap, final):
    n = 0
    for i in range(min(len(snap), len(final))):
        a, b = snap[i], final[i]
        if a is None or b is None:
            continue
        if any(a[k] != b[k] for k in ("bg", "fg", "bc")):
            n += 1
    return n


from playwright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 860})
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.emulate_media(color_scheme="light")
    page.goto(BASE + "/", wait_until="networkidle")
    page.wait_for_timeout(1500)

    # 起手是系统浅色（emulate_media 定了）。第一遍点「深」= 浅转深；落定后页面已是深色，
    # 第二遍点「浅」= 深转浅。两个方向各自都是「从当前色切到另一色」，不必先摆姿态。
    for which, label in (("dark", "浅 → 深"), ("light", "深 → 浅")):
        r = page.evaluate(SWITCH_JS, which)
        bad, worst = mid_count(r["A"], r["M1"], r["M2"], r["B"])
        moved = changed_count(r["A"], r["B"])
        chk("真机（%s）：换肤时没有任何元素停在中间色上（不在补间）" % label,
            bad == 0, "补间中的元素 %d 个，例如：%s" % (bad, worst))
        chk("真机（%s）：换肤确实换到了（不是点了没反应）" % label,
            moved >= 8 and r["theme"] == which, "变了 %d 个，theme=%s" % (moved, r["theme"]))
        chk("真机（%s）：落定后 <html> 上没留着 .theme-switching" % label,
            r["cls"] is False, "classList 里还有 theme-switching")

    page.wait_for_timeout(400)
    page.screenshot(path=SHOT)
    chk("真机：全程没有报错", not errors, errors[:6])
    browser.close()

bad = [c for c in checks if not c[0]]
print("\n%d/%d 通过" % (len(checks) - len(bad), len(checks)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
print("截图：" + SHOT)
sys.exit(1 if bad else 0)
