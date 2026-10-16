#!/usr/bin/env python3
"""独立图片下载器：强制 IPv4 + 并发 + 重试。读 raw/*.json 里的图片URL下到 images/。

落盘规矩只有一条：**正式文件只能靠自己长出来**。先写同目录的 `*.part`，下完整了
再 `os.replace` 改名过去。这样「images/xxx.jpg 存在」就等价于「这张下完了」，
重跑时的跳过判断才站得住。

上一版是 `open(fpath, "wb").write(raw)`——先清空再写。中断 / 磁盘满会在原地留下
半截图；下一轮只看大小（>1KB 就跳过），半截图正好过线，于是被当成下好了，
一张裂图就此钉死到用户手动去删。所以除了原子改名，这里还顺着文件头认一眼
是不是真图片，老版本留下的坏文件也能重下。

地址也要过一道闸：`url` 是从网页扒下来的，默认 opener 连 `file:///etc/passwd`
都会读回来写进 images/（之后还会跟着书库同步出去）。跳转那道闸只管跳转目标，
管不了「一开始喂进来的就是 file://」，所以取之前先按 clip_article 那一份 scheme
名单核一遍；清单阶段的跳过与 download_one 自己的拒绝共用同一个判据。
"""
import json, glob, os, re, socket, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import platform_compat as pc
import clip_article

# 书库位置：默认相对 cwd 的 output/，壳/后端用 GUIZANG_OUTPUT 指到「文档/归藏」。
OUTPUT_DIR = pc.output_dir()

# 小于这个字节数算没下到；大于这个字节数才有资格当「已下好」。
MIN_BYTES = 500
SKIP_BYTES = 1000

HEADERS = {"Referer": "https://weread.qq.com/", "User-Agent": "Mozilla/5.0"}

# 认得出来的图片文件头。export_precise.img_filename 只产 jpg / png / gif / webp，
# 多列 bmp / tiff 只是不想把更早的导出里的图误判成坏文件。
_MAGIC = (
    b"\xff\xd8\xff",          # jpeg
    b"\x89PNG\r\n\x1a\n",     # png
    b"GIF87a", b"GIF89a",     # gif
    b"BM",                    # bmp
    b"II*\x00", b"MM\x00*",   # tiff
)


def head_is_image(head):
    """看字节头认图。认不出给 False —— 宁可白重下一遍，别把坏文件当成品。"""
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True
    return any(head.startswith(sig) for sig in _MAGIC)


def force_ipv4():
    """强制 IPv4（macOS 上 IPv6 路由不通会导致每次连接卡 ~120s）。

    以前是在 import 那一下直接换掉 socket.getaddrinfo，于是任何
    `import download_images`（测试、MCP、别的工具）都会顺手把整个进程的名字
    解析改掉，换过之后还没有还原的路。现在只在真要下载时调一次；重复调用
    幂等，不会一层套一层。
    """
    if getattr(socket.getaddrinfo, "_guizang_ipv4_only", False):
        return
    orig = socket.getaddrinfo

    def ipv4_only(*args, **kwargs):
        return [x for x in orig(*args, **kwargs) if x[0] == socket.AF_INET]

    ipv4_only._guizang_ipv4_only = True
    socket.getaddrinfo = ipv4_only


def is_complete(fpath, min_bytes=SKIP_BYTES):
    """fpath 处是不是已经躺着一份完整的图。

    只看大小不够：老版本的非原子写留下的半截图只要过了 1KB 就会被永远跳过。
    所以大小达标之后再看一眼文件头，认不出就当没下好、重下。

    连 `os.path.getsize` 抛的东西一起收：路径里有 NUL 时它抛的是 ValueError，
    有落单的代理码点时是 UnicodeEncodeError —— 都不是 OSError，只认 OSError 的话
    这一条会把异常甩给调用方。getsize 都算不出来的路径，答案本来就是「没下好」。
    """
    try:
        if os.path.getsize(fpath) < min_bytes:
            return False
        with open(fpath, "rb") as fh:
            return head_is_image(fh.read(12))
    except (OSError, ValueError):
        return False


def fetchable_url(url):
    """这个地址够不够格替用户去取：只认 http / https。

    `url` 是从网页扒下来的 raw/*.json 里的外部数据，什么都可能是：

      · `file:///etc/passwd` —— 默认 opener 会老老实实把本机文件读回来写进
        images/，之后还会跟着书库同步出去；`data:` / `ftp:` / `gopher:` 同理，
        都不是「下了一张插图」；
      · 半截 IPv6（`http://[::1`）让 urlparse 自己抛 ValueError —— 一条脏记录
        不该带走整批图，解不出来一律当不合法。

    scheme 名单不在这里另抄一份：与剪藏 / 平台解析 / 内置浏览器共用
    `clip_article.ALLOWED_SCHEMES`，以后加规则只改一处。
    """
    if not isinstance(url, str):
        return False
    parts = clip_article.split_url(url)
    return bool(parts) and parts[0] in clip_article.ALLOWED_SCHEMES


def download_one(url, fpath, retries=3, min_bytes=MIN_BYTES):
    """下一张图，返回 "ok" / "skip" / "fail:…"。

    非原子写的代价不是白下一张，是留下一个坏文件：下一轮只看大小就把它当成品，
    此后再也不重下。先写 fpath.part 再 os.replace 之后，fpath 存在即完整。
    """
    if is_complete(fpath):
        return "skip"
    if not fetchable_url(url):
        # 一开始就不该取的地址（file: / data: / scheme 空）：一次都别发，
        # 也别把重试次数浪费在它身上。
        return "fail:blocked-url"
    tmp = fpath + ".part"
    last = "fail:empty"
    for attempt in range(retries):
        try:
            # 图片地址是从网页扒下来的 raw/*.json 里读出来的，站点把我们 302 到内网
            # 也不算数：先装上跳转那道闸（与剪藏共用一份，见 ensure_redirect_guard）。
            clip_article.ensure_redirect_guard()
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
            if len(raw) >= min_bytes:
                with open(tmp, "wb") as fh:
                    fh.write(raw)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, fpath)
                return "ok"
            last = "fail:empty"
        except Exception as e:
            last = f"fail:{e}"
        finally:
            try:
                os.unlink(tmp)
            except (OSError, ValueError):
                # 清理残件顺带失败（含路径本身不合法）不该把这一张的失败升级成整批
                pass
        if attempt < retries - 1:
            time.sleep(1.5)
    return last


# 写不出合法路径的文件名：NUL 与 C0/DEL 控制字符，以及落单的代理码点
# （U+D800–U+DFFF，JSON 里塞进来的半个字符）。它们不是「下不下来这一张」的小事：
# os.path.getsize / open 对这样的路径抛的是 ValueError / UnicodeEncodeError（不是
# OSError），会穿出 is_complete，也会穿出 download_one 的 finally 清理，最后在
# main 的 fut.result() 当场抛掉 —— 一条脏记录带走整批图。与 `../` 同一档：不合法。
_BAD_NAME = re.compile("[\x00-\x1f\x7f\ud800-\udfff]")


def safe_target(img_dir, name):
    """raw 里的文件名 -> images/ 下的落点；拿不准的返回 None。

    `file` 名义上是 export_precise 生成的 ch0001_img01.jpg，但 raw/*.json 整份
    都是从网页上扒下来的数据，不该假定它一定乖。带路径分隔符 / 绝对路径 /
    `..` / 控制字符 / 落单的代理码点一律不收，越出 images/ 的落点也不收。
    """
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    if _BAD_NAME.search(name):
        return None
    if os.path.isabs(name) or "/" in name or "\\" in name or name in (".", ".."):
        return None
    target = os.path.normpath(os.path.join(img_dir, name))
    if os.path.normpath(os.path.dirname(target)) != os.path.normpath(img_dir):
        return None
    return target


def collect_tasks(raw_dir, img_dir):
    """把 raw/*.json 收成 [(url, 落点)]，返回 (tasks, 跳过条数)。

    单张图的坏记录不该让整本书都下不了：坏 JSON、缺 url/file、越界文件名都只
    记数不抛。同一个落点撞两次也合并——两条线程写同一个文件，最后留下谁的
    半截说不准。
    """
    tasks, skipped, seen = [], 0, set()
    for jf in sorted(glob.glob(os.path.join(raw_dir, "*.json"))):
        try:
            # utf-8-sig：raw/*.json 是用户能手改、云同步也可能回写的档，记事本
            # 「另存为 UTF-8」会在开头多塞三字节 BOM；裸 utf-8 解不动、被 `except`
            # 当成坏记录整份跳过，那一页的插图一张都不重下，界面上只少几张图、
            # 看不出为什么。与 `book_layout.read_meta` 那一族读口同一把尺子：
            # 写口仍旧 utf-8，只有读口认 BOM。
            with open(jf, "r", encoding="utf-8-sig") as fh:
                doc = json.load(fh)
        except Exception:
            skipped += 1
            continue
        images = doc.get("images") if isinstance(doc, dict) else None
        if not isinstance(images, list):
            continue
        for im in images:
            if not isinstance(im, dict):
                skipped += 1
                continue
            url = im.get("url")
            target = safe_target(img_dir, im.get("file"))
            # 认不出的 scheme（file: / data: / ftp: / 空）与缺 url / 越界文件名同一档：
            # 只跳过这一条，不进任务清单。download_one 自己还会再核一次（它是公开入口，
            # 导出收尾那条路直接调它）。
            if not isinstance(url, str) or not url.strip() or target is None:
                skipped += 1
                continue
            if not fetchable_url(url):
                skipped += 1
                continue
            if target in seen:
                skipped += 1
                continue
            seen.add(target)
            tasks.append((url.strip(), target))
    return tasks, skipped


def main(book_id):
    book_dir = os.path.join(OUTPUT_DIR, book_id)
    raw_dir = os.path.join(book_dir, "raw")
    img_dir = os.path.join(book_dir, "images")
    os.makedirs(img_dir, exist_ok=True)

    tasks, skipped = collect_tasks(raw_dir, img_dir)
    if not tasks:
        print(f"没有可下的图片（{raw_dir} 下没有带 images 的 raw/*.json）")
        if skipped:
            print(f"   跳过 {skipped} 条读不出来的记录")
        return
    force_ipv4()
    tail = f"（另有 {skipped} 条坏记录跳过）" if skipped else ""
    print(f"共 {len(tasks)} 张图片，8 线程并发下载（强制IPv4）...{tail}")
    ok = skip = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(download_one, u, f): f for u, f in tasks}
        done = 0
        for fut in as_completed(futs):
            r = fut.result(); done += 1
            if r == "ok": ok += 1
            elif r == "skip": skip += 1
            else:
                fail += 1
                print(f"  ⚠️  {os.path.basename(futs[fut])}: {r}")
            if done % 50 == 0:
                print(f"  {done}/{len(tasks)}  (ok={ok} skip={skip} fail={fail})  {time.time()-t0:.0f}s")

    print(f"\n✅ 完成: ok={ok} skip={skip} fail={fail}  耗时 {time.time()-t0:.0f}s")
    print(f"   图片目录: {img_dir}")


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("用法: download_images.py <书城书号 或 该书导出目录>")
        sys.exit(2)
    raw = sys.argv[1].strip().rstrip("/\\")
    book_id = raw.replace("\\", "/").split("/")[-1] if "weread" in raw else raw
    main(book_id)
