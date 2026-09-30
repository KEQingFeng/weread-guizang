#!/usr/bin/env python3
"""独立图片下载器：强制 IPv4 + 并发 + 重试。读 raw/*.json 里的图片URL下到 images/。"""
import json, glob, os, socket, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# 强制 IPv4（macOS 上 IPv6 路由不通会导致每次连接卡 ~120s）
_orig = socket.getaddrinfo
socket.getaddrinfo = lambda *a, **k: [x for x in _orig(*a, **k) if x[0] == socket.AF_INET]

HEADERS = {"Referer": "https://weread.qq.com/", "User-Agent": "Mozilla/5.0"}


def download_one(url, fpath, retries=3):
    if os.path.exists(fpath) and os.path.getsize(fpath) > 1000:
        return "skip"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            raw = urllib.request.urlopen(req, timeout=20).read()
            if len(raw) > 500:
                open(fpath, "wb").write(raw)
                return "ok"
        except Exception as e:
            if attempt == retries - 1:
                return f"fail:{e}"
            time.sleep(1.5)
    return "fail:empty"


def main(book_id):
    book_dir = os.path.join("output", book_id)
    raw_dir = os.path.join(book_dir, "raw")
    img_dir = os.path.join(book_dir, "images")
    os.makedirs(img_dir, exist_ok=True)

    tasks = []
    for jf in sorted(glob.glob(os.path.join(raw_dir, "*.json"))):
        for im in json.load(open(jf)).get("images", []):
            tasks.append((im["url"], os.path.join(img_dir, im["file"])))

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
    raw = sys.argv[1].rstrip("/")
    book_id = raw.split("/")[-1] if "weread" in raw else raw
    main(book_id)
