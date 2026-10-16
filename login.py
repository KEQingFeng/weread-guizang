#!/usr/bin/env python3
"""微信读书登录助手。

判定以 wr_vid cookie 为准（未登录时 /web/shelf 不会跳转，URL 判断会误报）。
未登录则自动点开扫码弹窗，等用户扫码。结果写入 cache/login_state.json 供 Web 界面读取。
持久化 profile 与 export_precise.py 共用（cache/browser_profile）。

这一份账本有三处读者：设置页（ui.html）、壳（shell/main.swift）、MCP 适配器。
三处都按同一个形状读 —— `logged_in` 是布尔、`checked_at` 是秒数，然后直接
`new Date(checked_at * 1000)`。可磁盘上这份文件可能被手改过、可能是半截的、
也可能是老版本留下的别种形状，于是它们算出来的就是那句
「未连接 NaN月NaN日 NaN:NaN」。所以形状只在 normalize_state 一处定义：
写进去的、读出来的，都先过这一把尺子；认不出的形状一律当「没有这一份」。
"""
import asyncio
import json
import math
import os
import time

import platform_compat as pc  # noqa: F401  —— 导入即抹掉 XPC_FLAGS 的 0x2 位（见该模块说明）

USER_DATA_DIR = os.path.join("cache", "browser_profile")
STATE_PATH = os.path.join("cache", "login_state.json")
SHELF_URL = "https://weread.qq.com/web/shelf"
SCAN_TIMEOUT = 300


def _seconds(v):
    """只认真正的秒数：布尔是 int 的子类不算，字符串一律不算。

    `"昨天" * 1000` 是 NaN、`True * 1000` 是 1000（1970 年那条假时间），
    两者都会让界面和 MCP 印出一句看着像后端坏了的胡话。

    `float(v)` 这一步自己也要兜 `OverflowError`：`json` 把账本里一长串数字解成一个
    Python 大整数（`{"checked_at": 10**400}` 就是），而大整数到 float 装不下时会抛
    —— `math.isfinite` 还没轮到，异常先穿出 `normalize_state`，被 `read_state` 那把
    宽 except 接住，于是**整份登录态回 None**：`logged_in: true` 连同 url / note 一起
    没了，界面上是「已登录」显示成「未检测」。这与 `web_parse._fmt_epoch` 收的是同一
    种越界（那里专门 catch 了 OverflowError），口径也照旧：认不出的时间给 None，
    其余字段一个字不改。
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        v = float(v)
    except OverflowError:
        return None
    if not math.isfinite(v) or v <= 0:
        return None
    return v


def normalize_state(raw):
    """把磁盘上那一份洗成读者认得的样子；洗不出来回 None。

    读的三处只认这四种键；多出来的键丢掉，缺的 / 类型不对的按「不知道」处理
    （时间给 None，界面据此不印那半句）。**不猜**：`logged_in` 只认真正的 `true`，
    字符串 "false"、数字 1 之类一律当未登录 —— 谎报「已连接」比说「未检测」更坏。
    """
    if not isinstance(raw, dict):
        return None
    return {"logged_in": raw.get("logged_in") is True,
            "checked_at": _seconds(raw.get("checked_at")),
            "url": raw.get("url") if isinstance(raw.get("url"), str) else "",
            "note": raw.get("note") if isinstance(raw.get("note"), str) else ""}


def read_state(path=STATE_PATH):
    """读账本唯一入口：坏 JSON / 半截档 / 脏形状都回 None，绝不把脏值往下游传。"""
    try:
        # utf-8-sig：手改 / 另存过的账本可能带 BOM，按 utf-8 读会整份解不动（被当空账）。
        with open(path, encoding="utf-8-sig") as f:
            return normalize_state(json.load(f))
    except Exception:
        return None


def _discard(tmp):
    """收掉这一次写留下的临时件；收不掉（已被搬走 / 权限 / 盘只读）也不算错。"""
    try:
        os.remove(tmp)
    except OSError:
        pass


def write_state(logged_in, url, note="", path=STATE_PATH):
    """写账本唯一入口：原子替换，中途崩了也不会留下半截文件把旧档毁掉。

    原来直接 `open(path, "w")` —— 那一瞬间就把旧账截成 0 字节，进程要是在
    写新内容之前被收走（用户点了停止、退出应用），磁盘上就只剩一个空文件，
    界面从此显示「未检测」直到下次登录。改成先写 `.tmp` 再 `os.replace`，
    旧档在整个过程里始终是完整的；写失败时把 `.tmp` 收掉再抛，不留垃圾。

    收残件那一句兜的是 `BaseException` 而不是 `Exception`：`KeyboardInterrupt` /
    `SystemExit` 不继承 `Exception`，只兜 `Exception` 会在退出路径上留一个
    `cache/login_state.json.tmp`（终端里手动跑 `login.py` 时 Ctrl-C 落在这一句
    附近就是；`login.py` 自带 `__main__`，这条路真实存在）。与
    `flomo_notes` / `mindmap` / `writer` / `ai_sum` 那几格同一条口径。

    **临时件名仍是固定的 `path + ".tmp"`，这是有意保留的**：`tests/check_login.py`
    把这一条名字形状钉成了源码断言（「走的是 .tmp + os.replace 那一套」），改名会
    动到那份既有门禁。全仓固定名就只剩这一处；`cache/login_state.json` 的写手只有
    `login.py` 自己（`:141` / `:183` / `:186` / `:189`），而它走 `start_task("login", …)`
    那把全局单任务闸，应用流程里同一时刻只有一趟 —— 不同进程各锁各的那一档（终端里
    手动跑一趟）才够得着固定名，本轮没有改它，改之前先照 `check_login.py` 那条断言
    一起收。残件仍归「清除本地数据」那把刀（`cleanup._residue_re` 认
    `<账本>.tmp` 与 `<账本>.<pid>.<八位随机>.tmp` 两种形状）。
    """
    payload = normalize_state({"logged_in": logged_in, "checked_at": time.time(),
                               "url": url, "note": note})
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        _discard(tmp)
        raise
    return payload


async def is_logged_in(ctx):
    try:
        cookies = await ctx.cookies("https://weread.qq.com")
    except Exception:
        return False
    return any(c.get("name") == "wr_vid" and (c.get("value") or "").strip()
               for c in cookies)


async def main():
    # playwright 只在真要开浏览器时才 import：这个模块同时被 ui_server 拿去做
    # 「读登录态」这件纯逻辑小事（见 ui_server.read_login_state），而 ui_server
    # 在首次运行、虚拟环境还没建好的时候就要能起来。顶层 import 会让
    # `import login` 连带把 playwright 拉进来 —— 那一步在装好依赖之前是 ImportError。
    from playwright.async_api import async_playwright

    # 开浏览器之前收尾一次环境：抹掉宿主留下的 XPC_FLAGS 0x2 位、把指向空目录的
    # PLAYWRIGHT_BROWSERS_PATH 纠回默认位置（详见 platform_compat 里那两个函数）。
    pc.ensure_browser_env()

    os.makedirs(USER_DATA_DIR, exist_ok=True)
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            USER_DATA_DIR, headless=False, viewport={"width": 1200, "height": 900},
            args=["--disable-blink-features=AutomationControlled"])
        page = await ctx.new_page()
        print("  打开书架页…", flush=True)
        try:
            await page.goto(SHELF_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"  ⚠️  页面打开异常: {e}", flush=True)
        await asyncio.sleep(4)

        if await is_logged_in(ctx):
            print("  ✅ 已登录（会话仍然有效）", flush=True)
            write_state(True, page.url, "会话有效")
            await asyncio.sleep(2)
            await ctx.close()
            return

        print("  ⚠️  未登录，正在打开扫码窗口…", flush=True)
        try:
            await page.click(".navBar_link_Login", timeout=6000)
        except Exception:
            try:
                await page.get_by_text("登录", exact=True).first.click(timeout=5000)
            except Exception as e:
                print(f"  ⚠️  未能自动点开扫码框，请手动点右上角「登录」：{e}", flush=True)
        await asyncio.sleep(2)
        print("  📱 请用微信扫窗口里的二维码（最长等 5 分钟）", flush=True)

        # 收场时要写进 state 的那个地址：页面关掉之后 page.url 就不一定还在了，
        # 所以在开等之前先记下来。
        try:
            last_url = page.url
        except Exception:
            last_url = SHELF_URL

        deadline = time.time() + SCAN_TIMEOUT
        logged = False
        gone = False
        while time.time() < deadline:
            await asyncio.sleep(3)
            # 用户把扫码窗口关掉了 / 浏览器没了：再等满 5 分钟只是让他对着一个
            # 不动的转圈发呆。认出来就立刻收工，由调用方决定下一步。
            if page.is_closed():
                gone = True
                break
            try:
                if await is_logged_in(ctx):
                    logged = True
                    break
            except Exception:
                gone = True
                break
        if logged:
            print("  ✅ 登录成功，会话已保存到 cache/browser_profile", flush=True)
            write_state(True, last_url, "扫码登录成功")
        elif gone:
            print("  ⚠️  扫码窗口已关闭，不再等待", flush=True)
            write_state(False, last_url, "窗口已关闭")
        else:
            print("  ❌ 等待扫码超时（5 分钟）", flush=True)
            write_state(False, last_url, "扫码超时")
        await asyncio.sleep(1)
        try:
            await ctx.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
