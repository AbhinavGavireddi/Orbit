import Foundation

public enum LocalControlPolicy {
    public static func match(_ text: String, final: Bool, awake: Bool) -> LocalPhrase? {
        guard final, let phrase = PhraseMatcher.match(text), awake || phrase == .wake else { return nil }
        return phrase
    }
}
public enum AudioEngineRecoveryPolicy {
    public static func shouldRecover(intendedCapture: Bool, engineRunning: Bool) -> Bool {
        intendedCapture && !engineRunning
    }
}

public enum CaptureDownmixPolicy {
    /// AVAudioConverter downmixes mono and stereo. A mic array (this Mac's default
    /// input is 9 channels) converts to a full buffer of silence, so speech never
    /// reaches local wake or the cloud voice socket. Chat does not use this path.
    public static func useLoudestChannel(channelCount: Int) -> Bool {
        channelCount > 2
    }

    public static func loudestChannel(_ energies: [Double]) -> Int {
        energies.enumerated().max { $0.element < $1.element }?.offset ?? 0
    }
}

public enum LocalSpeechLifecyclePolicy {
    public enum Decision: Equatable {
        case ignore
        case use(String)
        case restart
    }

    public static func decide(text: String, failed: Bool) -> Decision {
        if failed { return .restart }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? .ignore : .use(trimmed)
    }
}

public struct LocalSpeechRestartBudget {
    private var recent: [TimeInterval] = []
    public init() {}

    public mutating func allow(now: TimeInterval, window: TimeInterval = 10, limit: Int = 3) -> Bool {
        recent = recent.filter { now - $0 < window }
        guard recent.count < limit else { return false }
        recent.append(now)
        return true
    }
}

public struct AudioRouteRecovery {
    private var pendingDeadline: TimeInterval?
    public init() {}

    public mutating func noteChange(now: TimeInterval, settle: TimeInterval = 0.35) -> TimeInterval {
        let deadline = now + settle
        pendingDeadline = deadline
        return deadline
    }

    public mutating func shouldFire(now: TimeInterval, intendedCapture: Bool, engineRunning: Bool) -> Bool {
        guard let deadline = pendingDeadline, now >= deadline else { return false }
        pendingDeadline = nil
        return intendedCapture && !engineRunning
    }
}

public struct AudioLifecycleGate {
    public private(set) var playbackGeneration = 0
    public private(set) var streamGeneration = 0
    public private(set) var sessionGeneration = 0
    public private(set) var streaming = false
    private var captureGeneration = 0
    public private(set) var capturing = false
    public init() {}
    public mutating func beginCapture() -> Int? {
        guard !capturing else { return nil }
        captureGeneration += 1; capturing = true; return captureGeneration
    }
    public func recoveryGeneration() -> Int? { capturing ? captureGeneration : nil }
    public mutating func stopCapture() { captureGeneration += 1; capturing = false; setStreaming(false) }
    public func acceptsCapture(_ generation: Int) -> Bool { capturing && generation == captureGeneration }
    public mutating func clearPlayback() { playbackGeneration += 1 }
    public func acceptsPlayback(_ generation: Int) -> Bool { generation == playbackGeneration }
    public mutating func setStreaming(_ value: Bool) {
        guard streaming != value else { return }
        streaming = value; streamGeneration += 1
    }
    public mutating func resetSession() { sessionGeneration += 1; setStreaming(false); clearPlayback() }
    public func acceptsSession(_ generation: Int) -> Bool { generation == sessionGeneration }
    public func acceptsStream(capture: Int, stream: Int, session: Int) -> Bool {
        streaming && acceptsCapture(capture) && stream == streamGeneration && acceptsSession(session)
    }
}
public struct PlaybackAdmission {
    private var blocked = Set<String>()
    public init() {}
    public func accepts(item: String) -> Bool { !blocked.contains(item) }
    public mutating func block(items: [String]) { blocked.formUnion(items) }
    public mutating func reset() { blocked.removeAll() }
}
public struct SpeechInterruptionGate {
    private var interrupted = false
    public init() {}
    public mutating func observe(speech: Bool, hasPlayback: Bool) -> Bool {
        if !speech { interrupted = false; return false }
        guard hasPlayback, !interrupted else { return false }
        interrupted = true; return true
    }
}
public enum FocusRestorationDecision: Equatable { case ready, restore(Int32), reject }
public enum FocusRestorationPolicy {
    public static func decision(orbitPID: Int32, currentPID: Int32?, originalPID: Int32?) -> FocusRestorationDecision {
        guard let currentPID, let originalPID, originalPID > 0, originalPID != orbitPID else { return .reject }
        if currentPID == originalPID { return .ready }
        return currentPID == orbitPID ? .restore(originalPID) : .reject
    }
}
public enum PreviewScenario: String, CaseIterable {
    case listening, speaking, working, error, approval
    public static func parse(arguments: [String]) -> PreviewScenario? {
        for (index, argument) in arguments.enumerated() {
            if argument == "--preview" {
                return arguments.indices.contains(index + 1) ? PreviewScenario(rawValue: arguments[index + 1]) ?? .listening : .listening
            }
            if argument.hasPrefix("--preview=") { return PreviewScenario(rawValue: String(argument.dropFirst(10))) ?? .listening }
        }
        return nil
    }
}
public enum PanelPresentation {
    /// Approvals always stay visible. Issues/tasks may auto-open via `requested`, but the chevron can collapse them.
    public static func isExpanded(requested: Bool, hasApproval: Bool, hasIssue: Bool = false, hasTasks: Bool = false) -> Bool {
        _ = hasIssue; _ = hasTasks
        return requested || hasApproval
    }
}
public struct ApprovedActionFocus {
    private struct Binding { let taskID: String; let processID: Int32 }
    private var bindings: [String: Binding] = [:]
    public init() {}
    @discardableResult public mutating func bind(actionID: String, taskID: String, processID: Int32) -> Bool {
        guard processID > 0 else { return false }
        if let existing = bindings[actionID] { return existing.taskID == taskID && existing.processID == processID }
        bindings[actionID] = Binding(taskID: taskID, processID: processID)
        return true
    }
    public func allows(actionID: String, currentPID: Int32?) -> Bool {
        guard let binding = bindings[actionID], let currentPID else { return false }
        return binding.processID == currentPID
    }
    public mutating func revoke(taskIDs: Set<String>) { bindings = bindings.filter { !taskIDs.contains($0.value.taskID) } }
}
public enum ApplicationLaunchPolicy {
    public static func accepts(requested: String, actual: String?, authorityValid: Bool) -> Bool {
        authorityValid && actual == requested
    }
}

public struct PermissionSnapshot: Equatable {
    public let microphoneAuthorized: Bool
    public let speechAuthorized: Bool
    public init(microphoneAuthorized: Bool, speechAuthorized: Bool) {
        self.microphoneAuthorized = microphoneAuthorized
        self.speechAuthorized = speechAuthorized
    }
}

public final class PermissionSnapshotCache {
    private let reader: () -> PermissionSnapshot
    private var snapshot: PermissionSnapshot

    public init(reader: @escaping () -> PermissionSnapshot) {
        self.reader = reader
        snapshot = reader()
    }

    public var current: PermissionSnapshot { snapshot }

    public func refresh() {
        snapshot = reader()
    }
}

/// Glass status for capture. Never show "Listening" unless mic+speech are
/// authorized, capture is ready, and a recent input meter peak proves audio.
public enum ListeningStatusPolicy {
    public static let listening = "Listening"
    public static let needsMicrophone = "Needs microphone"
    public static let needsSpeech = "Needs speech"
    /// Authorized, but local capture / assets are still coming up.
    public static let preparingSpeech = "Preparing speech"
    /// Capture is live; meter is quiet (Design: quiet awake — not Listening, not a permission fail).
    public static let ready = "Awake"
    /// RMS peak that counts as "meter moving" (quiet room still above digital silence).
    public static let liveMeterFloor: Float = 0.002

    public static func label(
        microphoneAuthorized: Bool,
        speechAuthorized: Bool,
        captureReady: Bool,
        recentMeterPeak: Float
    ) -> String {
        if !microphoneAuthorized { return needsMicrophone }
        if !speechAuthorized { return needsSpeech }
        if !captureReady { return preparingSpeech }
        if recentMeterPeak < liveMeterFloor { return ready }
        return listening
    }

    public static func isPermissionState(_ status: String) -> Bool {
        status == needsMicrophone || status == needsSpeech
    }

    public static func shouldReveal(status: String, explicitlyAwake: Bool) -> Bool {
        if isPermissionState(status) { return true }
        guard explicitlyAwake else { return false }
        return status == preparingSpeech || status == ready || status == listening
    }
}

public struct PolicyVisibilityGate {
    private var policyRevealed: Bool?

    public init() {}

    /// Emits visibility only when policy reveal state changes.
    public mutating func observe(shouldReveal: Bool) -> Bool? {
        if let policyRevealed {
            guard policyRevealed != shouldReveal else { return nil }
        } else if !shouldReveal {
            self.policyRevealed = false
            return nil
        }
        policyRevealed = shouldReveal
        return shouldReveal
    }

    /// Align policy tracking after explicit face show/hide outside policy updates.
    public mutating func sync(revealed: Bool) {
        policyRevealed = revealed
    }
}
