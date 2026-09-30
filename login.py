#!/usr/bin/env python3
"""微信读书登录助手。

判定以 wr_vid cookie 为准（未登录时 /web/shelf 不会跳转，URL 判断会误报）。
未登录则自动点开扫码弹窗，等用户扫码。结果写入 cache/login_state.json 供 Web 界面读取。
持久化 profile 与 export_precise.py 共用（cache/browser_profile）。
"""
import asyncio
import json
import os
import time

from playwright.async_api import async_playwright

USER_DATA_DIR = os.path.join("cache", "browser_profile")
STATE_PATH = os.path.join("cache", "login_state.json")
SHELF_URL = "https://weread.qq.com/web/shelf"
SCAN_TIMEOUT = 300


async def is_logged_in(ctx):
    try:
        cookies = await ctx.cookies("https://weread.qq.com")
    except Exception:
        return False
    return any(c.get("name") == "wr_vid" and (c.get("value") or "").strip()
               for c in cookies)


def write_state(logged_in, url, note=""):
    os.makedirs("cache", exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"logged_in": logged_in, "checked_at": time.time(),
                   "url": url, "note": note}, f, ensure_ascii=False)


async def main():
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

        deadline = time.time() + SCAN_TIMEOUT
        logged = False
        while time.time() < deadline:
            await asyncio.sleep(3)
            if await is_logged_in(ctx):
                logged = True
                break
        if logged:
            print("  ✅ 登录成功，会话已保存到 cache/browser_profile", flush=True)
            write_state(True, page.url, "扫码登录成功")
        else:
            print("  ❌ 等待扫码超时（5 分钟）", flush=True)
            write_state(False, page.url, "扫码超时")
        await asyncio.sleep(1)
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
