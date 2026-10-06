import AppKit
import SwiftUI
import Combine
import ApplicationServices
import OrbitCore

@main struct OrbitEntry {
    @MainActor static func main() {
        let application = NSApplication.shared, delegate = AppDelegate()
        application.delegate = delegate; application.setActivationPolicy(.accessory); application.run()
        withExtendedLifetime(delegate) {}
    }
}
final class FacePanel: NSPanel {
    var acceptsKeyboardInput = false
    var endKeyboardInteraction: (() -> Void)?
    override var canBecomeKey: Bool { acceptsKeyboardInput }
    override var canBecomeMain: Bool { false }
    override func cancelOperation(_ sender: Any?) { endKeyboardInteraction?() }
}
@MainActor final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let model = OrbitModel(previewMode: PreviewScenario.parse(arguments: ProcessInfo.processInfo.arguments) != nil)
    private let runtime = BundledRuntime()
    private let setup = SetupState()
    private var welcome: NSWindow?
    private var face: FacePanel!
    private var settings: NSWindow?
    private var statusItem: NSStatusItem!
    private var monitor: TakeoverMonitor?
    private var subscriptions = Set<AnyCancellable>()
    private var workspaceObservers: [NSObjectProtocol] = []
    private var distributedObservers: [NSObjectProtocol] = []
    private var previousApplication: NSRunningApplication?
    private var approvalOrigins: [String: Int32] = [:]
    private var promptedListenAccess = false
    private var listenRetry: Timer?
    func applicationDidFinishLaunching(_ notification: Notification) {
        face = FacePanel(contentRect: NSRect(x: 0, y: 0, width: 224, height: 52), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        face.level = .statusBar; face.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        face.isOpaque = false; face.backgroundColor = .clear; face.hasShadow = true
        face.hidesOnDeactivate = false; face.isReleasedWhenClosed = false; face.becomesKeyOnlyIfNeeded = true
        face.contentView = NSHostingView(rootView: OrbitPanel(model: model)); positionFace()
        face.endKeyboardInteraction = { [weak self] in self?.endKeyboardInteraction() }
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.image = NSImage(systemSymbolName: "sparkle", accessibilityDescription: "Orbit")
        let menu = NSMenu(); menu.delegate = self
        for (title, selector, key) in [("Wake Orbit", #selector(wake), ""), ("Focus Orbit controls", #selector(focusControls), ""), ("Mute / unmute microphone", #selector(mute), ""), ("Stop task", #selector(stop), "."), ("Goodbye", #selector(goodbye), ""), ("Settings & status…", #selector(showSettings), ","), ("Change API keys…", #selector(changeKeys), ""), ("Quit Orbit", #selector(quit), "q")] {
            let item = NSMenuItem(title: title, action: selector, keyEquivalent: key); item.target = self; menu.addItem(item)
        }
        statusItem.menu = menu
        model.visibilityChanged = { [weak self] visible in
            guard let self else { return }; if visible { self.positionFace(); self.face.orderFrontRegardless() } else { self.face.orderOut(nil) }
        }
        model.setupRequested = { [weak self] in self?.showSettings() }
        model.keyboardControlsRequested = { [weak self] in self?.focusControls() }
        model.restoreActionFocus = { [weak self] confirmation in
            guard let self else { return nil }
            return await self.restoreFocus(to: self.approvalOrigins[confirmation.actionID])
        }
        model.permissionsChanged = { [weak self] in self?.installMonitor() }
        model.bridge.refreshTakeover = { [weak self] in
            self?.installMonitor()
            return self?.model.bridge.takeoverAvailable ?? false
        }
        model.bridge.requestTakeover = { [weak self] in self?.promptListenAccess() }
        model.$confirmations.sink { [weak self] confirmations in
            guard let self else { return }
            self.approvalOrigins = self.approvalOrigins.filter { entry in confirmations.contains { $0.actionID == entry.key } }
            let current = NSWorkspace.shared.frontmostApplication
            let origin = current?.processIdentifier == ProcessInfo.processInfo.processIdentifier ? self.previousApplication : current
            for confirmation in confirmations where self.approvalOrigins[confirmation.actionID] == nil {
                self.approvalOrigins[confirmation.actionID] = origin?.processIdentifier
            }
        }.store(in: &subscriptions)
        model.objectWillChange.sink { [weak self] in
            Task { @MainActor in self?.resizePanel() }
        }.store(in: &subscriptions)
        NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main) { [weak self] _ in Task { @MainActor in self?.positionFace() } }
        NotificationCenter.default.addObserver(forName: NSApplication.didBecomeActiveNotification, object: nil, queue: .main) { [weak self] _ in Task { @MainActor in self?.installMonitor() } }
        observeSessionState()
        NotificationCenter.default.addObserver(forName: .orbitReplaceKeys, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.showWelcome() }
        }
        if let scenario = PreviewScenario.parse(arguments: ProcessInfo.processInfo.arguments) { model.preview(scenario); return }
        if BundledRuntime.isBundled {
            NSApp.setActivationPolicy(.regular)
            if SecretStore.hasKeys { beginBundled() } else { showWelcome() }
            return
        }
        installMonitor(); model.launch()
        // Mark onboarding without opening Settings — OS permission prompts are enough.
        if !UserDefaults.standard.bool(forKey: "orbit.onboardingShown") {
            UserDefaults.standard.set(true, forKey: "orbit.onboardingShown")
        }
    }
    private func resizePanel() {
        let size = OrbitPanelMetrics.size(for: model)
        guard face.contentView?.frame.size != size else { return }
        face.setContentSize(size); positionFace()
    }
    private func observeSessionState() {
        let workspace = NSWorkspace.shared.notificationCenter
        for name in [NSWorkspace.willSleepNotification, NSWorkspace.sessionDidResignActiveNotification] {
            workspaceObservers.append(workspace.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.model.suspendForLockOrSleep() }
            })
        }
        workspaceObservers.append(workspace.addObserver(forName: NSWorkspace.sessionDidBecomeActiveNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {
                // Session activation alone is not proof that the lock screen is gone.
                if self?.model.lifecycle.suspended == true { self?.model.status = "Wake Orbit to resume" }
            }
        })
        workspaceObservers.append(workspace.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {
                // didWake can precede unlocking. Stay suspended until unlock/session-active
                // notification or an explicit wake selected in Orbit's visible controls.
                if self?.model.lifecycle.suspended == true { self?.model.status = "Unlock, then wake Orbit" }
            }
        })
        let distributed = DistributedNotificationCenter.default()
        distributedObservers.append(distributed.addObserver(forName: NSNotification.Name("com.apple.screenIsLocked"), object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.model.suspendForLockOrSleep() }
        })
        distributedObservers.append(distributed.addObserver(forName: NSNotification.Name("com.apple.screenIsUnlocked"), object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.model.resumeLocalWake() }
        })
    }
    private func positionFace() {
        guard let screen = NSScreen.screens.first(where: { ($0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value == CGMainDisplayID() }) ?? NSScreen.main else { return }
        let topInset = max(screen.safeAreaInsets.top, screen.frame.maxY - screen.visibleFrame.maxY)
        face.setFrameOrigin(NSPoint(x: screen.frame.midX-face.frame.width/2, y: screen.frame.maxY-topInset-face.frame.height-7))
    }
    private func installMonitor() {
        guard !model.isPreview else { return }
        if monitor == nil {
            monitor = TakeoverMonitor(model: model, isOrbitPoint: { [weak self] point in
                guard let self else { return false }
                let point = NSPoint(x: point.x, y: CGDisplayBounds(CGMainDisplayID()).height-point.y)
                if self.face.isVisible && self.face.frame.contains(point) { return true }
                if let settings = self.settings, settings.isVisible, settings.frame.contains(point) { return true }
                if let button = self.statusItem.button, let window = button.window, window.frame.contains(point) { return true }
                return false
            })
        }
        model.bridge.takeoverAvailable = monitor?.start() ?? false
    }
    private func promptListenAccess() {
        installMonitor()
        guard !model.bridge.takeoverAvailable else { listenRetry?.invalidate(); return }
        if !promptedListenAccess {
            promptedListenAccess = true
            _ = CGRequestListenEventAccess()
            installMonitor()
        }
        guard listenRetry == nil else { return }
        var tries = 0
        listenRetry = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] timer in
            Task { @MainActor in
                guard let self else { timer.invalidate(); return }
                tries += 1
                self.installMonitor()
                if self.model.bridge.takeoverAvailable || tries >= 90 {
                    timer.invalidate(); self.listenRetry = nil
                }
            }
        }
    }
    func menuWillOpen(_ menu: NSMenu) { monitor?.ownMenuOpen = true; installMonitor() }
    func menuDidClose(_ menu: NSMenu) { monitor?.ownMenuOpen = false }
    @objc func wake() { model.wake() }
    @objc func mute() { model.setMuted(!model.lifecycle.muted) }
    @objc func stop() { model.stopTask() }
    @objc func goodbye() { model.goodbye() }
    @objc func quit() { NSApplication.shared.terminate(nil) }
    private func rememberExternalFocus() {
        if let application = NSWorkspace.shared.frontmostApplication,
           application.processIdentifier != ProcessInfo.processInfo.processIdentifier { previousApplication = application }
    }
    @objc func focusControls() {
        rememberExternalFocus(); model.detailsRequested = true; resizePanel()
        face.acceptsKeyboardInput = true; face.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true); face.selectNextKeyView(nil)
    }
    private func endKeyboardInteraction() {
        model.detailsRequested = false
        let pid = previousApplication?.processIdentifier
        Task { _ = await restoreFocus(to: pid) }
    }
    private func restoreFocus(to originalPID: Int32?) async -> Int32? {
        let ownPID = ProcessInfo.processInfo.processIdentifier
        switch FocusRestorationPolicy.decision(orbitPID: ownPID,
            currentPID: NSWorkspace.shared.frontmostApplication?.processIdentifier, originalPID: originalPID) {
        case .reject: return nil
        case .ready: face.acceptsKeyboardInput = false; face.resignKey(); return originalPID
        case .restore(let pid):
            guard let app = NSRunningApplication(processIdentifier: pid), !app.isTerminated else { return nil }
            face.acceptsKeyboardInput = false; face.resignKey()
            guard app.activate(options: []) else { return nil }
            for _ in 0..<10 {
                if NSWorkspace.shared.frontmostApplication?.processIdentifier == pid { return pid }
                try? await Task.sleep(for: .milliseconds(30))
            }
            return nil
        }
    }
    @objc func showSettings() {
        rememberExternalFocus()
        if settings == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 620, height: 620), styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
            window.title = "Orbit · Settings & status"; window.isReleasedWhenClosed = false
            window.contentView = NSHostingView(rootView: SettingsView(model: model)); window.center(); settings = window
        }
        settings?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }
    @objc func changeKeys() { showWelcome() }
    private func showWelcome() {
        NSApp.setActivationPolicy(.regular)
        if welcome == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: 420), styleMask: [.titled, .closable], backing: .buffered, defer: false)
            window.title = "Orbit"; window.isReleasedWhenClosed = false
            window.contentView = NSHostingView(rootView: WelcomeView(setup: setup) { openai, jev in
                SecretStore.saveKeys(openai: openai, jev: jev)
                if BundledRuntime.isBundled { self.beginBundled() } else { self.welcome?.orderOut(nil) }
            })
            window.center(); welcome = window
        }
        welcome?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }
    private func beginBundled() {
        setup.starting = true; setup.failure = ""
        Task {
            do {
                try await runtime.start()
                setup.starting = false
                welcome?.orderOut(nil)
                installMonitor(); model.launch(); requestPermissionsOnce()
            } catch {
                setup.starting = false
                setup.failure = error.localizedDescription
                showWelcome()
            }
        }
    }
    private func requestPermissionsOnce() {
        guard !UserDefaults.standard.bool(forKey: "orbit.permissionsRequested") else { return }
        UserDefaults.standard.set(true, forKey: "orbit.permissionsRequested")
        Task {
            model.requestAccessibility()
            try? await Task.sleep(for: .milliseconds(700))
            model.requestScreenCapture()
            try? await Task.sleep(for: .milliseconds(700))
            model.requestInputMonitoring()
        }
    }
    func applicationWillTerminate(_ notification: Notification) {
        runtime.stop(); model.shutdown(); monitor?.stop()
        for observer in workspaceObservers { NSWorkspace.shared.notificationCenter.removeObserver(observer) }
        for observer in distributedObservers { DistributedNotificationCenter.default().removeObserver(observer) }
    }
}
@MainActor final class TakeoverMonitor {
    private var tap: CFMachPort?
    private var source: CFRunLoopSource?
    private weak var model: OrbitModel?
    private let isOrbitPoint: (CGPoint) -> Bool
    var ownMenuOpen = false
    init(model: OrbitModel, isOrbitPoint: @escaping (CGPoint) -> Bool) { self.model = model; self.isOrbitPoint = isOrbitPoint }
    func start() -> Bool {
        if tap != nil { return true }
        guard CGPreflightListenEventAccess() else { return false }
        tap = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap, options: .listenOnly, eventsOfInterest: TakeoverPolicy.eventMask, callback: { _, type, event, context in
            guard let context else { return Unmanaged.passUnretained(event) }
            let monitor = Unmanaged<TakeoverMonitor>.fromOpaque(context).takeUnretainedValue()
            MainActor.assumeIsolated { monitor.receive(type, event: event) }
            return Unmanaged.passUnretained(event)
        }, userInfo: Unmanaged.passUnretained(self).toOpaque())
        guard let tap else { return false }
        source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), source, .commonModes); CGEvent.tapEnable(tap: tap, enable: true); return true
    }
    private func receive(_ type: CGEventType, event: CGEvent) {
        if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
            // Input may have been missed while the monitor was disabled. Revoke
            // desktop work before restoring monitoring; research stays active.
            model?.takeover()
            if let tap { CGEvent.tapEnable(tap: tap, enable: true) }
            return
        }
        guard let model else { return }
        guard TakeoverPolicy.shouldRevoke(type: type, desktopTasks: model.desktopControlTaskIDs,
            runnableDesktopTasks: model.runnableDesktopTaskIDs, awaitingApproval: Set(model.confirmations.map(\.taskID)),
            synthetic: event.getIntegerValueField(.eventSourceUserData) == DeviceBridge.syntheticMarker,
            ownMenuOpen: ownMenuOpen, orbitOwnsKeyboard: NSApp.isActive && NSApp.keyWindow?.isKeyWindow == true,
            pointerInsideOrbit: isOrbitPoint(event.location)) else { return }
        model.takeover()
    }
    func stop() {
        if let source { CFRunLoopRemoveSource(CFRunLoopGetMain(), source, .commonModes) }
        if let tap { CFMachPortInvalidate(tap) }; source = nil; tap = nil
    }
}

@MainActor enum OrbitPanelMetrics {
    static func size(for model: OrbitModel) -> NSSize {
        guard model.expanded else { return NSSize(width: 224, height: 52) }
        let height: CGFloat = !model.confirmations.isEmpty ? 350 : (!model.issue.isEmpty ? 248 : (!model.activeTasks.isEmpty ? 218 : 136))
        return NSSize(width: 340, height: height)
    }
}

struct OrbitPanel: View {
    @ObservedObject var model: OrbitModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private var label: String { model.lifecycle.muted ? "Muted" : model.displayStatus }
    private var spring: Animation { .spring(response: 0.7, dampingFraction: 0.82) }
    var body: some View {
        let size = OrbitPanelMetrics.size(for: model)
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                AudioWaveform(levels: model.lifecycle.muted ? Array(repeating: 0, count: 10) : model.waveformLevels, speaking: model.speaking && !model.lifecycle.muted)
                    .frame(width: 46, height: 25).accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        if !model.issue.isEmpty { Circle().fill(Color(red: 0.92, green: 0.62, blue: 0.20)).frame(width: 6, height: 6) }
                        Text(label).font(.system(size: 13, weight: .medium, design: .rounded)).foregroundStyle(Color(red: 1, green: 0.97, blue: 0.94)).lineLimit(1)
                    }
                    if model.expanded { Text(model.isPreview ? "Offline preview" : "Orbit").font(.system(size: 10, design: .rounded)).foregroundStyle(.secondary) }
                }
                Spacer(minLength: 4)
                Button { model.detailsRequested.toggle() } label: {
                    Image(systemName: model.expanded ? "chevron.up" : "ellipsis").font(.system(size: 12, weight: .semibold)).frame(width: 28, height: 28)
                }.buttonStyle(.plain).accessibilityLabel(model.expanded ? "Collapse Orbit details" : "Show Orbit controls")
                    .help("Show Orbit controls")
            }.padding(.horizontal, 16).frame(height: 52)
            if model.expanded {
                Group {
                if let confirmation = model.confirmations.first {
                    approval(confirmation)
                } else if let followUp = model.followUps.first {
                    VStack(alignment: .leading, spacing: 10) {
                        Text(followUp.deliveryID == nil ? "Schedule this follow-up?" : "A little follow-up").font(.headline)
                        ScrollView { Text(followUp.text).textSelection(.enabled); Text(followUp.detail).font(.caption).foregroundStyle(.secondary) }
                        HStack {
                            Button(followUp.deliveryID == nil ? "Decline" : "Cancel reminder") { model.respondFollowUp(followUp, accept: false) }
                            Spacer()
                            Button(followUp.deliveryID == nil ? "Schedule" : "Dismiss") { model.respondFollowUp(followUp, accept: true) }
                        }
                    }.padding(16)
                } else if !model.issue.isEmpty {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 8) {
                            Text(model.issue).font(.system(size: 12, design: .rounded)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                        }.frame(maxWidth: .infinity, alignment: .leading).padding(16)
                    }
                } else if !model.activeTasks.isEmpty {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 10) {
                            Text("In progress").font(.system(size: 11, weight: .medium, design: .rounded)).foregroundStyle(.secondary)
                            ForEach(model.activeTasks.keys.sorted(), id: \.self) { id in
                                HStack(alignment: .top, spacing: 9) {
                                    Image(systemName: "circle.dotted").foregroundStyle(.secondary)
                                    Text(model.activeTasks[id] ?? "Working").font(.system(size: 12, design: .rounded)).fixedSize(horizontal: false, vertical: true)
                                }
                            }
                        }.frame(maxWidth: .infinity, alignment: .leading).padding(16)
                    }
                } else { Spacer(minLength: 0) }
                controls.animation(reduceMotion ? nil : spring.delay(0.04), value: model.expanded)
                }
                .transition(.opacity.combined(with: .scale(scale: 0.96, anchor: .top)))
            }
        }
        .frame(width: size.width, height: size.height)
        .modifier(OrbitGlassSurface(expanded: model.expanded))
        .animation(reduceMotion ? nil : spring, value: model.expanded)
        .accessibilityElement(children: .contain).accessibilityLabel("Orbit, \(label)")
    }
    private func approval(_ confirmation: Confirmation) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Review this action").font(.system(size: 12, weight: .semibold))
                Spacer()
                if model.confirmations.count > 1 { Text("1 of \(model.confirmations.count)").font(.caption).foregroundStyle(.secondary) }
            }
            ScrollView {
                Text(confirmation.summary).font(.system(size: 12)).frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            }.frame(maxHeight: .infinity)
            HStack(spacing: 12) {
                Button("Reject") { model.respond(confirmation, approved: false, source: .button) }
                    .buttonStyle(.plain).font(.system(size: 13, weight: .medium, design: .rounded))
                Button { model.respond(confirmation, approved: true, source: .button) } label: {
                    HStack(spacing: 8) {
                        Image(systemName: "checkmark").font(.system(size: 11, weight: .semibold))
                            .frame(width: 18, height: 18)
                            .overlay(Circle().stroke(Color.white.opacity(0.45), lineWidth: 1))
                        Text("Approve").font(.system(size: 13, weight: .medium, design: .rounded))
                    }
                    .frame(maxWidth: .infinity).padding(.vertical, 8)
                    .background(Capsule().fill(Color.white.opacity(0.14)))
                }.buttonStyle(.plain).modifier(ApprovePress())
            }.disabled(model.respondingActions.contains(confirmation.actionID))
            Text("Approval applies only to this exact action.").font(.system(size: 10)).foregroundStyle(.secondary)
        }.padding(.horizontal, 16).padding(.top, 14).padding(.bottom, 8)
    }
    private var controls: some View {
        HStack(spacing: 18) {
            Button { model.setMuted(!model.lifecycle.muted) } label: { Image(systemName: model.lifecycle.muted ? "mic.slash" : "mic") }
                .accessibilityLabel(model.lifecycle.muted ? "Unmute microphone" : "Mute microphone").help(model.lifecycle.muted ? "Unmute microphone" : "Mute microphone")
            Button { model.stopTask() } label: { Image(systemName: "stop.fill") }.accessibilityLabel("Stop task").help("Stop task")
            Button { model.goodbye() } label: { Image(systemName: "moon") }.accessibilityLabel("Goodbye").help("Goodbye")
            Spacer()
            Button { model.keyboardControlsRequested?() } label: { Image(systemName: "keyboard") }.accessibilityLabel("Focus Orbit controls for keyboard").help("Keyboard controls · Escape returns to your app")
            Button { model.setupRequested?() } label: { Image(systemName: "slider.horizontal.3") }.accessibilityLabel("Settings and status").help("Settings and status")
        }.font(.system(size: 13)).buttonStyle(.plain).padding(.horizontal, 20).frame(height: 44)
    }
}

private struct ApprovePress: ViewModifier {
    @GestureState private var pressed = false
    func body(content: Content) -> some View {
        content.scaleEffect(pressed ? 0.98 : 1)
            .simultaneousGesture(DragGesture(minimumDistance: 0).updating($pressed) { _, state, _ in state = true })
    }
}

private struct AudioWaveform: View {
    let levels: [Float]
    let speaking: Bool
    var body: some View {
        Canvas { context, size in
            guard !levels.isEmpty else { return }
            let step = size.width / CGFloat(levels.count)
            let chroma = Color.white.opacity(speaking ? 0.92 : 0.34)
            for (index, level) in levels.enumerated() {
                let height = max(3, min(size.height, CGFloat(sqrt(max(0, level))) * size.height * 2))
                let bar = CGRect(x: CGFloat(index) * step, y: (size.height - height) / 2, width: 2.5, height: height)
                context.fill(Path(roundedRect: bar, cornerRadius: 1.25), with: .color(chroma))
            }
        }
    }
}

private struct OrbitGlassSurface: ViewModifier {
    let expanded: Bool
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    func body(content: Content) -> some View {
        let outerRadius: CGFloat = expanded ? 28 : 26
        let inner = RoundedRectangle(cornerRadius: outerRadius - 3, style: .continuous)
        let outer = RoundedRectangle(cornerRadius: outerRadius, style: .continuous)
        content
            .background {
                Group {
                    if reduceTransparency {
                        inner.fill(Color(nsColor: .windowBackgroundColor))
                    } else if #available(macOS 26, *) {
                        Color.clear.glassEffect(.regular, in: inner)
                    } else {
                        inner.fill(.ultraThinMaterial)
                    }
                }
                .padding(3)
            }
            .overlay(alignment: .top) {
                inner.fill(LinearGradient(colors: [Color.white.opacity(0.28), .clear], startPoint: .top, endPoint: .center))
                    .frame(height: 16).padding(.top, 4).padding(.horizontal, 8).allowsHitTesting(false)
            }
            .overlay(outer.stroke(Color.white.opacity(0.28), lineWidth: 0.6))
            .shadow(color: .black.opacity(0.06), radius: 18, y: 4)
    }
}
struct SettingsView: View {
    @ObservedObject var model: OrbitModel
    @State private var input = ""
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                HStack { Text("Orbit").font(.system(size: 32, weight: .medium, design: .rounded)); Spacer(); Text(model.displayStatus).foregroundStyle(.secondary) }
                Text("A small presence. A little follow-through.").foregroundStyle(.secondary)
                if !model.issue.isEmpty { Text(model.issue).font(.callout).padding(12).frame(maxWidth: .infinity, alignment: .leading).background(.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 8)).textSelection(.enabled) }
                HStack {
                    Button("Wake") { model.wake() }; Button("Reconnect") { model.reconnect() }
                    Button(model.lifecycle.muted ? "Unmute" : "Mute") { model.setMuted(!model.lifecycle.muted) }
                    Button("Stop task") { model.stopTask() }; Button("Goodbye") { model.goodbye() }
                }
                Divider(); Text("Permissions & local wake").font(.headline)
                Text("On-device wake and stop detection requires macOS 26 and Apple's English speech assets. Hidden standby keeps local listening active. Mute stops microphone capture entirely.").font(.callout).foregroundStyle(.secondary)
                HStack {
                    Button(model.downloadingSpeech ? "Preparing speech…" : "Enable voice & download speech assets") { Task { await model.enableLocalSpeech(installAssets: true) } }.disabled(model.downloadingSpeech)
                    Image(systemName: model.localSpeechReady ? "checkmark.circle.fill" : "circle").foregroundStyle(model.localSpeechReady ? Color.green : Color.secondary)
                }
                HStack {
                    Button("Accessibility") { model.requestAccessibility() }; Button("Screen Recording") { model.requestScreenCapture() }
                    Button("Input Monitoring") { model.requestInputMonitoring() }
                }
                Text("Allow Apple Notes Automation when macOS asks during your first note. Physical input outside Orbit stops automation. Restart Orbit after changing OS permissions if macOS asks.").font(.caption).foregroundStyle(.secondary)
                if BundledRuntime.isBundled {
                    Divider(); Text("API keys").font(.headline)
                    Text("OpenAI and Jev keys stay in this Mac’s keychain. Changing them restarts the built-in services.").font(.caption).foregroundStyle(.secondary)
                    Button("Change API keys") { NotificationCenter.default.post(name: .orbitReplaceKeys, object: nil) }
                }
                Divider(); Text("Typed conversation").font(.headline)
                HStack {
                    TextField("Wake Orbit, then type a request…", text: $input).textFieldStyle(.roundedBorder).onSubmit(send)
                    Button("Send", action: send)
                }
                if model.dictating { Button("Finish dictation") { model.finishDictation() } }
                if !model.transcript.isEmpty { Text(model.transcript).font(.callout).textSelection(.enabled) }
                ForEach(model.activeTasks.keys.sorted(), id: \.self) { id in Text(model.activeTasks[id] ?? "").font(.caption).foregroundStyle(.secondary) }
                Divider(); Text("Connection").font(.headline)
                Text(BundledRuntime.isBundled
                     ? "This copy starts its own services. Keys stay in the keychain."
                     : "Device config is written automatically by scripts/launch.py. If reconnect fails, run setup + launch from the orbit folder.")
                    .font(.caption).foregroundStyle(.secondary)
                Text(Configuration.configURL.path).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                Text("No long-term memory. Microphone audio stays local while in standby or muted. Task screenshots require an active task and macOS permission.").font(.caption).foregroundStyle(.secondary)
            }.padding(26)
        }.frame(minWidth: 560, minHeight: 550)
    }
    private func send() { model.submitText(input); input = "" }
}
