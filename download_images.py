#!/usr/bin/env python3
"""独立图片下载器：强制 IPv4 + 并发 + 重试。读 raw/*.json 里的图片URL下到 images/。

界面「补图」那颗按钮跑的就是这个脚本（ui_server.py 的 action=images）。它读进来的是
磁盘上的 raw/*.json —— 可能来自旧版本、可能被人手改过、也可能是上次导出被中途杀掉时
留下的半截文件 —— 所以这里守三件事：

  * 一份清单读不懂只跳过那一份，不带调用栈把整个补图任务打死；
  * 文件名只认「光名字」，`../` 这种一律拒收，不往书库外面写字节；
  * 先写 `.part` 再改名，images/ 里不会留一张看着存在、其实只有半截的破图。
"""
import json, glob, os, socket, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import platform_compat as pc

# 书库位置：默认相对 cwd 的 output/，壳/后端用 GUIZANG_OUTPUT 指到「文档/归藏」。
OUTPUT_DIR = pc.output_dir()

# 强制 IPv4（macOS 上 IPv6 路由不通会导致每次连接卡 ~120s）
_orig = socket.getaddrinfo
socket.getaddrinfo = lambda *a, **k: [x for x in _orig(*a, **k) if x[0] == socket.AF_INET]

HEADERS = {"Referer": "https://weread.qq.com/", "User-Agent": "Mozilla/5.0"}

# 一张图至少这么多字节才算数。下载时认它，下次判断「已下好」也必须用同一把尺子：
# 原来这两处一个 >500、一个 >1000，700 字节的小图每次补图都会被重新敲一遍微信图床。
MIN_BYTES = 500


def safe_image_path(img_dir, name):
    """把 raw 里记的 `file` 收成 images/ 下的一个落盘路径，收不了给 None。

    只认光文件名：带目录分隔符、`..`、Windows 盘符的一律拒收。这不是防自己——
    `img_filename()` 生成的名字一直是安全的——而是防磁盘上那份 json：它是外部输入，
    一个 `../../../某文件` 就能让补图任务把字节写到书库外面去。最后再用 abspath
    复核一次「确实落在 images/ 里」，两道都过才给路径。
    """
    name = str(name if name is not None else "").strip()
    if not name or name in (".", ".."):
        return None
    # basename 是按平台算的：Windows 上 a\b.jpg 会被切成 b.jpg，于是被判为带目录。
    if name != os.path.basename(name) or ":" in name:
        return None
    full = os.path.abspath(os.path.join(img_dir, name))
    if not full.startswith(os.path.abspath(img_dir) + os.sep):
        return None
    return full


def collect_tasks(raw_dir, img_dir):
    """把 raw/*.json 里的图片清单摊成 [(url, 落盘路径)]，返回 (tasks, bad)。

    bad 是「读不懂的清单 + 用不了的记录」的条数。以前这里是 `json.load(open(...))`
    一杆子到底：一个半截 JSON 就让整个补图任务带着调用栈死掉，用户看到的是「一张都没下」，
    而其实其它几章的图都好好的。坏文件只跳过、只计数，不牵连别人。
    """
    tasks, bad, seen = [], 0, set()
    for jf in sorted(glob.glob(os.path.join(raw_dir, "*.json"))):
        try:
            with open(jf, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            bad += 1
            continue
        if not isinstance(data, dict):
            bad += 1
            continue
        for im in data.get("images") or []:
            if not isinstance(im, dict):
                bad += 1
                continue
            url = str(im.get("url") or "").strip()
            fp = safe_image_path(img_dir, im.get("file"))
            if not url or fp is None:
                bad += 1
                continue
            if (url, fp) in seen:
                # 同一张图在好几章的清单里重复出现：只下一次，免得两个线程抢写同一个文件。
                continue
            seen.add((url, fp))
            tasks.append((url, fp))
    return tasks, bad


def write_atomic(fpath, raw):
    """先写同目录的临时文件再改名。

    直接 `open(fpath,"wb").write(raw)` 在写到一半被杀时，images/ 里会留一张长度
    看着够（超过判据）其实内容缺尾的图，而下次补图会把它当成「已下好」永远跳过。
    改名是原子的：要么没有，要么是完整的那一张。
    """
    tmp = fpath + ".part"
    with open(tmp, "wb") as f:
        f.write(raw)
    os.replace(tmp, fpath)


def download_one(url, fpath, retries=3):
    if os.path.exists(fpath):
        try:
            if os.path.getsize(fpath) >= MIN_BYTES:
                return "skip"
        except OSError:
            pass
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            raw = urllib.request.urlopen(req, timeout=20).read()
            if len(raw) >= MIN_BYTES:
                write_atomic(fpath, raw)
                return "ok"
        except Exception as e:
            if attempt == retries - 1:
                return f"fail:{e}"
            time.sleep(1.5)
    return "fail:empty"


def main(book_id):
    book_dir = os.path.join(OUTPUT_DIR, book_id)
    raw_dir = os.path.join(book_dir, "raw")
    img_dir = os.path.join(book_dir, "images")
    os.makedirs(img_dir, exist_ok=True)

    tasks, bad = collect_tasks(raw_dir, img_dir)
    if bad:
        print(f"  ⚠️  有 {bad} 条图片记录用不了（半截 JSON / 缺网址 / 文件名带目录），已跳过")

    print(f"共 {len(tasks)} 张图片，8 线程并发下载（强制IPv4）...")
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
    if not sys.argv[1:]:
        print("用法: download_images.py <bookId|书库路径>", file=sys.stderr)
        sys.exit(2)
    raw = sys.argv[1].rstrip("/")
    book_id = raw.split("/")[-1] if "weread" in raw else raw
    main(book_id)
