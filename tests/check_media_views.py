# -*- coding: utf-8 -*-
"""订阅 / 视频转笔记这两屏的真机走查。

验的是「界面把后端那两条线接对了没有」，不动数据、不联网：
  · 两屏进得去、结构铺得出来、空态有话；整页不横向溢出、屏幕上一个 emoji 也没有；
  · 视频那屏的四盏灯 + 两个按钮在，点「转写设置」能直接落到设置里的「视频转写」；
  · 转写偏好「保存 → /api/state 读回来」这条往返真的通（写的是沙盒里的 config.json）；
  · 三条只读接口（/api/feed、/api/video、/api/state 的 feed/video 段）形状对。

跑之前先 `python tests/seed.py`，或者由 run_all.sh 代劳。
"""
import json
import pathlib
import re
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import selftest  # noqa: E402
from playwright.sync_api import sync_playwright

BASE = selftest.need_base(1)
URL = BASE + "/"
selftest.SHOTS.mkdir(parents=True, exist_ok=True)
FAIL = []
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u2B00-\u2BFF]")


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if not cond:
        FAIL.append(name)


def api(path, body=None):
    """直连后端的只读/低危接口。不走 curl —— 本机没有按名字找得到的它。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def flush(page):
    page.evaluate("""async () => {
      await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
      document.getAnimations().forEach(a => { try { a.finish(); } catch (e) {} });
    }""")
    page.wait_for_timeout(80)


OVERFLOW = """() => {
  const win = document.documentElement.clientWidth + 1;
  // 被某个 overflow 祖先剪掉的（背景那层模糊光斑就是这样）用户根本看不到，
  // 也不使整页出现滚动条 —— 整页 scrollWidth 那条已经在管这个了，这里不算它戳出。
  const shown = (e) => {
    for (let p = e; p && p !== document.documentElement; p = p.parentElement) {
      const cs = getComputedStyle(p);
      if (p.hidden || cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity < .05)
        return false;
      if (p !== e && /(hidden|clip|auto|scroll)/.test(cs.overflowX)) {
        const r = p.getBoundingClientRect();
        if (r.right <= win && r.left >= 0) return false;
      }
    }
    return true;
  };
  const off = [...document.querySelectorAll('*')].filter(e => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && (r.right > win || r.left < -1) &&
           getComputedStyle(e).position !== 'fixed' &&
           !e.closest('[hidden]') && !e.closest('.pop') &&
           getComputedStyle(e).visibility !== 'hidden' && shown(e);
  }).map(e => ((e.className || '') + ' ' + e.tagName).toString().trim().slice(0, 34));
  return {doc: document.documentElement.scrollWidth, win,
          off: [...new Set(off)].slice(0, 6)};
}"""


def sweep(page, tag):
    t = page.evaluate(OVERFLOW)
    chk(f"{tag}：整页没有横向溢出", t["doc"] <= t["win"] + 1, t)
    chk(f"{tag}：没有元素戳出视口", not t["off"], t["off"])
    hits = sorted(set(EMOJI.findall(page.evaluate("() => document.body.innerText"))))
    chk(f"{tag}：屏幕上一个 emoji 也没有", not hits, hits)


# ── 先验接口形状（离线也跑得动的那几条） ──────────────────────
st = api("/api/state")
chk("state：带了 feed 段（subs / unread）",
    isinstance(st.get("feed"), dict) and "unread" in st["feed"], st.get("feed"))
chk("state：带了 video 段（available / asr）",
    isinstance(st.get("video"), dict) and isinstance(st["video"].get("available"), dict)
    and isinstance(st["video"].get("asr"), dict),
    st.get("video"))
fd = api("/api/feed?mode=list")
chk("feed：mode=list 回 ok 与 subs 数组",
    fd.get("ok") is True and isinstance(fd.get("subs"), list), fd)
chk("feed：summary 形状对", isinstance(fd.get("summary"), dict)
    and "subs" in fd["summary"] and "unread" in fd["summary"], fd.get("summary"))
vd = api("/api/video?mode=status")
chk("video：mode=status 回 available 与 asr",
    vd.get("ok") is True and isinstance(vd.get("available"), dict)
    and isinstance(vd.get("asr"), dict), vd)
chk("video：available 里 ytdlp/ffmpeg/asr/llm 都在",
    all(k in vd["available"] for k in ("ytdlp", "ffmpeg", "asr", "llm")), vd.get("available"))
# 1.0.8 起本地转写（mlx / faster）整体砍掉、只留云端一路 —— 引擎清单里不该再冒出第二样，
# 也没了从前那份 setup（引擎/模型到没到）——那套是给本地权重看的，现在无物可报。
chk("video：转写只剩云端一路（engines 就是 ['cloud']）",
    vd["available"].get("engines") == ["cloud"], vd["available"].get("engines"))
chk("video：云端那盏灯标着 asr.cloud",
    (vd["available"].get("asr") or {}).get("cloud") is True, vd["available"].get("asr"))
chk("video：没东西可报时不再塞一份 setup",
    "setup" not in vd["available"], sorted(vd["available"]))
chk("video：asr 快照报引擎/语言/地址/密钥尾号/模型",
    all(k in vd["asr"] for k in ("engine", "lang", "url", "key_set", "key_tail", "model"))
    and vd["asr"]["engine"] == "cloud", vd.get("asr"))
# 本地模型那一问：没了就得明说没了并指路，不能死在那儿、更不能误报「准备好」。
mst = api("/api/video", {"act": "media_status"})
chk("video：media_status 明说本地模型已移除、并指到设置里改云端",
    mst.get("ok") is False and "云端" in (mst.get("msg") or ""), mst)

# ── 转写偏好：保存 → 读回来（写的是沙盒 config.json） ──────────
old = vd.get("asr") or {}
try:
    r = api("/api/action", {"action": "media.save", "engine": "faster", "lang": "zh"})
    chk("media.save：存得进（ok）", r.get("ok") is True, r)
    # 老配置里残留的 faster 会被静默归正到 cloud —— 界面点了保存不该像没反应。
    chk("media.save：引擎一律归正到 cloud（不报错、静默改道）",
        (r.get("asr") or {}).get("engine") == "cloud", r.get("asr"))
    st2 = api("/api/state")
    chk("media.save：state 读回来就是刚存的语言",
        (st2["video"].get("asr") or {}).get("lang") == "zh", st2.get("video"))
    # 密钥只报「有没有」和末四位，明文绝不回界面、也不进回执。
    sk = api("/api/action", {"action": "media.save", "key": "sk-selftest-1234"})
    view = sk.get("asr") or {}
    chk("media.save：密钥只回末四位，明文不回界面",
        view.get("key_set") is True and view.get("key_tail") == "1234"
        and "sk-selftest-1234" not in json.dumps(sk), view)
finally:
    # 沙盒里这几项本来就是空的，跑完照空清回去 —— 免得后面那几盏灯读到脏配置。
    api("/api/action", {"action": "media.save",
                        "lang": old.get("lang") or "",
                        "url": old.get("url") or "",
                        "key": "", "model": old.get("model") or ""})
chk("media.save：跑完把原值还回去了",
    (api("/api/state")["video"].get("asr") or {}).get("lang") == (old.get("lang") or ""), old)

# ── 真机：两屏 + 设置里那一类 ────────────────────────────────
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto(URL, wait_until="networkidle")
    page.wait_for_timeout(900)

    # 侧边栏两颗新按钮在
    chk("侧边栏有「订阅」", page.locator('#nav button[data-v="feed"]').count() == 1)
    chk("侧边栏有「视频转笔记」", page.locator('#nav button[data-v="video"]').count() == 1)
    chk("订阅那颗徽章挂在按钮上", page.locator('#nav button[data-v="feed"] #bgFeed').count() == 1)

    # ── 订阅那一屏 ──
    page.click('#nav button[data-v="feed"]')
    page.wait_for_timeout(800)
    flush(page)
    chk("订阅：pane 可见", page.evaluate(
        "() => { const p = document.querySelector('.vpane[data-pane=\"feed\"]');"
        " return !!p && !p.hidden; }"))
    for sel, what in (("#fTa", "输入框"), ("#fAdd", "识别并订阅"), ("#fRefresh", "刷新全部"),
                      ("#fSubs", "订阅源列表"), ("#fList", "文章列表"), ("#fOpen", "打开书库文件夹")):
        chk(f"订阅：「{what}」在", page.locator(f'[data-pane="feed"] {sel}').count() == 1)
    chk("订阅：没订任何源时有空态话", page.evaluate(
        "() => (document.querySelector('#fSubs').innerText || '').includes('还没有订阅任何源')"))
    sweep(page, "订阅")
    page.screenshot(path=str(selftest.SHOTS / "media-feed.png"))

    # ── 视频转笔记那一屏 ──
    page.click('#nav button[data-v="video"]')
    page.wait_for_timeout(1400)
    flush(page)
    chk("视频：pane 可见", page.evaluate(
        "() => { const p = document.querySelector('.vpane[data-pane=\"video\"]');"
        " return !!p && !p.hidden; }"))
    for sel, what in (("#vTa", "链接输入框"), ("#vPlan", "识别"), ("#vStart", "开始转笔记"),
                      ("#vCap", "能力灯"), ("#vProg", "进度条"), ("#vShelf", "转出来的书"),
                      ("#vWrap", "两栏工作台"), ("#vPick", "看哪本"), ("#vQ", "关键词筛"),
                      ("#vFrom", "起点"), ("#vTo", "终点"), ("#vRows", "段落容器"),
                      ("#vSave", "保存转写"), ("#vRebuild", "重建章节"),
                      ("#vExport", "导出"), ("#vCntT", "段落计数"), ("#vFoldNav", "收起左栏")):
        chk(f"视频：「{what}」在", page.locator(f'[data-pane="video"] {sel}').count() == 1)
    lights = page.evaluate(
        "() => [...document.querySelectorAll('#vCap .it')].map(e => e.className)")
    chk("视频：四盏灯都亮出来了（下载器 / ffmpeg / 云端转写 / 大模型）",
        len(lights) == 4, lights)
    chk("视频：每盏灯非 ok 即 no", all(("ok" in c) ^ ("no" in c) for c in lights), lights)
    # 云端转写那盏灯必须说真话：沙盒里没填过接口地址，就不许冒充「已配置」。
    chk("视频：没填过接口时，云端转写那盏灯不冒充已配置", page.evaluate("""() => {
      const t = [...document.querySelectorAll('#vCap .it')]
        .map(e => e.textContent).find(x => x.includes('云端转写'));
      return !!t && /未配置/.test(t);
    }"""), page.evaluate("() => [...document.querySelectorAll('#vCap .it')].map(e => e.textContent)"))
    # 能力灯旁边那颗「转写设置」得在 —— 云化之后填地址全靠它把用户送进设置页。
    chk("视频：「转写设置」那颗钮挂在能力灯旁边", page.evaluate(
        "() => [...document.querySelectorAll('#vCap button')]"
        ".some(b => (b.textContent || '').includes('转写设置'))"))
    # 沙盒里 seeded 了一本转出来的书（tests/seed.py 的 video_SE_LECTURE），
    # 所以这一栏不该再是空态；空态那条话在 check_video_workbench.py 里另有验法。
    rows = page.evaluate("() => document.querySelectorAll('#vShelf .vtbook').length")
    chk("视频：左栏把转出来的书列出来了（不再整栏空着）", rows >= 1, rows)
    chk("视频：栏头报了本数", page.evaluate(
        "(() => { const t = (document.querySelector('#vShelfHead').textContent || '');"
        " return /一共 \\d+ 本/.test(t); })()"),
        page.evaluate("() => document.querySelector('#vShelfHead').textContent"))
    lamps = page.evaluate(
        "() => [...document.querySelectorAll('#vShelf .vtbook .lt i')].map(e => e.textContent)")
    chk("视频：每本书挂着三盏灯（时间戳 / 手改 / 导过几份）",
        len(lamps) >= 3 and any('时间戳' in x for x in lamps)
        and any('手改' in x for x in lamps) and any('导' in x for x in lamps), lamps)
    chk("视频：没挑书时段落是空的、工具条按住了", page.evaluate(
        "() => document.querySelectorAll('#vRows .vtrow').length === 0"
        " && document.querySelector('#vSave').disabled"
        " && document.querySelector('#vRebuild').disabled"))
    chk("视频：没挑书时计数说「还没挑书」", page.evaluate(
        "() => (document.querySelector('#vCntT').textContent || '').includes('还没挑书')"))
    chk("视频：没贴链接时提示贴链接", page.evaluate(
        "() => (document.querySelector('#vCnt').innerText || '').includes('把视频链接贴进来')"))

    # plan() 给的是 pages / count，不是 parts —— 分 P 视频必须报出总数和这次取哪一 P，
    # 否则用户以为整部都转了。这里塞一份真形状的 plan 进去，看它有没有用错字段。
    page.evaluate("""() => {
      vidPlan = {platform: 'bilibili', vid: 'BV1gA411j7Ta',
        url: 'https://www.bilibili.com/video/BV1gA411j7Ta?p=3',
        title: '高等数学基础课', uploader: '某某', duration: 2047, cover: '',
        count: 46, pages: [
          {i: 1, title: 'p01 函数', url: 'https://www.bilibili.com/video/BV1gA411j7Ta?p=1'},
          {i: 2, title: 'p02 极限', url: 'https://www.bilibili.com/video/BV1gA411j7Ta?p=2'},
          {i: 3, title: 'p03 连续', url: 'https://www.bilibili.com/video/BV1gA411j7Ta?p=3'}]};
      vidRenderPlan();
    }""")
    page.wait_for_timeout(200)
    plan_txt = page.evaluate("() => (document.querySelector('#vPlanBox').innerText || '')")
    chk("视频：认出后 plan 卡弹出来", page.evaluate(
        "() => document.querySelector('#vPlanBox').classList.contains('on')"), plan_txt[:80])
    chk("视频：plan 卡报出分 P 总数（读的是 count 不是 parts）",
        "46 个分 P" in plan_txt, plan_txt[:200])
    chk("视频：plan 卡写清这次转的是第几 P",
        "连续" in plan_txt, plan_txt[:200])
    chk("视频：plan 卡报出时长与作者",
        "34 分" in plan_txt and "某某" in plan_txt, plan_txt[:200])
    page.evaluate("() => { vidPlan = null; vidRenderPlan(); }")
    page.wait_for_timeout(150)
    chk("视频：清掉 plan 后卡片收回去", not page.evaluate(
        "() => document.querySelector('#vPlanBox').classList.contains('on')"))

    sweep(page, "视频")
    page.screenshot(path=str(selftest.SHOTS / "media-video.png"))

    # 点了「开始转笔记」但没贴链接 → 只提示、不炸
    page.click('[data-pane="video"] #vStart')
    page.wait_for_timeout(400)
    chk("视频：空链接点开始只出提示，不报错", not errors, errors[:4])

    # ── 从这一屏点进设置里的「视频转写」 ──
    page.evaluate("() => { const b=[...document.querySelectorAll('#vCap button')]"
                  ".find(x => x.textContent.includes('转写设置')); if (b) b.click(); }")
    page.wait_for_timeout(700)
    flush(page)
    chk("设置：弹层开了", page.evaluate(
        "() => document.querySelector('#pop').classList.contains('open')"))
    chk("设置：直接落在「视频转写」这一类", page.evaluate(
        "() => { const s = document.querySelector('.popcat[data-cat=\"media\"]');"
        " return !!s && s.classList.contains('on'); }"))
    for sel, what in (("#asrUrl", "接口地址"), ("#asrKey", "API Key"), ("#asrModel", "模型"),
                      ("#asrLang", "语言"), ("#asrSave", "保存"), ("#vfFix", "装 ffmpeg")):
        chk(f"设置：「{what}」在", page.locator(sel).count() == 1)
    chk("设置：本地引擎那张选单已经摘掉（转写只剩云端一路）",
        page.locator("#asrEngine").count() == 0)
    chk("设置：状态那行有话（不是占位符）", page.evaluate(
        "() => { const t=(document.querySelector('#v-asr').textContent||'').trim();"
        " return !!t && t !== '—'; }"))
    sweep(page, "设置·视频转写")

    # 在界面上改一次再存：值真的落到后端（云端三格 + 语言；引擎恒 cloud）
    page.fill("#asrUrl", "https://asr.selftest/v1")
    page.fill("#asrKey", "sk-selftest-9876")
    page.fill("#asrModel", "whisper-large-v3")
    page.fill("#asrLang", "ja")
    page.click("#asrSave")
    page.wait_for_timeout(900)
    after = api("/api/state")["video"]["asr"]
    chk("设置：保存后后端就是填的地址 / 模型 / 语言，引擎恒 cloud",
        after.get("engine") == "cloud" and after.get("url") == "https://asr.selftest/v1"
        and after.get("model") == "whisper-large-v3" and after.get("lang") == "ja", after)
    chk("设置：Key 只回末四位，明文不回界面",
        after.get("key_set") is True and after.get("key_tail") == "9876"
        and "sk-selftest-9876" not in json.dumps(after), after)
    chk("设置：轮询不会把用户刚填的值顶回去", page.evaluate(
        "() => document.querySelector('#asrUrl').value") == "https://asr.selftest/v1")
    page.fill("#asrUrl", old.get("url") or "")
    page.fill("#asrModel", old.get("model") or "")
    page.fill("#asrLang", old.get("lang") or "")
    page.click("#asrSave")
    page.wait_for_timeout(700)
    restored = api("/api/state")["video"]["asr"]
    chk("设置：收尾还原成原值",
        restored.get("url") == (old.get("url") or "")
        and restored.get("lang") == (old.get("lang") or "")
        and restored.get("model") == (old.get("model") or ""), restored)
    # Key 字段留空 = 不改，所以上面那枚测试 Key 得单独清掉，别带进下一个套件
    api("/api/action", {"action": "media.save", "key": ""})

    # 关掉弹层，回到两屏再转一圈（常驻 pane 不该在来回时炸掉）
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    page.click('#nav button[data-v="feed"]')
    page.wait_for_timeout(500)
    page.click('#nav button[data-v="video"]')
    page.wait_for_timeout(500)
    chk("来回切两屏：控件还在、没被清掉", page.locator(
        '[data-pane="video"] #vStart').count() == 1
        and page.locator('[data-pane="feed"] #fAdd').count() == 1)
    chk("全程没有报错", not errors, errors[:6])

    browser.close()

print()
print(f"媒体两屏：{'全部通过' if not FAIL else str(len(FAIL)) + ' 项失败 -> ' + ' | '.join(FAIL)}")
sys.exit(1 if FAIL else 0)
