// 归藏 · 原生外壳（macOS）
//
// 这层只干四件事，别的一概不碰：
//
//   1. 开一个窗口，装一个 WKWebView
//   2. 环境不全 → 载首启页；用户点「我思故我在」→ 跑 bootstrap.py，把 stdout 流回页面
//   3. 环境齐了 → 起 ui_server.py，等它就绪，把同一个 WebView 导航过去
//   4. 退出时收掉自己起的进程
//
// 界面本身还是 ui.html 那一套，一行没改。壳不重画界面，也不解释书 —— 它只负责
// 让「双击之后能用」这件事成立。
//
// 编译见 shell/build_macos.sh。用的都是 CommandLineTools 自带的 Swift + SDK，
// 不需要完整 Xcode。

import AppKit
import Darwin
import UniformTypeIdentifiers
import WebKit

let APP_TITLE = "归藏"
let PREFERRED_PORT = 8770

// MARK: - 排障日志
//
// 外壳出问题（白屏、自己退出、窗口没出来）时，用户那边看不到任何输出 ——
// 双击启动的东西没有终端。所以留一条开关：设了 GUIZANG_DEBUG 就把关键节点
// 记到数据目录下的 shell.log，出问题时有据可查。
//
//   GUIZANG_DEBUG=1 open -a 归藏
//
// 平时完全不写盘。

let DEBUG_ON = ProcessInfo.processInfo.environment["GUIZANG_DEBUG"] != nil

func dbg(_ text: String) {
    guard DEBUG_ON else { return }
    let line = "[\(Date())] \(text)\n"
    FileHandle.standardError.write(Data(line.utf8))
    let support = FileManager.default.urls(for: .applicationSupportDirectory,
                                           in: .userDomainMask)[0]
    let dir = support.appendingPathComponent(APP_TITLE, isDirectory: true)
    try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let log = dir.appendingPathComponent("shell.log")
    if let h = try? FileHandle(forWritingTo: log) {
        h.seekToEndOfFile()
        h.write(Data(line.utf8))
        try? h.close()
    } else {
        try? Data(line.utf8).write(to: log)
    }
}

// MARK: - 小工具

/// 把字符串安全地塞进 JS 源码里（引号、换行、中文都交给 JSON 转义）。
func jsString(_ s: String) -> String {
    guard let d = try? JSONSerialization.data(withJSONObject: [s]),
          let t = String(data: d, encoding: .utf8) else { return "\"\"" }
    return String(t.dropFirst().dropLast())
}

/// 逐行读一个管道的输出。
///
/// 必须边读边放：Playwright 下载 Chromium 要几分钟，中间会吐很多行；如果只等结束
/// 再一次性读，管道缓冲区满了子进程就会停在那儿等我们 —— 表现是「进度条卡死」，
/// 而且永远等不到结束。
final class LineStreamer {
    private let queue = DispatchQueue(label: "guizang.stream")
    private var buf = Data()
    private let onLine: (String) -> Void

    init(_ handle: FileHandle, onLine: @escaping (String) -> Void) {
        self.onLine = onLine
        handle.readabilityHandler = { [weak self] h in
            let d = h.availableData
            guard let self else { return }
            if d.isEmpty {                       // 管道关了
                h.readabilityHandler = nil
                return
            }
            self.queue.async { self.feed(d) }
        }
    }

    private func feed(_ d: Data) {
        buf.append(d)
        while let i = buf.firstIndex(of: 0x0A) {
            let line = buf.subdata(in: 0..<i)
            buf.removeSubrange(0...i)
            onLine(String(decoding: line, as: UTF8.self))
        }
    }
}

// MARK: - 外壳

final class Shell: NSObject, NSApplicationDelegate, WKScriptMessageHandler,
                   WKUIDelegate, WKNavigationDelegate {

    let window: NSWindow
    let web: WKWebView

    private var server: Process?
    private var serverURL: URL?
    private var keeper: Any?                       // 保住 streamer，别让它被回收
    private var busy = false

    // 应用包里的三处路径
    private let resDir: URL
    private let appDir: URL                        // Python 源码 + ui.html
    private let onboarding: URL
    private let dataDir: URL                       // 可写：导出的书、缓存、虚拟环境

    override init() {
        let res = Bundle.main.resourceURL ?? Bundle.main.bundleURL
        resDir = res
        appDir = res.appendingPathComponent("app")
        onboarding = res.appendingPathComponent("onboarding.html")

        let support = FileManager.default.urls(for: .applicationSupportDirectory,
                                               in: .userDomainMask)[0]
        dataDir = support.appendingPathComponent(APP_TITLE, isDirectory: true)

        let cfg = WKWebViewConfiguration()
        cfg.preferences.setValue(true, forKey: "developerExtrasEnabled")
        web = WKWebView(frame: .zero, configuration: cfg)

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        super.init()

        window.title = APP_TITLE
        window.minSize = NSSize(width: 460, height: 600)
        window.center()
        window.setFrameAutosaveName("guizang.window")   // 记住位置与大小
        window.contentView = web
        web.autoresizingMask = [.width, .height]
        web.frame = window.contentView!.bounds
        web.uiDelegate = self
        // navigationDelegate 和 uiDelegate 是两回事，都得设：
        // uiDelegate 管「弹文件选择框」这类界面行为，navigationDelegate 才管
        // 「页面加载完了 / 加载失败了」。漏了它，didFinish 永远不会回调，
        // 加载失败也没人知道 —— 表现是白屏但没有任何线索。
        web.navigationDelegate = self
        web.configuration.userContentController.add(self, name: "guizang")
    }

    // MARK: 生命周期

    /// 收掉上一轮没带走的残留。
    ///
    /// 崩溃、强杀（pkill、活动监视器里强制退出）都不会走 applicationWillTerminate，
    /// 于是后端子进程和扫码窗口的浏览器就留在那儿，继续占着端口。攒上几次，
    /// 下次打开会莫名其妙换到 8771、8772 —— 用户看到的是「怎么冒出好几个归藏」。
    ///
    /// 所以启动时按本项目自己的路径精确清扫一遍。路径只可能是应用包里的
    /// ui_server.py 和数据目录下的 browser_profile，碰不到别人的进程。
    private func sweepStale() {
        let patterns = [
            appDir.appendingPathComponent("ui_server.py").path,
            dataDir.appendingPathComponent("cache/browser_profile").path,
        ]
        for pat in patterns {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
            p.arguments = ["-f", pat]
            try? p.run()
            p.waitUntilExit()
            if p.terminationStatus == 0 { dbg("清扫了残留进程：\(pat)") }
        }
    }

    func applicationDidFinishLaunching(_ n: Notification) {
        dbg("didFinishLaunching 进入")
        buildMenu()
        try? FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)
        sweepStale()

        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        dbg("窗口已 order front visible=\(window.isVisible) frame=\(window.frame)")

        // 谁把窗口关了就会触发一次「最后一个窗口关闭 → 退出」，顺手记下关闭时机
        NotificationCenter.default.addObserver(
            forName: NSWindow.willCloseNotification, object: window, queue: .main) { _ in
                dbg("窗口 willClose")
            }

        // 已经就绪就直接进去；缺东西才让首启页出场。
        // 这一步决定「第二次打开是一秒进界面，而不是又问一遍要不要配置」。
        let ready = isReady()
        dbg("isReady=\(ready)  解释器=\(venvPython() ?? "无")  浏览器=\(browserReady())  账号=\(accountReady())")
        if !ready {
            // 「配置失败」的远程排障全靠这一行：到底是机器上没 Python，
            // 还是选中的那个用不了。不记下来就只能靠猜。
            dbg("若要配置，会用系统解释器：\(systemPython() ?? "没找到能用的")")
        }
        if ready {
            startServer()
        } else {
            loadOnboarding()
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool {
        dbg("applicationShouldTerminateAfterLastWindowClosed 被问，答 true")
        return true
    }

    func applicationWillTerminate(_ n: Notification) {
        dbg("willTerminate 进入")
        server?.terminate()
        // 顺手收回可能还占着 profile 的浏览器：它握着 profile 锁，
        // 不收拾的话下一轮会起不来。按本项目自己的 profile 路径精确匹配，
        // 不会碰到用户自己开着的浏览器。
        let profile = dataDir.appendingPathComponent("cache/browser_profile").path
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        p.arguments = ["-f", profile]
        try? p.run()
    }

    // MARK: 路径与体检

    /// 虚拟环境里的解释器（数据目录下）。
    ///
    /// 建好了但用不了的情况是真实存在的：虚拟环境里那个 python 只是指向
    /// 「当初用来建它的那个解释器」的软链，用户后来把那个 Python 卸了、
    /// 或者升了级，链接就成了死的。光判断文件在不在会把它当成「环境齐了」，
    /// 于是直接跳进界面，然后后端起不来，白屏 —— 所以这里也真跑一下。
    private func venvPython() -> String? {
        for sub in ["bin/python", "Scripts/python.exe"] {
            let p = dataDir.appendingPathComponent(".venv/\(sub)").path
            if pythonWorks(p) { return p }
        }
        return nil
    }

    /// Playwright 的浏览器缓存目录。认官方环境变量，各平台默认位置不同。
    private func playwrightDir() -> URL {
        if let env = ProcessInfo.processInfo.environment["PLAYWRIGHT_BROWSERS_PATH"],
           !env.isEmpty, env != "0" {
            return URL(fileURLWithPath: (env as NSString).expandingTildeInPath)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Caches/ms-playwright")
    }

    private func browserReady() -> Bool {
        let fm = FileManager.default
        guard let items = try? fm.contentsOfDirectory(atPath: playwrightDir().path) else { return false }
        return items.contains { $0.hasPrefix("chromium-") || $0.hasPrefix("chromium_headless_shell-") }
    }

    private func accountReady() -> Bool {
        let p = dataDir.appendingPathComponent("cache/login_state.json")
        guard let d = try? Data(contentsOf: p),
              let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return false }
        return (o["logged_in"] as? Bool) ?? false
    }

    /// 三样齐了才算就绪：解释器、浏览器、账号。
    /// 依赖装没装这里不查 —— 那是 bootstrap.py 的事，它一跑就知道，查两次没意义。
    private func isReady() -> Bool { venvPython() != nil && browserReady() && accountReady() }

    // MARK: 页面

    private func loadOnboarding() {
        web.loadFileURL(onboarding, allowingReadAccessTo: resDir)
    }

    private func pushState() {
        js("window.gz.init({env:\(venvPython() != nil),browser:\(browserReady()),account:\(accountReady())})")
    }

    private func js(_ code: String) {
        DispatchQueue.main.async { [weak self] in
            self?.web.evaluateJavaScript(code, completionHandler: nil)
        }
    }

    private func pushLog(_ line: String) {
        let t = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        js("window.gz.log(\(jsString(t)))")
    }

    // MARK: 配置流程

    /// 跑一个脚本，逐行把输出推给页面；结束时回调退出码。
    private func run(_ exe: String, _ args: [String], cwd: URL, onExit: @escaping (Int32) -> Void) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = args
        p.currentDirectoryURL = cwd
        var env = ProcessInfo.processInfo.environment
        // 数据目录交给 Python 那边：源码在应用包里（不该写），
        // 导出的书与缓存改落用户目录。platform_compat.data_dir 认这个变量。
        env["GUIZANG_DATA"] = dataDir.path
        env["PYTHONUNBUFFERED"] = "1"
        // 源码躺在应用包里，是只读的。Python 默认会在源码旁边写 __pycache__，
        // 那等于每次运行都在往自己身体里刻字 —— 签名会被搞花，装到只读卷上还会报错。
        // 关掉字节码落盘，包体保持干净。
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        p.environment = env

        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe

        let streamer = LineStreamer(pipe.fileHandleForReading) { [weak self] line in
            self?.pushLog(line)
        }
        keeper = streamer                     // 不保住的话它会被回收，输出就断了

        p.terminationHandler = { [weak self] proc in
            let code = proc.terminationStatus
            DispatchQueue.main.async { self?.keeper = nil; onExit(code) }
        }
        do {
            try p.run()
        } catch {
            pushLog("起不来：\(error.localizedDescription)")
            DispatchQueue.main.async { onExit(-1) }
        }
    }

    /// 先找系统解释器来跑 bootstrap —— 这一步正是要把虚拟环境建出来，
    /// 所以此时还不能指望 .venv 里那个。
    ///
    /// 候选表按「越可能是干净可用的越靠前」排：python.org 官方包 → Homebrew →
    /// pyenv / conda → /usr/local → 最后才是系统自带的那个。
    private func systemPython() -> String? {
        var candidates: [String] = []
        for v in ["3.14", "3.13", "3.12", "3.11", "3.10", "3.9"] {
            candidates.append("/Library/Frameworks/Python.framework/Versions/\(v)/bin/python3")
        }
        candidates += [
            "/opt/homebrew/bin/python3",
            "/opt/homebrew/bin/python3.13",
            "/opt/homebrew/bin/python3.12",
            "/opt/homebrew/bin/python3.11",
            "/usr/local/bin/python3",
        ]
        let home = NSHomeDirectory()
        candidates += [
            "\(home)/.pyenv/shims/python3",
            "/opt/anaconda3/bin/python3",
            "/opt/miniconda3/bin/python3",
            "\(home)/miniconda3/bin/python3",
            "\(home)/anaconda3/bin/python3",
            "/usr/bin/python3",
        ]
        for c in candidates where pythonWorks(c) { return c }
        return nil
    }

    /// 候选解释器是不是真能用 —— 要真跑一句，不能只看文件在不在、可不可执行。
    ///
    /// /usr/bin/python3 是 Apple 的转发壳，会去问「当前开发者目录」要解释器。
    /// 本机正好撞上过：Xcode 的许可没同意，它每次都打印
    /// 「You have not agreed to the Xcode license agreements」然后退出 69。
    /// 光看可执行位它会稳稳通过，壳就选了它 —— 配置在第一步失败，
    /// 用户那边只看到一句「环境没配成功」，完全不知道是许可的事。
    ///
    /// 挂住也算不能用：没有 CLT 的机器上，这个壳会弹系统对话框等用户点，
    /// 不设上限就会把启动吊死。
    private func pythonWorks(_ path: String) -> Bool {
        guard FileManager.default.isExecutableFile(atPath: path) else { return false }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: path)
        p.arguments = ["-c", "import sys;sys.stdout.write(str(sys.version_info[0]))"]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = Pipe()
        do { try p.run() } catch { return false }

        let deadline = Date().addingTimeInterval(6)
        while p.isRunning && Date() < deadline { usleep(50_000) }
        if p.isRunning {
            p.terminate()
            usleep(200_000)
            if p.isRunning { kill(p.processIdentifier, SIGKILL) }
            return false
        }
        let shown = String(data: out.fileHandleForReading.readDataToEndOfFile(),
                           encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
        return p.terminationStatus == 0 && shown == "3"
    }

    private func bootstrap() {
        guard !busy else { return }
        busy = true

        guard let py = venvPython() ?? systemPython() else {
            busy = false
            js("window.gz.fail(\(jsString("这台机器上没找到能用的 Python。去 python.org/downloads 装一个官方版本（3.9 以上），装完不用重启，回来再点一次就行。")) )")
            return
        }

        js("window.gz.showLog()")
        pushLog("—— 用 \(py) 开始配置 ——")

        run(py, [appDir.appendingPathComponent("bootstrap.py").path], cwd: appDir) { [weak self] code in
            guard let self else { return }
            if code != 0 {
                self.busy = false
                self.js("window.gz.fail(\(jsString("环境没配成功，上面的记录里写着卡在哪一步。挂上代理再点一次通常就好。")) )")
                return
            }
            self.js("window.gz.step(0,'ok','依赖已装齐')")
            if self.browserReady() {
                self.js("window.gz.step(1,'ok','Chromium 已就绪')")
            } else {
                self.js("window.gz.step(1,'fail','Chromium 没装上')")
                self.busy = false
                self.js("window.gz.fail(\(jsString("Chromium 没装上，挂上代理重试一次。")) )")
                return
            }
            self.login()
        }
    }

    /// 扫码登录。这一步必开一个真窗口 —— 二维码得让用户拿手机扫，
    /// 所以 login.py 是 headless=False，壳这边不拦它。
    private func login() {
        guard let py = venvPython() else {
            busy = false
            js("window.gz.fail(\(jsString("虚拟环境没建起来，没法登录。")) )")
            return
        }
        js("window.gz.step(2,'run','等你在弹出的窗口里扫码')")
        pushLog("—— 即将弹出一个窗口，请用微信扫码 ——")

        run(py, [appDir.appendingPathComponent("login.py").path], cwd: dataDir) { [weak self] _ in
            guard let self else { return }
            self.busy = false
            if self.accountReady() {
                self.js("window.gz.step(2,'ok','账号已连接')")
                self.js("window.gz.done()")
                // 等页面那下淡出放完，再切进界面，别让过渡白做
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.95) { self.enter() }
            } else {
                self.js("window.gz.step(2,'idle','还没连上')")
                self.js("window.gz.fail(\(jsString("账号没连上。点「再试一次」，会重新弹出扫码窗口。")) )")
            }
        }
    }

    /// 起后端，就绪后把 WebView 导航过去。
    private func startServer() {
        if let url = serverURL, server?.isRunning == true {
            web.load(URLRequest(url: url))
            return
        }
        guard let py = venvPython() ?? systemPython() else {
            js("window.gz.fail(\(jsString("找不到能用的 Python，后端起不来。去 python.org/downloads 装一个官方版本再来。")) )")
            return
        }

        let p = Process()
        p.executableURL = URL(fileURLWithPath: py)
        p.arguments = [appDir.appendingPathComponent("ui_server.py").path,
                       "--port", String(PREFERRED_PORT)]
        p.currentDirectoryURL = dataDir
        var env = ProcessInfo.processInfo.environment
        env["GUIZANG_DATA"] = dataDir.path
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"   // 别往只读的应用包里写 __pycache__
        // 让引擎里那些子进程用 .venv 的解释器，不走系统那套
        if let v = venvPython() { env["GUIZANG_PYTHON"] = v }
        p.environment = env

        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe

        // 后端自己会挑端口（8770 被占就往后找），所以认它打出来的那行，
        // 不假定端口号 —— 猜错的话窗口会一直白屏。
        let streamer = LineStreamer(pipe.fileHandleForReading) { [weak self] line in
            guard let self else { return }
            guard let url = self.urlIn(line) else { return }
            dbg("识别到后端地址 \(url.absoluteString)，导航过去")
            self.serverURL = url
            DispatchQueue.main.async { self.web.load(URLRequest(url: url)) }
        }
        keeper = streamer

        p.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                self?.keeper = nil
                self?.server = nil
            }
        }

        do {
            try p.run()
            server = p
            dbg("后端已起 pid=\(p.processIdentifier)")
        } catch {
            dbg("后端起不来 \(error.localizedDescription)")
            js("window.gz.fail(\(jsString("后端起不来：\(error.localizedDescription)")) )")
        }
    }

    /// 从一行输出里认出 `http://127.0.0.1:端口`。
    private func urlIn(_ line: String) -> URL? {
        guard let r = line.range(of: "http://127.0.0.1:") else { return nil }
        let tail = line[r.lowerBound...]
        var digits = ""
        for ch in tail.dropFirst("http://127.0.0.1:".count) {
            if ch.isNumber { digits.append(ch) } else { break }
        }
        guard !digits.isEmpty else { return nil }
        return URL(string: "http://127.0.0.1:\(digits)/")
    }

    private func enter() {
        if serverURL != nil { startServer(); return }
        // 首启页还开着：先让它知道该进去了，再起后端
        js("window.gz.done()")
        startServer()
    }

    // MARK: 菜单
    //
    // 不建菜单栏的话，界面里的输入框连复制粘贴都用不了（选中了按 Cmd+C 没反应），
    // 这是新建 AppKit 程序最容易漏掉的一处。

    private func buildMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于\(APP_TITLE)",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
                        keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏\(APP_TITLE)",
                        action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出\(APP_TITLE)",
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let editItem = NSMenuItem()
        main.addItem(editItem)
        let edit = NSMenu(title: "编辑")
        // 用 NSSelectorFromString 而不是 Selector(("…"))：这些动作沿响应链往下找
        // 目标（谁在前台谁处理），没有可以写成 #selector 的具体方法。
        edit.addItem(withTitle: "撤销", action: NSSelectorFromString("undo:"), keyEquivalent: "z")
        edit.addItem(withTitle: "重做", action: NSSelectorFromString("redo:"), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "剪切", action: NSSelectorFromString("cut:"), keyEquivalent: "x")
        edit.addItem(withTitle: "拷贝", action: NSSelectorFromString("copy:"), keyEquivalent: "c")
        edit.addItem(withTitle: "粘贴", action: NSSelectorFromString("paste:"), keyEquivalent: "v")
        edit.addItem(withTitle: "全选", action: NSSelectorFromString("selectAll:"), keyEquivalent: "a")
        editItem.submenu = edit

        let winItem = NSMenuItem()
        main.addItem(winItem)
        let win = NSMenu(title: "窗口")
        win.addItem(withTitle: "最小化", action: NSSelectorFromString("performMiniaturize:"), keyEquivalent: "m")
        win.addItem(withTitle: "缩放", action: NSSelectorFromString("performZoom:"), keyEquivalent: "")
        winItem.submenu = win
        NSApp.windowsMenu = win

        NSApp.mainMenu = main
    }

    // MARK: 页面 → 壳

    func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any],
              let cmd = body["cmd"] as? String else {
            dbg("收到无法识别的消息 \(message.body)")
            return
        }
        dbg("页面 → 壳：\(cmd)")
        switch cmd {
        case "probe": pushState()
        case "start": bootstrap()
        case "enter": enter()
        default: dbg("未知指令 \(cmd)")
        }
    }

    // MARK: 壳 → 页面（导航完成时补一次状态，页面自己也有一手兜底）

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        dbg("页面加载完成 url=\(webView.url?.absoluteString ?? "nil")")
        // 只有首启页需要喂状态；进了主界面就别再往里打命令了
        if webView.url?.isFileURL == true { pushState() }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        dbg("页面加载失败 \(error.localizedDescription)")
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        dbg("页面预加载失败 url=\(webView.url?.absoluteString ?? "nil") \(error.localizedDescription)")
    }

    // MARK: 文件选择
    //
    // WKWebView 自己不弹选择框，得由宿主来实现 —— 界面上「选图片」那个按钮
    // 走的就是这里。不实现的话点了毫无动静。

    func webView(_ webView: WKWebView,
                 runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.allowedContentTypes = [.image]
        panel.prompt = "选它"
        panel.message = "挑一张当背景的图片"

        if panel.runModal() == .OK {
            completionHandler(panel.urls)
        } else {
            completionHandler(nil)
        }
    }
}

// MARK: - 启动

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let shell = Shell()
app.delegate = shell
app.run()
