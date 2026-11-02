// trkar-screen-ndi: Blender のウィンドウ (またはその中の 1 つのエリア) を ScreenCaptureKit で取り、NDI で送る。
//
//     trkar-screen-ndi --pid <Blender の PID> [--name "Blender Screen (trkAR)"] [--fps 30] [--half]
//                      [--rect fx,fy,fw,fh] [--aspect <中身の幅/高さ>] [--no-cursor] [--parent <PID>]
//
// Blender の中から画面を読む (Window.screenshot) と、GPU がそのとき描いているコマを描き終わるのを
// メインスレッドで待つので、重いシーンではシーンの fps が落ちる。ここでは OS が合成したウィンドウの絵を
// GPU のまま (IOSurface) 受け取るので、Blender は待たされない。OBS の画面キャプチャと同じしくみで、
// 間に OBS の描画や NDI プラグインのバッファを挟まない分だけ遅れが小さい。
//
// --rect は送る範囲を中身 (タイトルバーを除いた Blender の画面) に対する割合で、左下が原点 (Blender の
// area.x / area.y と同じ向き)。--aspect は中身の幅 / 高さで、ウィンドウの枠からタイトルバーの高さを出すのに使う。
// 標準入力に "rect fx fy fw fh aspect" (rect が無ければ "full aspect") を送ると、止めずに範囲を変える。
// 1 秒ごとに標準出力へ "STATUS fps=… size=…x… conns=…" を、失敗したら "ERROR …" を出す。
// Mac の「画面収録」の許可が要る (Blender から起動したときは Blender に許可を出す)。

import AppKit
import CoreGraphics
import CoreMedia
import CoreVideo
import Foundation
import ScreenCaptureKit

setvbuf(stdout, nil, _IOLBF, 0)
// ウィンドウサーバーにつなぐ (つながないと CoreGraphics の画面収録の確認で止まる)。Dock には出さない
NSApplication.shared.setActivationPolicy(.prohibited)

func fail(_ message: String, code: Int32 = 2) -> Never {
    print("ERROR \(message)")
    exit(code)
}

// MARK: - 引数

struct Options {
    var pid: pid_t = 0
    var name = "Blender Screen (trkAR)"
    var fps = 30
    var half = false
    var rect: CGRect?   // 中身に対する割合、左下が原点
    var aspect: Double = 0
    var cursor = true
    var parent: pid_t = 0
}

func parseOptions() -> Options {
    var options = Options()
    var args = CommandLine.arguments.dropFirst().makeIterator()
    while let arg = args.next() {
        switch arg {
        case "--pid": options.pid = pid_t(args.next() ?? "") ?? 0
        case "--name": options.name = args.next() ?? options.name
        case "--fps": options.fps = max(1, min(60, Int(args.next() ?? "") ?? 30))
        case "--half": options.half = true
        case "--rect": options.rect = parseRect(args.next() ?? "")
        case "--aspect": options.aspect = Double(args.next() ?? "") ?? 0
        case "--no-cursor": options.cursor = false
        case "--parent": options.parent = pid_t(args.next() ?? "") ?? 0
        default: fail("unknown argument \(arg)")
        }
    }
    if options.pid == 0 { fail("--pid is required") }
    return options
}

func parseRect(_ text: String) -> CGRect? {
    let v = text.split(separator: ",").compactMap { Double($0) }
    return v.count == 4 ? CGRect(x: v[0], y: v[1], width: v[2], height: v[3]) : nil
}

// MARK: - NDI (libndi を実行時に読み込む)

final class NDISender {
    private let lib: UnsafePointer<NDIlib_v5>
    private let instance: NDIlib_send_instance_t
    private let fps: Int

    init(name: String, fps: Int) {
        let env = ProcessInfo.processInfo.environment
        var paths = [env["NDI_RUNTIME_DIR_V6"], env["NDI_RUNTIME_DIR_V5"]].compactMap { $0 }.map { $0 + "/libndi.dylib" }
        paths += ["/Library/NDI SDK for Apple/lib/macOS/libndi.dylib", "/usr/local/lib/libndi.dylib",
                  "/opt/homebrew/lib/libndi.dylib", "/Library/Application Support/NDI/lib/libndi.dylib"]
        guard let handle = paths.lazy.compactMap({ dlopen($0, RTLD_NOW) }).first else {
            fail("NDI runtime (libndi.dylib) not found. Install NDI SDK for Apple or NDI Tools")
        }
        typealias Load = @convention(c) () -> UnsafePointer<NDIlib_v5>?
        guard let symbol = dlsym(handle, "NDIlib_v5_load"),
              let lib = unsafeBitCast(symbol, to: Load.self)() else { fail("NDIlib_v5_load not found") }
        guard lib.pointee.initialize() else { fail("NDIlib_initialize failed") }
        var settings = NDIlib_send_create_t()
        let created: NDIlib_send_instance_t? = name.withCString { cName in
            settings.p_ndi_name = cName
            return lib.pointee.send_create(&settings)
        }
        guard let created else { fail("NDIlib_send_create failed") }
        self.lib = lib
        self.instance = created
        self.fps = fps
    }

    /// BGRA の画素 (受け取った IOSurface をロックしてそのまま) を送る。NDI は送る前に圧縮するので同期で返る
    func send(_ pixels: CVPixelBuffer) {
        CVPixelBufferLockBaseAddress(pixels, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pixels, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(pixels) else { return }
        var frame = NDIlib_video_frame_v2_t()
        frame.xres = Int32(CVPixelBufferGetWidth(pixels))
        frame.yres = Int32(CVPixelBufferGetHeight(pixels))
        frame.FourCC = NDIlib_FourCC_video_type_BGRX
        frame.frame_rate_N = Int32(fps * 1000)
        frame.frame_rate_D = 1000
        frame.frame_format_type = NDIlib_frame_format_type_progressive
        frame.timecode = NDIlib_send_timecode_synthesize
        frame.p_data = base.assumingMemoryBound(to: UInt8.self)
        frame.line_stride_in_bytes = Int32(CVPixelBufferGetBytesPerRow(pixels))
        lib.pointee.send_send_video_v2(instance, &frame)
    }

    var connections: Int32 { lib.pointee.send_get_no_connections(instance, 0) }

    func close() {
        lib.pointee.send_destroy(instance)
        lib.pointee.destroy()
    }
}

// MARK: - キャプチャ

final class Capture: NSObject, SCStreamOutput, SCStreamDelegate {
    private var options: Options
    private let ndi: NDISender
    private let queue = DispatchQueue(label: "trkar.screen-ndi.capture")
    private var stream: SCStream?
    private var window: SCWindow?
    private var frames = 0
    private var size = (0, 0)

    init(options: Options) {
        self.options = options
        self.ndi = NDISender(name: options.name, fps: options.fps)
    }

    func start() async {
        guard CGPreflightScreenCaptureAccess() else {
            CGRequestScreenCaptureAccess()
            fail("screen recording permission is not granted (System Settings > Privacy & Security > "
                 + "Screen & System Audio Recording > allow Blender, then restart Blender)", code: 3)
        }
        do {
            guard let window = try await findWindow() else { fail("Blender window (pid \(options.pid)) not found") }
            self.window = window
            let filter = SCContentFilter(desktopIndependentWindow: window)
            let stream = SCStream(filter: filter, configuration: configuration(for: window, filter: filter),
                                  delegate: self)
            try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
            try await stream.startCapture()
            self.stream = stream
        } catch {
            fail("capture failed: \(error.localizedDescription)")
        }
    }

    /// Blender のいちばん大きい通常のウィンドウ
    private func findWindow() async throws -> SCWindow? {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        return content.windows
            .filter { $0.owningApplication?.processID == options.pid && $0.windowLayer == 0 && $0.frame.width > 100 }
            .max { $0.frame.width * $0.frame.height < $1.frame.width * $1.frame.height }
    }

    /// 送る範囲 (ウィンドウの中のポイント、左上が原点) と出力の大きさ
    private func configuration(for window: SCWindow, filter: SCContentFilter) -> SCStreamConfiguration {
        let frame = window.frame
        // 中身 (Blender の画面) の高さ。上の残りがタイトルバー
        let contentHeight = options.aspect > 0 ? min(frame.height, frame.width / options.aspect) : frame.height
        let titlebar = frame.height - contentHeight
        var source = CGRect(x: 0, y: titlebar, width: frame.width, height: contentHeight)
        if let r = options.rect {
            source = CGRect(x: r.minX * frame.width, y: titlebar + (1 - r.minY - r.height) * contentHeight,
                            width: r.width * frame.width, height: r.height * contentHeight)
        }
        let scale = CGFloat(filter.pointPixelScale) / (options.half ? 2 : 1)
        let config = SCStreamConfiguration()
        config.sourceRect = source
        // NDI は奇数の幅・高さだと端の 1 画素を落とすので偶数にそろえる
        config.width = max(2, Int(source.width * scale) & ~1)
        config.height = max(2, Int(source.height * scale) & ~1)
        config.pixelFormat = kCVPixelFormatType_32BGRA
        config.minimumFrameInterval = CMTime(value: 1, timescale: CMTimeScale(options.fps))
        config.queueDepth = 3
        config.showsCursor = options.cursor
        config.capturesAudio = false
        return config
    }

    /// 範囲を変える (標準入力の rect / full)。止めずに設定だけ差し替える
    func update(rect: CGRect?, aspect: Double) {
        options.rect = rect
        options.aspect = aspect
        refresh()
    }

    /// ウィンドウの大きさが変わっていたら、範囲を合わせ直す (1 秒ごと)
    func refresh() {
        Task {
            guard let stream, let found = try? await findWindow() else { return }
            window = found
            let filter = SCContentFilter(desktopIndependentWindow: found)
            try? await stream.updateContentFilter(filter)
            try? await stream.updateConfiguration(configuration(for: found, filter: filter))
        }
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, sampleBuffer.isValid,
              let attachments = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false)
                as? [[SCStreamFrameInfo: Any]],
              let raw = attachments.first?[.status] as? Int,
              SCFrameStatus(rawValue: raw) == .complete,
              let pixels = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        ndi.send(pixels)
        frames += 1
        size = (CVPixelBufferGetWidth(pixels), CVPixelBufferGetHeight(pixels))
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        fail("capture stopped: \(error.localizedDescription)")
    }

    /// 1 秒ごとの状態 (直近 1 秒に送った枚数)
    func report() {
        queue.async {
            print(String(format: "STATUS fps=%d size=%dx%d conns=%d", self.frames, self.size.0, self.size.1,
                         self.ndi.connections))
            self.frames = 0
        }
    }

    func stop() {
        let semaphore = DispatchSemaphore(value: 0)
        Task {
            try? await stream?.stopCapture()
            semaphore.signal()
        }
        _ = semaphore.wait(timeout: .now() + 1)
        ndi.close()
    }
}

// MARK: - main

let options = parseOptions()
let capture = Capture(options: options)

let terminate = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
signal(SIGTERM, SIG_IGN)
terminate.setEventHandler {
    capture.stop()
    exit(0)
}
terminate.resume()

// 標準入力: "rect fx fy fw fh aspect" / "full aspect"
Thread.detachNewThread {
    while let line = readLine() {
        let parts = line.split(separator: " ").map(String.init)
        if parts.first == "rect", parts.count >= 6, let values = Optional(parts[1...5].compactMap { Double($0) }),
           values.count == 5 {
            capture.update(rect: CGRect(x: values[0], y: values[1], width: values[2], height: values[3]),
                           aspect: values[4])
        } else if parts.first == "full", parts.count >= 2, let aspect = Double(parts[1]) {
            capture.update(rect: nil, aspect: aspect)
        }
    }
    // Blender が閉じた (標準入力が閉じた)
    capture.stop()
    exit(0)
}

let timer = DispatchSource.makeTimerSource(queue: .main)
timer.schedule(deadline: .now() + 1, repeating: 1)
timer.setEventHandler {
    if options.parent != 0, kill(options.parent, 0) != 0 {
        capture.stop()
        exit(0)
    }
    capture.report()
    capture.refresh()
}
timer.resume()

Task { await capture.start() }
dispatchMain()
