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

USER_DATA_DIR = os.path.join("cache", "browser_profile")
STATE_PATH = os.path.join("cache", "login_state.json")
SHELF_URL = "https://weread.qq.com/web/shelf"
SCAN_TIMEOUT = 300


def _seconds(v):
    """只认真正的秒数：布尔是 int 的子类不算，字符串一律不算。

    `"昨天" * 1000` 是 NaN、`True * 1000` 是 1000（1970 年那条假时间），
    两者都会让界面和 MCP 印出一句看着像后端坏了的胡话。
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
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
        with open(path, encoding="utf-8") as f:
            return normalize_state(json.load(f))
    except Exception:
        return None


def write_state(logged_in, url, note="", path=STATE_PATH):
    """写账本唯一入口：原子替换，中途崩了也不会留下半截文件把旧档毁掉。

    原来直接 `open(path, "w")` —— 那一瞬间就把旧账截成 0 字节，进程要是在
    写新内容之前被收走（用户点了停止、退出应用），磁盘上就只剩一个空文件，
    界面从此显示「未检测」直到下次登录。改成先写 `.tmp` 再 `os.replace`，
    旧档在整个过程里始终是完整的；写失败时把 `.tmp` 收掉再抛，不留垃圾。
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
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
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
