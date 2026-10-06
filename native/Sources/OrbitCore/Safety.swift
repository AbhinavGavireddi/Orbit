import Foundation

public enum LocalPhrase: Equatable { case wake, stop, goodbye, finishDictation, approve, reject }
public enum PhraseMatcher {
    public static func match(_ text: String) -> LocalPhrase? {
        let normalized = text.lowercased().unicodeScalars.map { CharacterSet.alphanumerics.contains($0) ? String($0) : " " }.joined()
            .split(whereSeparator: \.isWhitespace).joined(separator: " ")
        switch normalized {
        case "orbit wake up", "wake up orbit": return .wake
        case "stop", "orbit stop", "stop orbit", "stop task": return .stop
        case "goodbye", "goodbye orbit", "orbit goodbye": return .goodbye
        case "finish dictation", "orbit finish dictation", "stop dictation": return .finishDictation
        case "yes", "approve", "yes approve": return .approve
        case "no", "reject", "no reject": return .reject
        default: return nil
        }
    }
}
public struct Lifecycle {
    public private(set) var awake = false
    public private(set) var muted = false
    public private(set) var suspended = false
    public var capturesMicrophone: Bool { !muted && !suspended }
    public var streamsMicrophone: Bool { awake && capturesMicrophone }
    public init() {}
    public mutating func wake() { if !suspended { awake = true } }
    public mutating func goodbye() { awake = false }
    public mutating func setMuted(_ value: Bool) { muted = value }
    public mutating func suspend() { suspended = true; awake = false }
    public mutating func resumeLocal() { suspended = false }
}
public struct Command: Equatable {
    public let sessionID: String, taskID: String, actionID: String
    public let fence: Int
    public let expiresAt: Double
    public init(sessionID: String, taskID: String, actionID: String, fence: Int, expiresAt: Double) {
        self.sessionID = sessionID; self.taskID = taskID; self.actionID = actionID
        self.fence = fence; self.expiresAt = expiresAt
    }
}
public enum GuardVerdict: Equatable { case execute, duplicate(Data), reject }
public enum TaskKind: String { case goal, automation, dictation, research }
public enum ActiveTaskStatus: String { case queued, running; case needsConfirmation = "needs_confirmation" }
public final class CommandGuard {
    private struct RegisteredTask {
        let kind: TaskKind, status: ActiveTaskStatus, version: Int
        var controlsDesktop: Bool { kind == .goal || kind == .automation || kind == .dictation }
    }
    private let lock = NSLock()
    private var session: String?
    private var tasks: [String: RegisteredTask] = [:]
    private var revoked = Set<String>()
    private var fence = -1
    private var seen = [String: Command]()
    private var results = [String: Data]()
    public init() {}
    public func activate(sessionID: String) {
        lock.lock(); defer { lock.unlock() }
        session = sessionID; tasks.removeAll(); revoked.removeAll(); seen.removeAll(); results.removeAll(); fence = -1
    }
    @discardableResult public func register(taskID: String, kind: TaskKind = .automation, status: ActiveTaskStatus = .running, version: Int = 0) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard session != nil, !revoked.contains(taskID), version >= 0 else { return false }
        if let existing = tasks[taskID] {
            guard existing.kind == kind, version >= existing.version else { return false }
            if version == existing.version { return existing.status == status }
        }
        tasks[taskID] = RegisteredTask(kind: kind, status: status, version: version); return true
    }
    public var desktopTaskIDs: Set<String> {
        lock.lock(); defer { lock.unlock() }
        return Set(tasks.filter { $0.value.controlsDesktop }.keys)
    }
    public var runnableDesktopTaskIDs: Set<String> {
        lock.lock(); defer { lock.unlock() }
        return Set(tasks.filter { $0.value.controlsDesktop && $0.value.status != .queued }.keys)
    }
    public func revokeDesktopTasks() -> Set<String> {
        lock.lock(); defer { lock.unlock() }
        let ids = Set(tasks.filter { $0.value.controlsDesktop }.keys)
        for id in ids { tasks.removeValue(forKey: id) }
        revoked.formUnion(ids)
        return ids
    }
    public func revoke(taskID: String) {
        lock.lock(); defer { lock.unlock() }
        tasks.removeValue(forKey: taskID); revoked.insert(taskID)
    }
    public func revokeAll() {
        lock.lock(); defer { lock.unlock() }
        session = nil; revoked.formUnion(tasks.keys); tasks.removeAll()
    }
    public func isTaskActive(_ taskID: String) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return session != nil && tasks[taskID] != nil && !revoked.contains(taskID)
    }
    public func isValid(_ command: Command, now: Double = Date().timeIntervalSince1970) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return valid(command, now: now)
    }
    private func valid(_ command: Command, now: Double) -> Bool {
        session == command.sessionID && tasks[command.taskID] != nil && !revoked.contains(command.taskID)
            && command.expiresAt.isFinite && command.expiresAt > now && command.fence >= fence && command.fence >= 0
    }
    public func check(_ command: Command, now: Double = Date().timeIntervalSince1970) -> GuardVerdict {
        lock.lock(); defer { lock.unlock() }
        guard valid(command, now: now) else { return .reject }
        if let previous = seen[command.actionID] {
            guard previous == command, let result = results[command.actionID] else { return .reject }
            return .duplicate(result)
        }
        fence = command.fence; seen[command.actionID] = command
        return .execute
    }
    public func complete(_ command: Command, result: Data) {
        lock.lock(); defer { lock.unlock() }
        guard session == command.sessionID, seen[command.actionID] == command else { return }
        results[command.actionID] = result
    }
}
public enum ActionPolicy {
    public static func number(_ value: Any?) -> Double? {
        guard let value = value as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID() else { return nil }
        return value.doubleValue
    }
    public static func validateComputer(actions: [[String: Any]], approved: Bool) -> Bool {
        guard !actions.isEmpty, actions.count <= 8 else { return false }
        let supported: Set<String> = ["click", "double_click", "move", "drag", "scroll", "keypress", "type", "wait", "screenshot"]
        let readOnly: Set<String> = ["move", "wait", "screenshot"]
        return actions.allSatisfy { action in
            guard let type = action["type"] as? String, supported.contains(type), approved || readOnly.contains(type) else { return false }
            if type == "wait" {
                let seconds = action["seconds"] == nil ? 0 : number(action["seconds"]) ?? .nan
                return seconds.isFinite && seconds >= 0 && seconds <= 2
            }
            if type == "type" { return (action["text"] as? String)?.utf16.count ?? 10001 <= 10000 }
            return true
        }
    }
    public static func spotifyURL(_ text: String) -> URL? {
        guard let url = URL(string: text), url.scheme?.lowercased() == "https", url.host?.lowercased() == "open.spotify.com",
              url.user == nil, url.password == nil, url.port == nil || url.port == 443 else { return nil }
        return url
    }
    public static func artifactFilename(_ text: String) -> String? {
        guard !text.isEmpty, text.utf8.count <= 180, text != ".", text != "..", !text.contains("/"), !text.contains("\\"),
              !text.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }),
              ["pdf", "md", "txt"].contains((text as NSString).pathExtension.lowercased()) else { return nil }
        return text
    }
}
public struct PixelMapping {
    public let width: Double, height: Double, originX: Double, originY: Double, pointWidth: Double, pointHeight: Double
    public init(width: Double, height: Double, originX: Double, originY: Double, pointWidth: Double, pointHeight: Double) {
        self.width = width; self.height = height; self.originX = originX; self.originY = originY
        self.pointWidth = pointWidth; self.pointHeight = pointHeight
    }
    public func point(x: Double, y: Double) -> (Double, Double)? {
        guard width > 0, height > 0, pointWidth > 0, pointHeight > 0, x.isFinite, y.isFinite, x >= 0, y >= 0, x < width, y < height else { return nil }
        return (originX + x / width * pointWidth, originY + y / height * pointHeight)
    }
}
