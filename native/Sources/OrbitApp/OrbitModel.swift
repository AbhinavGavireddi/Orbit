import AppKit
import AVFoundation
import Speech
import OrbitCore

struct Confirmation: Identifiable {
    let taskID: String, actionID: String, version: Int, summary: String
    var id: String { actionID }
    var identity: ApprovalIdentity { ApprovalIdentity(taskID: taskID, actionID: actionID, version: version) }
}

struct FollowUpCard: Identifiable {
    let id: String, version: Int, text: String, detail: String
    let deliveryID: String?
}

@MainActor final class OrbitModel: ObservableObject {
    @Published private(set) var lifecycle = Lifecycle()
    @Published var status = "Standby"
    @Published var issue = ""
    @Published var transcript = ""
    @Published var microphoneLevel: Float = 0
    private var recentMeterPeak: Float = 0
    private var lastMeterAt: TimeInterval = 0
    @Published var waveformLevels = Array(repeating: Float(0), count: 10)
    @Published var speaking = false
    @Published var detailsRequested = false
    private var hadIssueChrome = false
    private var hadTaskChrome = false
    @Published private(set) var respondingActions = Set<String>()
    @Published var connected = false
    @Published var localSpeechReady = false
    @Published var downloadingSpeech = false
    @Published var confirmations: [Confirmation] = []
    @Published var followUps: [FollowUpCard] = []
    var displayStatus: String { PresentationCoordinator.label(conversation: status, approval: !confirmations.isEmpty, speaking: speaking, tasks: activeTasks.count) }
    @Published var activeTasks: [String: String] = [:]
    @Published var dictating = false
    let guardrail = CommandGuard()
    var desktopControlTaskIDs: Set<String> { guardrail.desktopTaskIDs }
    var runnableDesktopTaskIDs: Set<String> { guardrail.runnableDesktopTaskIDs }
    var expanded: Bool { PanelPresentation.isExpanded(requested: detailsRequested, hasApproval: !confirmations.isEmpty || !followUps.isEmpty, hasIssue: !issue.isEmpty, hasTasks: !activeTasks.isEmpty) }
    var isPreview: Bool { previewMode }
    lazy var bridge = DeviceBridge(guardrail: guardrail)
    private lazy var audio = AudioIO()
    private lazy var permissionCache = PermissionSnapshotCache {
        PermissionSnapshot(
            microphoneAuthorized: AVCaptureDevice.authorizationStatus(for: .audio) == .authorized,
            speechAuthorized: SFSpeechRecognizer.authorizationStatus() == .authorized
        )
    }
    private let taskSocket = SocketLink()
    private let voiceSocket = SocketLink()
    private var api: APIClient?
    private var sessionID: String?
    private var epoch = UUID()
    private var speech: Any?
    private var speechSetupEpoch = UUID()
    private var voiceReady = false
    private var voiceItem: (String, Int)?
    private var playedPositions: [String: Int] = [:]
    private var dictationTarget: (task: String, note: String)?
    private var finishingDictation = false
    private var stopping = false
    private var previewMode = false
    private var resumeLocalAfterUnlock = false
    private var pendingSpeechSetup: Bool?
    private var workQueue: [[String: Any]] = []
    private var workRunner: Task<Void, Never>?
    private var connectTask: Task<Void, Never>?
    private var openedArtifacts = Set<String>()
    private var policyVisibilityGate = PolicyVisibilityGate()
    private var speechRestartBudget = LocalSpeechRestartBudget()
    var visibilityChanged: ((Bool) -> Void)?
    var setupRequested: (() -> Void)?
    var permissionsChanged: (() -> Void)?
    var keyboardControlsRequested: (() -> Void)?
    var restoreActionFocus: ((Confirmation) async -> Int32?)?

    init(previewMode: Bool = false) {
        self.previewMode = previewMode
        guard !previewMode else { return }
        taskSocket.onJSON = { [weak self] in self?.taskFrame($0) }
        voiceSocket.onJSON = { [weak self] in self?.voiceFrame($0) }
        taskSocket.onFailure = { [weak self] in self?.connectionFailed("Task connection: " + $0) }
        voiceSocket.onFailure = { [weak self] in self?.connectionFailed("Voice connection: " + $0) }
        voiceSocket.onBinary = { [weak self] data in
            guard let self, self.lifecycle.awake, !self.dictating, let item = self.voiceItem else { return }
            self.audio.play(data, item: item.0, index: item.1)
        }
        audio.onPCM = { [weak self] data, capture, stream, session in Task { @MainActor in
            guard let self, self.audio.acceptsStream(capture: capture, stream: stream, session: session), self.lifecycle.streamsMicrophone, self.voiceReady, !self.finishingDictation else { return }
            self.voiceSocket.send(data)
        } }
        audio.onPlayed = { [weak self] item, index, ms, session in Task { @MainActor in
            guard let self, self.audio.acceptsSession(session) else { return }
            self.reportPlayed(item: item, index: index, milliseconds: ms)
        } }
        audio.onError = { [weak self] message in Task { @MainActor in self?.issue = message } }
        audio.onCaptureFailure = { [weak self] message in Task { @MainActor in
            self?.localSpeechReady = false; self?.microphoneLevel = 0; self?.issue = message; self?.applyListeningStatus()
        } }
        audio.onInputMeter = { [weak self] value, generation in Task { @MainActor in
            guard let self, self.audio.acceptsCapture(generation) else { return }
            self.microphoneLevel = value
            self.recentMeterPeak = max(self.recentMeterPeak * 0.85, value)
            self.lastMeterAt = ProcessInfo.processInfo.systemUptime
            if !self.speaking { self.appendLevel(value) }
            if self.lifecycle.awake { self.applyListeningStatus() }
        } }
        audio.onOutputMeter = { [weak self] value, generation in Task { @MainActor in
            guard let self, self.audio.acceptsPlayback(generation) else { return }
            if self.speaking { self.appendLevel(value) }
        } }
        audio.onPlaybackChanged = { [weak self] value, generation in Task { @MainActor in
            guard let self, self.audio.acceptsPlayback(generation) else { return }
            self.speaking = value
            self.voiceSocket.send(["type": "playback.state", "playing": value])
            if !value { self.waveformLevels = Array(repeating: 0, count: 10) }
        } }
        audio.onLocalInterruption = { [weak self] heard, session, playback in Task { @MainActor in
            guard let self, self.audio.acceptsSession(session), self.lifecycle.streamsMicrophone,
                  self.voiceReady, !self.dictating else { return }
            for (item, index, ms) in heard { self.reportPlayed(item: item, index: index, milliseconds: ms) }
            // A server clear or another local control may already have interrupted
            // this playback while the callback waited for the UI actor.
            guard self.audio.acceptsPlayback(playback) else { return }
            self.voiceSocket.send(["type": "interrupt", "positions": self.finalPositions(heard)])
            self.applyListeningStatus()
        } }
    }
    private func appendLevel(_ value: Float) {
        waveformLevels.removeFirst(); waveformLevels.append(value.isFinite ? max(0, value) : 0)
    }

    private func revealFaceExplicitly() {
        policyVisibilityGate.sync(revealed: true)
        visibilityChanged?(true)
    }

    private func hideFaceExplicitly() {
        policyVisibilityGate.sync(revealed: false)
        visibilityChanged?(false)
    }

    private func applyPolicyVisibility(shouldReveal: Bool) {
        if let visible = policyVisibilityGate.observe(shouldReveal: shouldReveal) {
            visibilityChanged?(visible)
        }
    }

    /// Design contract: Listening only with live meter. Permission chrome only when TCC is actually missing.
    private func applyListeningStatus() {
        guard !previewMode else { return }
        // Peak ages out quickly so a stuck Listening label cannot survive silence/dead capture.
        if ProcessInfo.processInfo.systemUptime - lastMeterAt > 0.75 { recentMeterPeak = 0 }
        let permissions = permissionCache.current
        status = ListeningStatusPolicy.label(
            microphoneAuthorized: permissions.microphoneAuthorized,
            speechAuthorized: permissions.speechAuthorized,
            captureReady: localSpeechReady && !lifecycle.muted && !lifecycle.suspended,
            recentMeterPeak: recentMeterPeak
        )
        let permissionChrome = ListeningStatusPolicy.isPermissionState(status)
        if permissionChrome {
            waveformLevels = Array(repeating: 0, count: 10)
            if issue.isEmpty || issue.hasPrefix("Orbit needs Microphone") || issue.hasPrefix("Orbit needs Speech") {
                if status == ListeningStatusPolicy.needsMicrophone {
                    issue = "Orbit needs Microphone access. Allow it when macOS asks."
                } else {
                    issue = "Orbit needs Speech Recognition access. Allow it when macOS asks."
                }
            }
        } else if issue.hasPrefix("Orbit needs Microphone") || issue.hasPrefix("Orbit needs Speech") {
            issue = ""
        }
        applyPolicyVisibility(
            shouldReveal: ListeningStatusPolicy.shouldReveal(status: status, explicitlyAwake: lifecycle.awake)
        )
        // Auto-open details once when attention appears; chevron can still collapse (approvals stay forced open).
        let issueChrome = !issue.isEmpty
        if issueChrome && !hadIssueChrome { detailsRequested = true }
        hadIssueChrome = issueChrome
        let taskChrome = !activeTasks.isEmpty
        if taskChrome && !hadTaskChrome { detailsRequested = true }
        hadTaskChrome = taskChrome
    }
    func launch() {
        applyListeningStatus()
        // Always attempt local speech setup so macOS prompts for Mic/Speech on first run.
        Task { await enableLocalSpeech(installAssets: true) }
    }
    func preview(_ scenario: PreviewScenario) {
        previewMode = true; lifecycle.wake(); connected = true
        switch scenario {
        case .listening: status = "Listening"
        case .speaking:
            status = "Speaking"; speaking = true
            waveformLevels = [0.04, 0.12, 0.07, 0.21, 0.3, 0.18, 0.08, 0.24, 0.11, 0.05]
        case .working:
            status = "Working"; activeTasks = ["preview-research": "Comparing three desktop robot approaches and checking their sources."]
        case .error:
            status = "Reconnect"; issue = "Voice connection interrupted. Your local controls are still available. Reconnect when you are ready."
        case .approval:
            status = "Your approval"
            confirmations = [Confirmation(taskID: "preview-task", actionID: "preview-action", version: 1,
                summary: "In Google Chrome, open the Spotify web player and select the Play button for the playlist you requested.\n\nThis action controls the current Chrome tab. Confirm that it is the intended playlist before continuing.\n\nExpected action: click the visible Play button once. No purchase, account change, or message will be sent.\n\nThis is a preview. Approve and Reject change only this local preview.")]
        }
        revealFaceExplicitly()
    }
    private func reportPlayed(item: String, index: Int, milliseconds: Int) {
        guard voiceReady else { return }
        let key = "\(item):\(index)"
        guard milliseconds >= playedPositions[key, default: 0] else { return }
        playedPositions[key] = milliseconds
        voiceSocket.send(["type": "audio.played", "item_id": item, "content_index": index, "audio_end_ms": milliseconds])
    }
    private func finalPositions(_ heard: [AudioIO.Played]) -> [[String: Any]] {
        var positions = heard.map { item, index, milliseconds in
            ["item_id": item, "content_index": index,
             "audio_end_ms": max(milliseconds, playedPositions["\(item):\(index)", default: 0])] as [String: Any]
        }
        if let (item, index) = voiceItem, !heard.contains(where: { $0.0 == item && $0.1 == index }) {
            positions.append(["item_id": item, "content_index": index,
                              "audio_end_ms": playedPositions["\(item):\(index)", default: 0]])
        }
        return positions
    }
    @discardableResult private func clearPlayback() -> [[String: Any]] {
        let heard = audio.clearPlayback(blockingItem: voiceItem?.0)
        for (item, index, milliseconds) in heard { reportPlayed(item: item, index: index, milliseconds: milliseconds) }
        speaking = false; waveformLevels = Array(repeating: 0, count: 10)
        return finalPositions(heard)
    }
    func wake() {
        if lifecycle.suspended { resumeLocalWake() }
        if previewMode { lifecycle.wake(); status = "Listening"; revealFaceExplicitly(); return }
        if lifecycle.awake && connected { return }
        lifecycle.wake(); revealFaceExplicitly()
        connect()
    }
    func reconnect() { if lifecycle.suspended { resumeLocalWake() }; if previewMode { wake(); return }; if !lifecycle.awake { lifecycle.wake(); revealFaceExplicitly() }; disconnect(); connect() }
    func suspendForLockOrSleep() {
        guard !lifecycle.suspended else { return }
        if previewMode { lifecycle.suspend(); hideFaceExplicitly(); return }
        guardrail.revokeAll()
        resumeLocalAfterUnlock = localSpeechReady && !lifecycle.muted
        let oldSpeechEpoch = speechSetupEpoch
        lifecycle.suspend(); clearPlayback(); audio.stopCapture(); speechSetupEpoch = UUID(); localSpeechReady = false; microphoneLevel = 0
        if #available(macOS 26, *), let speech = speech as? LocalSpeech { Task { await speech.stop(ifRequest: oldSpeechEpoch) } }
        disconnect(); stopping = false; status = "Locked / asleep"; hideFaceExplicitly()
    }
    func resumeLocalWake() {
        guard lifecycle.suspended else { return }
        lifecycle.resumeLocal(); status = "Standby"
        let restore = resumeLocalAfterUnlock; resumeLocalAfterUnlock = false
        if restore && !lifecycle.muted && !previewMode { Task { await enableLocalSpeech(installAssets: false) } }
        // Unlock never reconnects cloud voice or revives a task. Wake is a new request.
    }
    private func connect() {
        guard connectTask == nil else { return }
        status = "Connecting"
        let expectedEpoch = epoch
        connectTask = Task {
            defer { if epoch == expectedEpoch { connectTask = nil } }
            do {
                let configuration = try Configuration.load()
                guard !configuration.token.isEmpty else { throw OrbitError("Set ORBIT_DEVICE_TOKEN in the launch environment or ~/Library/Application Support/Orbit/config.json, then reconnect.") }
                let api = APIClient(configuration); self.api = api
                bridge.downloadArtifact = { id, filename in try await api.artifact(id: id, filename: filename) }
                let result = try await api.request("v1/sessions", body: ["device_id": configuration.deviceID])
                guard epoch == expectedEpoch, lifecycle.awake, !Task.isCancelled else {
                    if let id = result["session_id"] as? String { _ = try? await api.request("v1/sessions/\(id)/close") }; return
                }
                guard let id = result["session_id"] as? String else { throw OrbitError("Task service returned no session ID.") }
                sessionID = id; guardrail.activate(sessionID: id)
                let url = try api.websocketURL(base: configuration.taskURL, path: "v1/devices/\(configuration.deviceID)/ws", sessionID: id)
                taskSocket.connect(url: url, token: configuration.token, taskHeartbeat: true)
                // Cloud voice waits for the task service's connected frame.
                issue = ""
            } catch { if epoch == expectedEpoch, !Task.isCancelled { connectionFailed(error.localizedDescription) } }
        }
    }
    private func connectVoice() {
        guard let api, let sessionID, lifecycle.awake, !voiceReady else { return }
        do {
            let url = try api.websocketURL(base: api.configuration.voiceURL, path: "v1/voice", sessionID: sessionID)
            voiceSocket.connect(url: url, token: api.configuration.token, taskHeartbeat: false)
        } catch { connectionFailed(error.localizedDescription) }
    }
    private func disconnect(finalVoiceFrame: [String: Any]? = nil) {
        guardrail.revokeAll() // synchronous, before any await/network cancellation
        epoch = UUID()
        connected = false; audio.setStreaming(false); clearPlayback(); audio.resetSession(); voiceReady = false; playedPositions.removeAll()
        bridge.reset(); workQueue.removeAll(); workRunner?.cancel(); workRunner = nil
        confirmations.removeAll(); followUps.removeAll(); respondingActions.removeAll(); activeTasks.removeAll(); dictating = false; dictationTarget = nil; finishingDictation = false
        connectTask?.cancel(); connectTask = nil
        taskSocket.close(); voiceSocket.close(finalFrame: finalVoiceFrame); voiceItem = nil
        if let id = sessionID, let api { Task { _ = try? await api.request("v1/sessions/\(id)/close") } }
        sessionID = nil
    }
    func goodbye() {
        if previewMode { lifecycle.goodbye(); status = "Standby"; hideFaceExplicitly(); return }
        guardrail.revokeAll()
        let positions = clearPlayback()
        lifecycle.goodbye()
        disconnect(finalVoiceFrame: ["type": "control", "command": "goodbye", "positions": positions])
        status = "Standby"; hideFaceExplicitly()
        // Capture stays local in standby unless muted.
    }
    func stopTask() {
        if previewMode { status = "Stopped"; speaking = false; activeTasks.removeAll(); confirmations.removeAll(); return }
        guard !stopping else { return }; stopping = true
        guardrail.revokeAll(); bridge.cancel()
        let positions = clearPlayback()
        disconnect(finalVoiceFrame: ["type": "control", "command": "stop", "positions": positions])
        status = "Stopped"
        if lifecycle.awake { connect() } // fresh session makes all old queued commands invalid
    }
    func takeover() {
        let ids = guardrail.revokeDesktopTasks() // atomically revokes only automation/dictation
        guard !ids.isEmpty else { return }
        cancelNativeWork(taskIDs: ids)
        for id in ids {
            activeTasks.removeValue(forKey: id)
            taskSocket.send(["type": "device.takeover", "task_id": id])
        }
        confirmations.removeAll { ids.contains($0.taskID) }
        status = "Desktop task paused"
        issue = "Physical input stopped desktop control. Research and conversation remain active; ask for a new desktop task when ready."
    }
    private func cancelNativeWork(taskIDs: Set<String>) {
        bridge.cancel(taskIDs: taskIDs)
        workQueue.removeAll { frame in (frame["task_id"] as? String).map(taskIDs.contains) ?? false }
        if let target = dictationTarget, taskIDs.contains(target.task) {
            // The target is revoked: never flush additional text into it. Preserve the voice
            // connection and briefly hold mic frames until the transcription mode has closed.
            dictating = false; dictationTarget = nil; finishingDictation = true
            audio.setStreaming(false)
            voiceSocket.send(["type": "dictation.stop", "flush": false])
        }
    }
    func setMuted(_ value: Bool) {
        guard lifecycle.muted != value else { return }
        lifecycle.setMuted(value)
        if previewMode { microphoneLevel = 0; return }
        if value {
            let positions = clearPlayback()
            voiceSocket.send(["type": "interrupt", "positions": positions])
            let oldSpeechEpoch = speechSetupEpoch
            audio.stopCapture(); microphoneLevel = 0; speechSetupEpoch = UUID(); localSpeechReady = false
            if #available(macOS 26, *), let speech = speech as? LocalSpeech { Task { await speech.stop(ifRequest: oldSpeechEpoch) } }
        } else if !lifecycle.suspended { Task { await enableLocalSpeech(installAssets: false) } }
    }
    func enableLocalSpeech(installAssets: Bool) async {
        guard !previewMode else { issue = "Preview mode does not request microphone or speech access."; return }
        guard !lifecycle.suspended, !lifecycle.muted else { return }
        guard !downloadingSpeech else { pendingSpeechSetup = (pendingSpeechSetup ?? false) || installAssets; return }
        guard #available(macOS 26, *) else { issue = "Local wake requires macOS 26. Use typed controls on this Mac."; return }
        let setupEpoch = UUID(); speechSetupEpoch = setupEpoch
        downloadingSpeech = true
        defer {
            downloadingSpeech = false
            if let pending = pendingSpeechSetup {
                pendingSpeechSetup = nil
                if !lifecycle.muted, !lifecycle.suspended { Task { await enableLocalSpeech(installAssets: pending) } }
            }
        }
        // Do not re-prompt when already decided — ad-hoc builds + repeated requestAuthorization feel like a loop.
        let micStatus = AVCaptureDevice.authorizationStatus(for: .audio)
        if micStatus == .denied || micStatus == .restricted {
            issue = "Microphone permission denied. Enable Orbit in System Settings → Privacy & Security → Microphone."
            permissionCache.refresh(); applyListeningStatus(); return
        }
        if micStatus != .authorized {
            let granted = await AVCaptureDevice.requestAccess(for: .audio)
            permissionCache.refresh()
            guard granted else {
                issue = "Microphone permission denied. Enable Orbit in System Settings → Privacy & Security → Microphone."
                applyListeningStatus(); return
            }
        }
        guard speechSetupEpoch == setupEpoch, !lifecycle.muted, !lifecycle.suspended else { return }
        let speechStatus = SFSpeechRecognizer.authorizationStatus()
        let permission: SFSpeechRecognizerAuthorizationStatus
        if speechStatus == .authorized {
            permission = .authorized
        } else if speechStatus == .denied || speechStatus == .restricted {
            permission = speechStatus
        } else {
            permission = await withCheckedContinuation { continuation in
                SFSpeechRecognizer.requestAuthorization { continuation.resume(returning: $0) }
            }
        }
        guard speechSetupEpoch == setupEpoch, !lifecycle.muted, !lifecycle.suspended else { return }
        permissionCache.refresh()
        guard permission == .authorized else {
            issue = "Speech Recognition permission denied. Enable Orbit in System Settings → Privacy & Security → Speech Recognition."
            applyListeningStatus(); return
        }
        applyListeningStatus()
        do {
            let speech = (self.speech as? LocalSpeech) ?? LocalSpeech(); self.speech = speech
            let (format, input) = try await speech.start(
                request: setupEpoch,
                installAssets: installAssets,
                onText: { [weak self] text, final in
                    Task { @MainActor in
                        guard let self, self.speechSetupEpoch == setupEpoch else { return }
                        self.localText(text, final: final)
                    }
                },
                onSpeechActivity: { [weak audio] detected in
                    audio?.observeSpeech(detected, token: setupEpoch)
                },
                onFailure: { [weak self] in
                    Task { @MainActor in
                        guard let self, self.speechSetupEpoch == setupEpoch else { return }
                        self.restartLocalSpeech()
                    }
                }
            )
            guard speechSetupEpoch == setupEpoch, !lifecycle.muted, !lifecycle.suspended else { await speech.stop(ifRequest: setupEpoch); return }
            audio.configureSpeech(format: format, token: setupEpoch, sink: { buffer in input.yield(AnalyzerInput(buffer: buffer)) })
            audio.startCapture(); audio.setStreaming(lifecycle.streamsMicrophone && voiceReady)
            localSpeechReady = true; issue = ""; applyListeningStatus()
        } catch {
            guard speechSetupEpoch == setupEpoch, !lifecycle.muted, !lifecycle.suspended else { return }
            issue = error.localizedDescription; localSpeechReady = false
            applyListeningStatus()
        }
    }
    private func restartLocalSpeech() {
        guard !lifecycle.muted, !lifecycle.suspended else { return }
        localSpeechReady = false
        if speechRestartBudget.allow(now: ProcessInfo.processInfo.systemUptime) {
            issue = ""
            applyListeningStatus()
            Task { await enableLocalSpeech(installAssets: false) }
            return
        }
        issue = "Local speech recognition stopped. Enable voice again in Settings."
        applyListeningStatus()
    }
    private func localText(_ text: String, final: Bool) {
        guard !lifecycle.muted else { return }
        switch LocalSpeechLifecyclePolicy.decide(text: text, failed: false) {
        case .ignore:
            return
        case .restart:
            restartLocalSpeech(); return
        case .use(let text):
            // Even wake must be final: a partial "Orbit wake up" may continue with
            // "tomorrow". SpeechDetector handles immediate interruption separately.
            guard let phrase = LocalControlPolicy.match(text, final: final, awake: lifecycle.awake) else { return }
            if phrase == .wake { wake(); return }
            guard lifecycle.awake else { return }
            switch phrase {
            case .stop: stopTask()
            case .goodbye: goodbye()
            case .finishDictation: finishDictation()
            case .approve: if !dictating { respond(nil, approved: true, source: .speech) }
            case .reject: if !dictating { respond(nil, approved: false, source: .speech) }
            default: break
            }
        }
    }
    func submitText(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        if let phrase = PhraseMatcher.match(trimmed) {
            switch phrase {
            case .wake: wake(); return
            case .stop: stopTask(); return
            case .goodbye: goodbye(); return
            case .finishDictation: finishDictation(); return
            case .approve: respond(nil, approved: true, source: .typed); return
            case .reject: respond(nil, approved: false, source: .typed); return
            }
        }
        guard lifecycle.awake && voiceReady else { issue = "Wake or reconnect Orbit, then send your message when connected."; return }
        if !dictating {
            let positions = clearPlayback()
            voiceSocket.send(["type": "interrupt", "positions": positions])
        }
        voiceSocket.send(["type": "text", "text": trimmed]); transcript = trimmed
    }
    func respond(_ confirmation: Confirmation?, approved: Bool, source: ApprovalSource) {
        guard ApprovalPolicy.allows(source: source, identity: confirmation?.identity, current: confirmations.map(\.identity)), let confirmation else {
            issue = "Use the current Approve or Reject buttons. Spoken and plain typed yes/no cannot authorize an action."
            return
        }
        if previewMode {
            confirmations.removeAll { $0.identity == confirmation.identity }; status = approved ? "Preview approved" : "Preview rejected"; return
        }
        guard connected, guardrail.isTaskActive(confirmation.taskID), !respondingActions.contains(confirmation.actionID) else { return }
        respondingActions.insert(confirmation.actionID)
        let expectedEpoch = epoch
        Task {
            defer { if epoch == expectedEpoch { respondingActions.remove(confirmation.actionID) } }
            var restoredPID: Int32?
            if approved {
                guard let pid = await restoreActionFocus?(confirmation) else {
                    if epoch == expectedEpoch { issue = "The original application could not be restored. Return to it and review this action again." }
                    return
                }
                restoredPID = pid
            }
            // Focus restoration yields; a task may be cancelled or a new exact action
            // may replace this one before its application becomes frontmost.
            guard epoch == expectedEpoch, connected, guardrail.isTaskActive(confirmation.taskID),
                  ApprovalPolicy.allows(source: source, identity: confirmation.identity, current: confirmations.map(\.identity)) else { return }
            if let restoredPID, !bridge.bindApprovedFocus(actionID: confirmation.actionID, taskID: confirmation.taskID, processID: restoredPID) {
                issue = "This action was already bound to a different application. Request a new action."; return
            }
            taskSocket.send(["type": "confirmation.response", "task_id": confirmation.taskID, "action_id": confirmation.actionID, "version": confirmation.version, "approved": approved])
            confirmations.removeAll { $0.identity == confirmation.identity }
            status = approved ? "Working" : "Declined"
        }
    }
    func finishDictation() {
        guard dictating, !finishingDictation else { return }
        finishingDictation = true; audio.setStreaming(false); status = "Finishing dictation"
        voiceSocket.send(["type": "dictation.stop"])
        // Keep the bound target active until the service drains final chunks and acknowledges
        // the mode change. Stop/goodbye use immediate revocation instead.
    }
    private func taskFrame(_ frame: [String: Any]) {
        guard lifecycle.awake, sessionID != nil else { return }
        switch frame["type"] as? String {
        case "connected": connected = true; connectVoice()
        case "followup.snapshot":
            followUps.removeAll()
            for rule in frame["rules"] as? [[String: Any]] ?? [] { updateFollowUp(rule) }
            for delivery in frame["due"] as? [[String: Any]] ?? [] { updateFollowUp(delivery, due: true) }
        case "followup.changed": if let rule = frame["rule"] as? [String: Any] { updateFollowUp(rule) }
        case "followup.due": if let delivery = frame["delivery"] as? [String: Any] { updateFollowUp(delivery, due: true) }
        case "task.event": if let task = frame["task"] as? [String: Any] { updateTask(task) }
        case "device.command": enqueue(frame)
        case "device.cancel":
            if let id = frame["task_id"] as? String {
                guardrail.revoke(taskID: id); activeTasks.removeValue(forKey: id); confirmations.removeAll { $0.taskID == id }
                cancelNativeWork(taskIDs: [id])
            }
        case "confirmation":
            guard let task = frame["task_id"] as? String, let action = frame["action_id"] as? String, let version = frame["version"] as? Int,
                  let summary = frame["summary"] as? String, guardrail.isTaskActive(task) else { return }
            confirmations.removeAll { $0.taskID == task }
            confirmations.append(Confirmation(taskID: task, actionID: action, version: version, summary: summary)); status = "Your approval"
        case "session.closed": connectionFailed("Session closed by the task service.")
        default: break
        }
    }
    private func voiceFrame(_ frame: [String: Any]) {
        guard lifecycle.awake else { return }
        switch frame["type"] as? String {
        case "ready": voiceReady = true; stopping = false; applyListeningStatus(); audio.setStreaming(lifecycle.streamsMicrophone)
        case "state":
            if frame["state"] as? String == "listening", finishingDictation {
                if let target = dictationTarget { bridge.cancel(taskIDs: [target.task]) }
                dictating = false; finishingDictation = false; dictationTarget = nil
                audio.setStreaming(lifecycle.streamsMicrophone)
            }
            if let value = frame["state"] as? String, !dictating {
                if value == "listening" { applyListeningStatus() } else { status = value.capitalized }
            }
        case "audio.meta": if let id = frame["item_id"] as? String { voiceItem = (id, frame["content_index"] as? Int ?? 0) }
        case "audio.clear":
            let positions = clearPlayback() // hardware is silent before any socket send
            if let interruption = frame["interruption_id"] as? String, let generation = frame["generation"] as? Int {
                voiceSocket.send(["type": "audio.cleared", "interruption_id": interruption,
                                  "generation": generation, "positions": positions])
            }
        case "transcript": if let text = frame["text"] as? String { transcript = text }
        case "task.started": if let task = frame["task"] as? [String: Any] { updateTask(task) }
        case "dictation.final": enqueue(frame)
        case "control":
            if frame["command"] as? String == "goodbye" { goodbye() }
            else if frame["command"] as? String == "stop" { stopTask() }
        case "error": connectionFailed(frame["message"] as? String ?? "Voice service error.")
        default: break
        }
    }
    private func updateFollowUp(_ value: [String: Any], due: Bool = false) {
        guard let id = value[due ? "rule_id" : "id"] as? String, let version = value["version"] as? Int else { return }
        followUps.removeAll { $0.id == id }
        if due, let text = value["text"] as? String, let delivery = value["delivery_id"] as? String {
            followUps.append(FollowUpCard(id: id, version: version, text: text, detail: "Scheduled follow-up", deliveryID: delivery))
        } else if value["status"] as? String == "proposed", let spec = value["spec"] as? [String: Any], let text = spec["text"] as? String {
            let detail = "\(spec["due_at"] as? String ?? "") · \(spec["timezone"] as? String ?? "") · \(spec["repeat"] as? String ?? "once")"
            followUps.append(FollowUpCard(id: id, version: version, text: text, detail: detail, deliveryID: nil))
        }
    }
    func respondFollowUp(_ card: FollowUpCard, accept: Bool) {
        guard let api, let sid = sessionID, followUps.contains(where: { $0.id == card.id && $0.version == card.version }) else { return }
        let expectedEpoch = epoch
        Task {
            do {
                if let delivery = card.deliveryID, accept {
                    _ = try await api.request("v1/sessions/\(sid)/followup-receipts", body: ["delivery_id": delivery])
                } else if accept {
                    _ = try await api.request("v1/sessions/\(sid)/followups/\(card.id)/confirm", body: ["version": card.version])
                } else {
                    _ = try await api.request("v1/sessions/\(sid)/followups/\(card.id)", method: "DELETE")
                }
                if epoch == expectedEpoch { followUps.removeAll { $0.id == card.id && $0.version == card.version } }
            } catch { if epoch == expectedEpoch { issue = "Follow-up changed or could not be saved. Reconnect to review it." } }
        }
    }

    private func updateTask(_ task: [String: Any]) {
        guard task["session_id"] as? String == sessionID, let id = task["task_id"] as? String, let state = task["status"] as? String else { return }
        if ["queued", "running", "needs_confirmation"].contains(state) {
            guard let rawKind = task["kind"] as? String, let kind = TaskKind(rawValue: rawKind), let activeStatus = ActiveTaskStatus(rawValue: state),
                  let version = task["version"] as? Int,
                  guardrail.register(taskID: id, kind: kind, status: activeStatus, version: version) else { return }
            activeTasks[id] = task["progress"] as? String ?? task["goal"] as? String ?? state
            if let result = task["result"] as? [String: Any], result["mode"] as? String == "dictation_ready", let note = result["note_id"] as? String,
               dictationTarget?.task != id, bridge.noteID == note, voiceReady, guardrail.isTaskActive(id) {
                dictating = true; dictationTarget = (id, note); status = "Dictating"
                let positions = clearPlayback()
                voiceSocket.send(["type": "dictation.start", "task_id": id, "note_id": note, "positions": positions])
            }
        } else {
            let wasActive = guardrail.isTaskActive(id)
            guardrail.revoke(taskID: id); activeTasks.removeValue(forKey: id); confirmations.removeAll { $0.taskID == id }
            cancelNativeWork(taskIDs: [id])
            if state == "failed" { issue = task["error"] as? String ?? "Task failed." }
            if state == "completed", wasActive, let result = task["result"] as? [String: Any], let artifacts = result["artifacts"] as? [[String: Any]] {
                for artifact in artifacts { openArtifact(artifact) }
            }
        }
    }
    private func openArtifact(_ artifact: [String: Any]) {
        guard let id = artifact["artifact_id"] as? String, let name = artifact["filename"] as? String, let api, !openedArtifacts.contains(id) else { return }
        openedArtifacts.insert(id); let expectedEpoch = epoch
        Task {
            do { let file = try await api.artifact(id: id, filename: name); if epoch == expectedEpoch, lifecycle.awake { NSWorkspace.shared.open(file) } }
            catch { if epoch == expectedEpoch { issue = error.localizedDescription } }
        }
    }
    private func enqueue(_ frame: [String: Any]) {
        guard workQueue.count < 32 else { connectionFailed("Too many pending native actions."); return }
        workQueue.append(frame)
        guard workRunner == nil else { return }
        let expectedEpoch = epoch
        workRunner = Task {
            while !workQueue.isEmpty, epoch == expectedEpoch, !Task.isCancelled {
                let next = workQueue.removeFirst()
                if next["type"] as? String == "dictation.final" { await dictate(next) }
                else { await execute(next) }
            }
            if epoch == expectedEpoch { workRunner = nil }
        }
    }
    private func execute(_ frame: [String: Any]) async {
        guard let session = frame["session_id"] as? String, let task = frame["task_id"] as? String, let action = frame["action_id"] as? String,
              let fence = frame["fence"] as? Int, let expires = frame["expires_at"] as? Double, let payload = frame["action"] as? [String: Any] else { return }
        let command = Command(sessionID: session, taskID: task, actionID: action, fence: fence, expiresAt: expires)
        var result: [String: Any] = ["type": "device.result", "task_id": task, "action_id": action, "fence": fence]
        switch guardrail.check(command) {
        case .reject: result["ok"] = false; result["error"] = "Native authority rejected stale, duplicate in-flight, cancelled or unknown command."
        case .duplicate(let data): if let cached = try? JSONSerialization.jsonObject(with: data) as? [String: Any] { taskSocket.send(cached) }; return
        case .execute:
            do {
                let value = try await bridge.execute(payload, command: command, approved: frame["approved"] as? Bool ?? false)
                try bridge.requireValid(command)
                result["result"] = value; result["ok"] = true
            }
            catch { result["ok"] = false; result["error"] = error.localizedDescription; issue = error.localizedDescription }
        }
        result["result"] = result["result"] ?? [:]
        if let data = try? JSONSerialization.data(withJSONObject: result) { guardrail.complete(command, result: data) }
        if sessionID == session { taskSocket.send(result) }
    }
    private func dictate(_ frame: [String: Any]) async {
        guard let chunk = frame["chunk_id"] as? String, let task = frame["task_id"] as? String, let note = frame["note_id"] as? String, let text = frame["text"] as? String else { return }
        guard guardrail.isTaskActive(task) else { return }
        let expectedEpoch = epoch
        var response: [String: Any] = ["type": "dictation.result", "chunk_id": chunk]
        do {
            guard dictating, dictationTarget?.task == task, dictationTarget?.note == note else { throw OrbitError("Dictation is no longer active.") }
            try await bridge.insert(note: note, text: text, chunk: chunk, taskID: task); response["ok"] = true
        } catch {
            guard epoch == expectedEpoch, guardrail.isTaskActive(task) else { return }
            response["ok"] = false; response["error"] = error.localizedDescription; issue = error.localizedDescription; finishDictation()
        }
        guard epoch == expectedEpoch, guardrail.isTaskActive(task) else { return }
        voiceSocket.send(response)
    }
    private func connectionFailed(_ message: String) {
        disconnect(); stopping = false; issue = message; status = "Reconnect"; if lifecycle.awake { revealFaceExplicitly() }
    }
    func requestAccessibility() {
        guard !previewMode else { return }
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(options); permissionsChanged?()
    }
    func requestScreenCapture() { guard !previewMode else { return }; _ = CGRequestScreenCaptureAccess() }
    func requestInputMonitoring() { guard !previewMode else { return }; _ = CGRequestListenEventAccess(); permissionsChanged?() }
    func shutdown() { goodbye(); if !previewMode { audio.stopCapture() } }
}
