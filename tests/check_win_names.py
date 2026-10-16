# -*- coding: utf-8 -*-
"""落盘名字在 Windows 上的那一档：设备名与结尾的点/空格，三平台同一把尺子。

为什么单开一条门禁
    归藏是三个平台同一份代码（有 `启动归藏.bat` 与 `shell/build_windows.sh`），
    而此前所有「文件名洗一遍」的口子都只挡了非法字符与长度，没管 Windows 的
    两条硬规矩：

      · **保留设备名不看扩展名**。`CON` / `NUL` / `COM1`（连上标 `COM¹` 也算）
        是设备，`CON.png`、`NUL.epub` 一样是设备：`open()` 要么失败，要么真写到
        设备上去。用户在写作台把标题起成 `CON`、远端站点给个 `nul.pdf`，导出/下载
        那一下就炸，而报错与「标题」看着毫无关系。
      · **结尾的点与空格会被系统悄悄抹掉**。`稿子.` 与 `稿子` 在 Windows 上落成
        同一个文件，设置里「重名文件自动加序号」的承诺当场失效。

    这一档在 macOS / Linux 上**没有症状**（`CON.png` 建得出来），所以不能靠「真跑
    一次导出看红不红」来守 —— 只能把**名字形状**钉住。这也是它单独一条门禁的理由。

钉住五件事
  A 共用尺子 `platform_compat.win_safe_filename`：设备名（含大小写、扩展名、上标、
    CONIN$/CONOUT$）补下划线，结尾点/空格收掉，正常名字一个字不改，幂等。
  B 对照：裸拼 `CON.png` 确实是设备名（没有这一层时导出就落在设备上），而 POSIX
    上它照样建得出来（所以只能钉形状，不能钉落盘）。
  C 写作台导出命名（`ui_server.file_stem`）与笔记 CSV 名不再产出设备名。
  D 安娜下载落盘名（`anna_state.safe_name`）、EPUB 资源名（`book_import._safe_name`）
    与视频转写导出名（`video_note._safe_file`）同样收口，且老口径（纯点号退回
    「未命名」、正常名不改）没被碰掉。
  E 插图下载（`download_images.safe_target`）把设备名与 `../` 同一档拒掉 ——
    正文按原名引用插图，改名会让图对不上号，所以这里只能跳过、不能补下划线。

纯函数 + 临时目录，不联网、不开浏览器，归 --fast 静态段。
"""
import os
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import selftest  # noqa: E402

selftest.export_sandbox_env()          # import ui_server 之前必须先指到临时沙盒

import platform_compat as pc  # noqa: E402
import anna_state as anna     # noqa: E402
import book_import as bi      # noqa: E402
import download_images as di  # noqa: E402
import ui_server as us        # noqa: E402
import video_note as vn       # noqa: E402

FAIL = []
N = [0]


def chk(name, cond, extra=""):
    N[0] += 1
    print(("  ok  " if cond else " FAIL ") + name + ("" if cond else "  <- " + repr(extra)[:220]))
    if not cond:
        FAIL.append(name)


TMP = tempfile.mkdtemp(prefix="guizang-winname-")

try:
    # ── A 共用尺子 ────────────────────────────────────────────────
    for raw, want in [
        ("CON", "_CON"),
        ("con", "_con"),
        ("NUL", "_NUL"),
        ("aux", "_aux"),
        ("PRN", "_PRN"),
        ("COM1", "_COM1"),
        ("lpt9", "_lpt9"),
        ("CON.epub", "_CON.epub"),
        ("NUL.jpg", "_NUL.jpg"),
        ("COM\u00b9", "_COM\u00b9"),
        ("LPT\u00b3.txt", "_LPT\u00b3.txt"),
        ("CONIN$", "_CONIN$"),
        ("CONOUT$", "_CONOUT$"),
    ]:
        got = pc.win_safe_filename(raw)
        chk("设备名补下划线：%r -> %r" % (raw, want), got == want, got)

    for bad in ["COM0", "COM10", "LPT10", "NULfoo", "console", "COM", "AUX1"]:
        chk("%r 不是设备名，一个字不改" % bad, pc.win_safe_filename(bad) == bad,
            pc.win_safe_filename(bad))

    for good in ["正常的一本书.epub", "The_Book_(2026).pdf", "notes", "a.b.c.md",
                 "第一章 插图_01.jpeg", "_CON", "img_"]:
        chk("正常名字原样留着：%r" % good, pc.win_safe_filename(good) == good,
            pc.win_safe_filename(good))

    chk("结尾的点收掉", pc.win_safe_filename("稿子.") == "稿子", pc.win_safe_filename("稿子."))
    chk("结尾的空格收掉", pc.win_safe_filename("稿子 ") == "稿子", pc.win_safe_filename("稿子 "))
    chk("结尾「空格 + 点」一起收掉", pc.win_safe_filename("稿子 . ") == "稿子",
        pc.win_safe_filename("稿子 . "))
    chk("只剩点号的名字收成空串（由调用方兜底）",
        pc.win_safe_filename("...") == "" and pc.win_safe_filename(" . ") == "",
        [pc.win_safe_filename("..."), pc.win_safe_filename(" . ")])
    chk("None / 空串 / 非字符串都不炸",
        pc.win_safe_filename(None) == "" and pc.win_safe_filename("") == ""
        and pc.win_safe_filename(123) == "123",
        [pc.win_safe_filename(None), pc.win_safe_filename(123)])

    pool = ["CON", "nul.epub", "稿子.", "正常名.pdf", "_CON", "COM\u00b9", " .. ", "a b"]
    chk("幂等：洗过的再洗一遍不变",
        all(pc.win_safe_filename(pc.win_safe_filename(x)) == pc.win_safe_filename(x)
            for x in pool),
        [pc.win_safe_filename(x) for x in pool])

    # ── B 对照（防空转）────────────────────────────────────────────
    chk("对照：裸拼 CON.png 就是设备名（没有这一层时导出正落在设备上）",
        pc.is_win_device_name("CON.png") and pc.is_win_device_name("NUL" + ".epub"),
        [pc.is_win_device_name("CON.png"), pc.is_win_device_name("NUL.epub")])
    try:
        with open(os.path.join(TMP, "CON.png"), "wb") as f:
            f.write(b"x")
        posix_ok = True
    except OSError:
        posix_ok = False
    chk("对照：POSIX 上 CON.png 照样建得出来（所以只能钉名字形状）", posix_ok)

    # ── C 写作台导出命名 ──────────────────────────────────────────
    chk("file_stem('CON') 不再是设备名", not pc.is_win_device_name(us.file_stem("CON")),
        us.file_stem("CON"))
    chk("file_stem('CON') 补成 _CON", us.file_stem("CON") == "_CON", us.file_stem("CON"))
    chk("file_stem('nul') 也补", us.file_stem("nul") == "_nul", us.file_stem("nul"))
    chk("file_stem('NUL.pdf') 保留扩展名并补下划线",
        us.file_stem("NUL.pdf") == "_NUL.pdf", us.file_stem("NUL.pdf"))
    chk("file_stem('COM1') 也在射程内", us.file_stem("COM1") == "_COM1", us.file_stem("COM1"))
    chk("导出的成品名（stem + .png）不是设备名",
        not pc.is_win_device_name(us.file_stem("CON") + ".png"))
    chk("file_stem 正常标题一个字不改",
        us.file_stem("我的稿子") == "我的稿子" and us.file_stem("_CON") == "_CON",
        [us.file_stem("我的稿子"), us.file_stem("_CON")])
    chk("老口径还在：空 / 纯点号标题退回「稿子」",
        us.file_stem("   ") == "稿子" and us.file_stem("...") == "稿子",
        [us.file_stem("   "), us.file_stem("...")])
    chk("笔记 CSV 名跟着收口",
        us.notes_csv_name("CON") == "flomo\u5bfc\u5165-_CON.csv", us.notes_csv_name("CON"))

    # ── D 安娜下载名 / EPUB 资源名 ────────────────────────────────
    chk("anna.safe_name('CON.epub') 补成 _CON.epub",
        anna.safe_name("CON.epub") == "_CON.epub", anna.safe_name("CON.epub"))
    chk("anna.safe_name('nul') 也补", anna.safe_name("nul") == "_nul", anna.safe_name("nul"))
    chk("anna.safe_name 正常名一个字不改",
        anna.safe_name("正常的一本书.epub") == "正常的一本书.epub",
        anna.safe_name("正常的一本书.epub"))
    chk("anna 老口径还在：纯点号退回「未命名」",
        anna.safe_name("..") == "未命名" and anna.safe_name(".") == "未命名",
        [anna.safe_name(".."), anna.safe_name(".")])
    chk("anna 名字洗过再洗一遍不变",
        anna.safe_name(anna.safe_name("CON.epub")) == anna.safe_name("CON.epub"),
        anna.safe_name(anna.safe_name("CON.epub")))

    chk("book_import._safe_name('CON.png') 补成 _CON.png",
        bi._safe_name("CON.png") == "_CON.png", bi._safe_name("CON.png"))
    chk("book_import 正常资源名一个字不改",
        bi._safe_name("images/cover.jpg") == "cover.jpg"
        and bi._safe_name("cover.jpg") == "cover.jpg",
        [bi._safe_name("images/cover.jpg"), bi._safe_name("cover.jpg")])
    chk("book_import 空 / 点号名仍退回 img_*",
        bi._safe_name("") == "img_" and bi._safe_name("..") == "img_",
        [bi._safe_name(""), bi._safe_name("..")])

    chk("video_note._safe_file('CON') 补成 _CON",
        vn._safe_file("CON") == "_CON", vn._safe_file("CON"))
    chk("video_note 导出的成品名（stem + .txt）不是设备名",
        not pc.is_win_device_name(vn._safe_file("NUL") + ".txt"))
    chk("video_note 正常标题一个字不改、空标题仍退回 transcript",
        vn._safe_file("这期视频叫什么") == "这期视频叫什么"
        and vn._safe_file("   ") == "transcript",
        [vn._safe_file("这期视频叫什么"), vn._safe_file("   ")])

    # ── E 插图下载：设备名与 ../ 同一档拒掉 ───────────────────────
    for bad in ["CON.jpg", "nul", "COM1.png", "LPT9", "aux.gif", "COM\u00b9.webp"]:
        chk("插图落点 %r 拒掉" % bad, di.safe_target(TMP, bad) is None,
            di.safe_target(TMP, bad))
    for good in ["console.jpg", "NULfoo.jpg", "COM10.png", "ch0001_img01.jpg",
                 "第一章 插图_01.jpeg"]:
        chk("插图落点 %r 照放行（不误伤）" % good, di.safe_target(TMP, good) is not None,
            di.safe_target(TMP, good))
    chk("老口径没被碰掉：../ 仍拒", di.safe_target(TMP, "../evil.jpg") is None)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("\nWindows 落盘名（设备名 / 结尾点空格）：通过 %d 项，失败 %d 项" % (N[0], len(FAIL)))
for f in FAIL:
    print("  \u00d7 " + f)
sys.exit(1 if FAIL else 0)
