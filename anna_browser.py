#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安娜的档案：工具自己开一个可见的浏览器窗口，用户在网页里搜书、点下载，
归藏把下载接住、转成书收进「本地书架」。

为什么是「开一个窗口」而不是「在页面里嵌一个」：这类站点的回包带着
`X-Frame-Options: SAMEORIGIN`，浏览器不允许把它嵌进 127.0.0.1 的界面里；就算
绕过了嵌套，跨源也读不到里面的链接、更接不住下载。所以浏览器该由工具自己开 ——
用户在真页面里怎么搜、怎么点都是站点自己的事，工具只干两件事：把文件接住，
把书收进书架。这条和微信读书扫码、上一版 Z-Library 登录是同一个套路。

为什么要「有头」（看得见窗口）：这类站点在 TLS 层就挡机器 —— 直接 urllib 撞
`UNEXPECTED_EOF_WHILE_READING`，无头 Chromium 撞 Access Denied，只有能看见的窗口
能正常出搜索结果。这里 `headless=False` 是正确性要求，不是口味。

隐私：日志与状态里只有文件名和字节数，绝不写 cookie、不写令牌、不写绝对路径。
浏览器档案单开一份（`cache/anna_profile`），不与取书那份共用 —— Playwright 的
持久化档案同一时刻只能被一个实例占用，共用会撞「profile 已被锁定」。
"""

import os
import signal
import time
import uuid

import platform_compat as pc  # noqa: F401  —— 导入即抹掉 XPC_FLAGS 的 0x2 位（见该模块说明）

import anna_state as anna

PROFILE_DIR = os.path.join("cache", "anna_profile")
PUMP_MS = 900            # 事件泵一轮等多久：太短白烧 CPU，太长下载落地了界面还不动
CMD_POLL_S = 1.0         # 多久看一眼「该搜这个词」那块牌子
HARD_LIVE_S = 4 * 3600   # 兜底：窗口开超过 4 小时一定收摊，别留僵尸进程占着任务槽

_STOP = {"now": False}


def data_dir():
    """账本与档案落在哪儿：装成 app 时后端用 GUIZANG_DATA 指到用户目录，
    源码直接跑时工作目录本身就是数据目录（start_task 的 cwd 就是它）。"""
    return (os.environ.get("GUIZANG_DATA") or "").strip() or os.getcwd()


def shelf_dir(dd):
    """本地书架那一格。后端起这条任务时已经把 GUIZANG_OUTPUT 指过来了。"""
    d = (os.environ.get("GUIZANG_OUTPUT") or "").strip()
    if d:
        return os.path.expanduser(d)
    import book_layout
    import platform_compat as pc
    repo = os.path.dirname(os.path.abspath(__file__))
    return book_layout.book_dir(pc.books_dir(repo), "local")


def covers_dir(dd):
    return os.path.join(dd, "cache", "covers")


def log(line):
    """一行行往外说：stdout 会被后端收进日志，界面「查看详情」看到的就是这些。"""
    print(line, flush=True)


def browser_args(proxy_url=""):
    """启动参数。代理要显式挂给浏览器：这类站点的域名在国内解析到的常常是假地址，
    Chromium 不一定照系统设置走（装成 app 时尤其），探到了就钉上去，探不到当直连。"""
    args = ["--disable-blink-features=AutomationControlled",
            "--no-first-run", "--no-default-browser-check",
            "--window-size=1280,900"]
    p = (proxy_url or "").strip()
    if p:
        args.append("--proxy-server=%s" % p)
    return args


def open_first_page(page, dd, term=""):
    """按镜像列表一个个试，直到有一个打开。回 (是否打开, 打开用的域名)。

    对方轮换镜像是常事：只认一个域会让「今天还能用」变成「明天全打不开」。域名
    试成功了就顺手记回账本，下次直接从能用的那个开始。
    """
    st = anna.read_state(dd)
    kw = str(term or "").strip() or str(st.get("keyword") or "").strip()
    for dom in anna.domain_candidates(st.get("domain")):
        url = anna.search_url(kw, dom) if kw else anna.with_scheme(dom) + "/"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            log("这个镜像没打开：%s" % dom)
            continue
        if dom != anna.clean_domain(st.get("domain")):
            anna.write_state(dd, {"domain": dom})
            log("换用能打开的镜像：%s" % dom)
        return True, dom
    return False, ""


def _part_name(target):
    """临时件名：目标旁边 + 进程号 + 一小串随机，以 `.part` 收尾。

    与 `sync._write_atomic` / `download_images.download_one` / `ffmpeg_tool._download`
    同一族：名字一次一个，两个写手不共用同一个件；以 `.part` 收尾的普通文件按定义
    就是没写完的中间件（书目录里那一刀由 `ui_server.drop_stray_tmps` 收，下载格
    这一头由「清除本地数据」整格带走）。
    """
    return "%s.%d.%s.part" % (target, os.getpid(), uuid.uuid4().hex[:8])


def _drop(path):
    """收残件：收不掉也不算错（它不属于任何读口，最多占一份盘）。"""
    try:
        os.remove(path)
    except OSError:
        pass


def handle_download(dd, queue, download):
    """接住一份下载：先原样落到原始下载格，再排队等主循环转成书。

    这一步只做「存下来」，不做解析：EPUB 拆章要几秒，卡在事件回调里会把后面的
    下载和点词一起拖住。存完把路径丢进队列，主循环一件件处理。

    落盘走「先写同目录 `.part`、下完整了再一次改名」这把尺子，而不是让 `save_as`
    直接往最终名上写。理由与插图 / 云同步 / 装组件那三处同一条：写到一半被打断
    （用户关掉窗口、站点把连接断了、磁盘满、进程被收走）会在**最终名**上留一个
    长度不对却「存在」的半截件 —— 它看着像一份已经接住的下载，「补收」下一轮拿它
    去解析，用户等到的是一句「这个 zip 打不开」；而且同名文件此后一律加时间戳
    绕开它，那份半截件再也没人重下。写 `.part` 再改名之后，最终名上要么没有文件、
    要么是一份完整的下载；同名的下一份因此走得通，不会被一个坏件顶掉。
    """
    name = anna.safe_name(getattr(download, "suggested_filename", "") or "")
    target = os.path.join(anna.incoming_dir(dd, create=True), name)
    # 同名不覆盖：加个时间戳尾巴。用户连着下两份不同东西撞到同一个名字是常事。
    if os.path.exists(target):
        stem, dot, tail = name.rpartition(".")
        if not stem:
            stem, dot, tail = name, "", ""
        folder = os.path.dirname(target)
        stamp = int(time.time())
        name = "%s-%d%s%s" % (stem, stamp, dot, tail)
        target = os.path.join(folder, name)
        # 时间戳只精确到秒：同一秒里连着接住三份同名文件时，后两份会算出同一个
        # 名字，save_as 就把上一份刚接住的覆盖掉（用户下了四本、书架里只剩两本）。
        # 所以撞上了再补一个序号，撞一次加一个，直到真的空出来为止。
        n = 1
        while os.path.exists(target):
            n += 1
            name = "%s-%d-%d%s%s" % (stem, stamp, n, dot, tail)
            target = os.path.join(folder, name)
    tmp = _part_name(target)
    try:
        download.save_as(tmp)
        os.replace(tmp, target)
    except Exception as e:
        # 写这一步失败：半截件收干净再报，最终名上不许留下任何东西 —— 下一份同名
        # 的下载还得用回这个名字（不会被一个没人认领的坏件顶去加时间戳）。
        _drop(tmp)
        msg = "这份没接住：%s" % (type(e).__name__)
        log(msg)
        anna.counted(dd, {"note": msg, "last_error": msg})
        return
    except BaseException:
        # 中止信号（KeyboardInterrupt / SystemExit）不继承 Exception：收掉半截件之后
        # 原样往上传，别把一次收摊装成「这份没接住」。
        _drop(tmp)
        raise
    size = os.path.getsize(target) if os.path.exists(target) else 0
    log("接到一份下载：%s（%d KB）" % (name, max(size // 1024, 0)))
    anna.counted(dd, {"caught_delta": 1, "note": "接到一份下载：%s" % name,
                      "window": "open"})
    queue.append((target, name))


def drain(dd, shelf, cover, queue):
    """把排队的下载一份份转成书。回这一轮处理了几份。"""
    n = 0
    while queue:
        path, name = queue.pop(0)
        res = anna.ingest(shelf, cover, path, data_dir=dd)
        log(("入库：" if res.get("ok") else "没入库：") + str(res.get("msg") or name))
        n += 1
    return n


def check_cmd(dd, seen, page):
    """看一眼牌子：界面里换了关键词就把窗口领过去。回新的「见过」标记。"""
    payload = anna.read_cmd(dd)
    stamp = (payload.get("term"), payload.get("ts"))
    if stamp == seen:
        return seen
    term = str(payload.get("term") or "").strip()
    if not term:
        return stamp
    url = anna.search_url(term, anna.read_state(dd).get("domain"))
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        anna.write_state(dd, {"keyword": term, "note": "窗口已经去搜：%s" % term})
        log("按界面给的关键词去搜：%s" % term)
    except Exception as e:
        log("这一页没打开：%s" % type(e).__name__)
        anna.write_state(dd, {"last_error": "搜索页没打开"})
    return stamp


def alive(ctx):
    """窗口还在不在：用户把最后一个标签关了就算走完了。"""
    try:
        return bool(ctx.pages)
    except Exception:
        return False


def run():
    dd = data_dir()
    shelf = shelf_dir(dd)
    cover = covers_dir(dd)
    anna.write_state(dd, {"window": "opening", "note": "正在打开安娜的档案窗口…",
                          "last_error": ""})
    proxy = anna.proxy_url()
    log("打开安娜的档案窗口。搜书、点下载都在窗口里做，归藏在这头接文件。")
    log("最长开 4 小时会自动收摊；关掉窗口或点「关掉窗口」就结束。"
        + ("" if not proxy else " 走本机代理 %s。" % proxy.split("//")[-1].split("@")[-1]))

    queue = []
    # 开窗口之前收尾一次环境：抹掉宿主留下的 XPC_FLAGS 0x2 位、把指向空目录的
    # PLAYWRIGHT_BROWSERS_PATH 纠回默认位置（详见 platform_compat 里那两个函数）。
    pc.ensure_browser_env()
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        msg = "浏览器组件没就位（%s）：先到设置里装好「浏览器组件」再来一次。" % type(e).__name__
        log(msg)
        anna.write_state(dd, {"window": "failed", "last_error": msg, "note": msg})
        return 1

    os.makedirs(anna.incoming_dir(dd, create=True), exist_ok=True)
    os.makedirs(PROFILE_DIR, exist_ok=True)
    code = 0
    ctx = None
    seen_cmd = (None, None)
    try:
        with sync_playwright() as p:
            try:
                ctx = p.chromium.launch_persistent_context(
                    PROFILE_DIR, headless=False, viewport={"width": 1260, "height": 860},
                    accept_downloads=True, downloads_path=anna.incoming_dir(dd),
                    args=browser_args(proxy))
            except Exception as e:
                msg = "窗口没开起来：%s" % str(e)[:160]
                log(msg)
                anna.write_state(dd, {"window": "failed", "last_error": msg, "note": msg})
                return 1
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                ctx.on("download", lambda dl: handle_download(dd, queue, dl))
                opened, _dom = open_first_page(page, dd, "")
                if not opened:
                    msg = "镜像一个都打不开 —— 对方可能又换了域名，或代理没开。" \
                          "到这一栏里改镜像域名，确认代理开着，再来一次。"
                    log(msg)
                    anna.write_state(dd, {"window": "failed", "last_error": msg, "note": msg})
                    return 1
                anna.write_state(dd, {"window": "open", "note": "窗口开着，等你搜书下载"})
                log("窗口已经打开，可以搜书了。")
                seen_cmd = (anna.read_cmd(dd).get("term"), anna.read_cmd(dd).get("ts"))

                started = time.time()
                last_beat = 0.0
                while True:
                    if _STOP["now"]:
                        log("已经中止，窗口收摊。")
                        break
                    if not alive(ctx):
                        log("窗口被关掉了，这一趟到此。")
                        break
                    if time.time() - started > HARD_LIVE_S:
                        log("开着超过 4 小时了，先替你收摊（占着任务槽会影响取书）。")
                        break
                    try:
                        page.wait_for_timeout(PUMP_MS)      # 事件泵：下载回调在这一步里跑
                    except Exception:
                        if not alive(ctx):
                            log("窗口被关掉了，这一趟到此。")
                            break
                        page = (ctx.pages[-1] if ctx.pages else ctx.new_page())
                    drain(dd, shelf, cover, queue)
                    seen_cmd = check_cmd(dd, seen_cmd, page)
                    if time.time() - last_beat > 60:
                        last_beat = time.time()
                        st = anna.read_state(dd)
                        log("窗口还开着：已接住 %d 份，入库 %d 本。"
                            % (st.get("caught") or 0, st.get("imported") or 0))
            finally:
                if ctx is not None:
                    try:
                        ctx.close()
                    except Exception:
                        pass
    except Exception as e:
        log("这一趟没走通：%s: %s" % (type(e).__name__, str(e)[:160]))
        code = 1
    # 收尾：把窗口状态落回 closed，并补一句这条路的累计（用户回来一眼能看见成果）。
    st = anna.write_state(dd, {"window": "closed"})
    log("这一趟结束：窗口里接住 %d 份，收进书架 %d 本，没收进来的 %d 份。"
        % (st.get("caught") or 0, st.get("imported") or 0, st.get("skipped") or 0))
    return code


def _on_stop(_signum, _frame):
    """点「关掉窗口」= 后端发 SIGTERM：立个牌子，主循环看见才收（窗口要关，别留孤儿）。"""
    _STOP["now"] = True


if __name__ == "__main__":
    for _sig in ("SIGTERM", "SIGINT", "SIGHUP"):
        _h = getattr(signal, _sig, None)
        if _h is not None:
            try:
                signal.signal(_h, _on_stop)
            except Exception:
                pass
    raise SystemExit(run())
