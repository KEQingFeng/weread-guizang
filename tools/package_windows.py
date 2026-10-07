"""打 Windows 分发包：一个 zip，解压后双击「安装归藏.bat」就能装。

为什么是 zip + 安装脚本，不是 .exe —— 因为打包机是 macOS。真 .exe 安装包
（PyInstaller / NSIS / Inno Setup）必须在 Windows 上编译，这台机器既没有 wine
也没有 makensis。所以给一份「解压即可用」的包：脚本负责把程序复制到
%USERPROFILE%\\归藏、在桌面放一个「归藏」快捷方式；不想装的话，双击
「启动归藏.bat」就地跑。

清单不另写一份 —— 读的是 shell/build_macos.sh 里的 APP_FILES / APP_DIRS，
和 .app、源码 zip 共用 tools/package_source_zip.py 的 manifest()。
"""
import pathlib, re, sys, zipfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from package_source_zip import (ROOT, SKIP_PARTS, SKIP_SUFFIX,  # noqa: E402
                               manifest, scan)

# Windows 侧入口文件都在 shell/windows/ 下，进包时要摊到 zip 根目录：
# 用户解压后第一眼得看见「安装归藏.bat」，而不是先点进 shell/windows/ 找。
ENTRY = {
    "shell/windows/安装归藏.bat": "安装归藏.bat",
    "shell/windows/首次打开必读.txt": "首次打开必读.txt",
    "shell/windows/归藏.ico": "归藏.ico",
}
# 根目录这两份照原位进包（启动脚本必须和 ui_server.py 同层，README 供查阅）。
ROOT_EXTRA = ["启动归藏.bat", "README.md"]

# 包体里绝不该出现的东西。cache/output 是数据和登录态，.venv 是本地环境，
# runtime.json 是运行痕迹。
FORBIDDEN = ("cache/", "output/", "browser_profile", "config.json", "notes_index",
             "runtime.json", ".venv/", "__pycache__/", ".git/", "browser_profile/")


def version():
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"',
                  (ROOT / "ui_server.py").read_text(encoding="utf-8"), re.M)
    if not m:
        sys.exit('读不到版本号：ui_server.py 里那行得是 VERSION = "x.y.z"')
    return m.group(1)


def check_bat():
    """两个 .bat 是要在 cmd 里跑的，必须是 CRLF 且不带 BOM。

    裸 LF 的经典症状是 cmd 把两行粘成一行执行；带 BOM 则第一行开头多出几个字节，
    cmd 直接报「不是内部或外部命令」。这两样本来就得是文件属性，打包时顺手拦一道 ——
    它们是打在包里的，验不了（本机没有 Windows），只能在这一步守住。
    """
    for t in ("启动归藏.bat", "shell/windows/安装归藏.bat"):
        raw = (ROOT / t).read_bytes()
        if raw[:3] == b"\xef\xbb\xbf":
            sys.exit(f"{t} 带 UTF-8 BOM，cmd 会把第一行当乱码 —— 存成无 BOM 的 UTF-8")
        if raw.count(b"\r\n") != raw.count(b"\n"):
            sys.exit(f"{t} 里还有裸 LF —— Windows 的 cmd 要 CRLF，转好再打包")


def build(out_dir=None):
    """出包，返回 (压缩包路径, 文件数, 顶层目录名)。out_dir 是目录，不给就落 安装包/。"""
    files, dirs = manifest()
    ver = version()
    top = f"归藏-Windows-{ver}"

    if out_dir is None:
        out_dir = ROOT / "安装包"
    out = pathlib.Path(out_dir) / f"归藏-Windows-{ver}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)

    check_bat()
    for src in ENTRY:
        if not (ROOT / src).exists():
            sys.exit(f"缺 {src} —— Windows 的入口文件得先写出来")

    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in list(files) + ROOT_EXTRA:
            f = ROOT / name
            if not f.exists():
                sys.exit(f"清单点名了 {name}，仓库里却没有 —— 先查是不是被误删了")
            z.write(f, f"{top}/{name}"); n += 1
        for d in dirs:                       # mcp / skills / vendor
            for f in sorted((ROOT / d).rglob("*")):
                if f.is_dir() or f.suffix in SKIP_SUFFIX or any(p in f.parts for p in SKIP_PARTS):
                    continue
                z.write(f, f"{top}/{f.relative_to(ROOT)}"); n += 1
        for src, dst in ENTRY.items():       # 入口文件摊到根
            z.write(ROOT / src, f"{top}/{dst}"); n += 1
    return out, n, top


def report(out, top):
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        bad = [n for n in names if any(p in n for p in FORBIDDEN)]
        print("夹带不该带的东西:", bad or "无")
        # 该在的东西逐个点名 —— 少一个这包就是残的
        for want in ("安装归藏.bat", "启动归藏.bat", "首次打开必读.txt", "归藏.ico",
                     "ui_server.py", "requirements.txt"):
            print(f"  {'有' if f'{top}/{want}' in names else '缺'}  {want}")
        for line in scan(z):
            print("  待核实", line)


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    out, n, top = build(arg)
    print(f"{out} · {n} 个文件 · {out.stat().st_size//1024} KB")
    report(out, top)


if __name__ == "__main__":
    main()
