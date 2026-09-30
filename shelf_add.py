#!/usr/bin/env python3
"""把书加进微信读书书架 —— 官方 Agent Gateway 只读，写这一步走网页端接口。

实测依据（2026-09-30，同一 profile 做对照实验）：
    POST https://weread.qq.com/mp/shelf/addToShelf   body {"bookIds": ["<书城短id>"]}
    成功 {"status":200,"succ":1,"errCode":0}
    回包 {"status":500,"errcode":-2012,"errmsg":"登录超时"} 不是「书有问题」，
    也不是「已在书架」，而是会话里的短期凭证 wr_skey 到期了（wr_vid 还在，
    所以只看 wr_vid 判登录会误判成「已登录」）。
    用同一个 profile 先访问一次 weread.qq.com 首页，服务端会重新下发 wr_skey，
    这条请求立刻就能成 —— 所以这里先续期再发，续期完仍失败才叫真没登录。
接口不校验页面那套 x-wrpa-* 反爬签名头。

bookIds 必须是书城短 id（/store/search 里 bookInfo.bookId 那种），
不是阅读器 deepLink 里的长 id —— 传错了接口照样回 succ:0。

用法: shelf_add.py <bookId> [bookId...]
输出: stdout 一行 JSON，供 ui_server.py 直接转给前端。
"""
import asyncio
import json
import os
import re
import sys
import time

from playwright.async_api import async_playwright

import platform_compat as pc

USER_DATA_DIR = os.path.join("cache", "browser_profile")
ADD_URL = "https://weread.qq.com/mp/shelf/addToShelf"

# 只翻实测过的码，别的照抄服务端自己的 errmsg —— 猜来的措辞会把人往错的方向带。
ERRCODE = {
    -2012: "登录过期",
}

CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
]


def fail(msg, code=1):
    print(json.dumps({"ok": False, "msg": msg}, ensure_ascii=False), flush=True)
    sys.exit(code)


def clean_ids(raw):
    """只留得住书架格式的 id：纯数字短 id。顺手挡掉误传的阅读器长 id / 链接。"""
    out = []
    for s in raw:
        s = (s or "").strip()
        if re.fullmatch(r"\d{5,12}", s) and s not in out:
            out.append(s)
    return out


def kill_stray_browsers(wait=0.8):
    """profile 被上一轮残留的浏览器锁住时，persistent context 起不来。

    按 profile 绝对路径精确匹配，不会误伤用户自己开着的 Chrome。
    实现走 platform_compat —— 原来直接调 pkill，Windows 没有这个命令，
    异常被吞掉之后看着「正常」，锁却一直没解开。
    """
    pc.kill_stray_browsers(USER_DATA_DIR, wait)


async def session_state(ctx):
    """看两套 cookie：wr_vid=是不是这个人，wr_skey=会话还在不在有效期。

    wr_vid 长期不过期，只查它会误判「已登录」；/mp 接口真正卡的是 wr_skey。
    """
    cookies = await ctx.cookies("https://weread.qq.com")
    have = {c.get("name") for c in cookies
            if (c.get("value") or "").strip()}
    return "wr_vid" in have, "wr_skey" in have


async def renew_session(ctx):
    """去 weread 首页走一趟，服务端会重新下发 wr_skey（实测有效）。

    不点任何东西，也不抓正文，就是让浏览器带 cookie 访问一次，几秒的事。
    """
    page = await ctx.new_page()
    try:
        await page.goto("https://weread.qq.com/",
                        wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(2000)
    finally:
        await page.close()


async def post_add(ctx, ids):
    """发一次加书架请求，返回 (HTTP 状态, 回包 dict, 原文)。

    接口只认登录 cookie，不校验页面那套签名头；referer 只是让请求形状和网页端一致。
    """
    resp = await ctx.request.post(
        ADD_URL,
        data=json.dumps({"bookIds": ids}),
        headers={"content-type": "application/json",
                 "referer": "https://weread.qq.com/web/shelf"},
        timeout=20000)
    text = await resp.text()
    try:
        body = json.loads(text)
    except Exception:
        body = None
    return resp.status, body, text


async def main():
    ids = clean_ids([a for a in sys.argv[1:] if not a.startswith("--")])
    if not ids:
        fail("没有可用的书籍编号（需要书城短 id）")

    async with async_playwright() as p:
        try:
            ctx = await p.chromium.launch_persistent_context(
                USER_DATA_DIR, headless=True, args=CHROME_ARGS)
        except Exception:
            # 起不来基本都是锁没放掉，清一次再试，仍然失败就如实报
            kill_stray_browsers()
            try:
                ctx = await p.chromium.launch_persistent_context(
                    USER_DATA_DIR, headless=True, args=CHROME_ARGS)
            except Exception:
                fail("打不开已登录的浏览器会话。如果导出任务正在跑，请等它结束后再加书架。")

        try:
            vid, skey = await session_state(ctx)
            if not vid:
                fail("还没登录微信读书，先在界面上点「连接账号」扫码", 3)
            if not skey:
                await renew_session(ctx)
                if not (await session_state(ctx))[1]:
                    fail("微信读书登录过期了，重新扫一次码就好", 3)

            status, body, text = await post_add(ctx, ids)
            if body is None:
                fail(f"接口回包看不懂（HTTP {status}）：{text[:160]}")

            err = body.get("errCode", body.get("errcode", 0)) or 0
            if err == -2012 or "登录" in str(body.get("errmsg", "")):
                # skey 半路失效：续一次再发，还是不行才让用户去扫码
                await renew_session(ctx)
                status, body, text = await post_add(ctx, ids)
                if body is None:
                    fail(f"续期后回包看不懂（HTTP {status}）：{text[:160]}")
                err = body.get("errCode", body.get("errcode", 0)) or 0
                if err < 0:
                    fail("微信读书登录过期了，重新扫一次码就好", 3)

            succ = body.get("succ", 0)
            if err < 0:
                msg = str(body.get("errmsg") or "").strip()
                fail(f"{ERRCODE.get(err) or msg or '没能加进书架'}（错误码 {err}）")
            if status != 200 or not succ:
                fail(f"没能加进书架（HTTP {status}，成功 {succ}/{len(ids)}）")

            done = len(ids) if succ in (True, len(ids)) else int(succ or 0) or len(ids)
            print(json.dumps({"ok": True,
                              "msg": f"已加入书架（{done}/{len(ids)}）",
                              "added": ids, "at": int(time.time())},
                             ensure_ascii=False), flush=True)
        finally:
            await ctx.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        fail("已取消")
    except Exception as e:
        # Playwright 超时会把整段调用栈吐到 stdout，前端只想要一行结论
        fail(f"加书架失败：{type(e).__name__}: {str(e)[:160]}")
