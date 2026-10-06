import Foundation
import CoreGraphics

public struct ApprovalIdentity: Equatable {
    public let taskID: String, actionID: String
    public let version: Int
    public init(taskID: String, actionID: String, version: Int) {
        self.taskID = taskID; self.actionID = actionID; self.version = version
    }
}
public enum ApprovalSource { case speech, typed, button }
public enum ApprovalPolicy {
    public static func allows(source: ApprovalSource, identity: ApprovalIdentity?, current: [ApprovalIdentity]) -> Bool {
        guard case .button = source, let identity else { return false }
        return current.contains(identity)
    }
}
public enum TakeoverPolicy {
    /// These actions use NSWorkspace or Notes AppleScript. They do not post HID events, so they do not need the listen tap.
    public static let hidFreeTypes: Set<String> = [
        "screenshot", "open_artifact", "open_app", "open_url", "notes_create",
        "dictation_start", "dictation_insert", "dictation_stop", "ax_snapshot", "ax_perform"
    ]
    /// Inner computer steps that only observe. A click, drag, scroll, or key still needs the listen tap.
    public static let passiveComputerSteps: Set<String> = ["screenshot", "wait"]
    public static func requiresListenAccess(type: String, actions: [[String: Any]] = []) -> Bool {
        if hidFreeTypes.contains(type) { return false }
        guard type == "computer" else { return true }
        guard !actions.isEmpty else { return true }
        return actions.contains { step in
            guard let inner = step["type"] as? String else { return true }
            return !passiveComputerSteps.contains(inner)
        }
    }
    public static let events: [CGEventType] = [
        .keyDown, .keyUp, .flagsChanged,
        .leftMouseDown, .leftMouseUp, .leftMouseDragged,
        .rightMouseDown, .rightMouseUp, .rightMouseDragged,
        .otherMouseDown, .otherMouseUp, .otherMouseDragged,
        .mouseMoved, .scrollWheel
    ]
    public static var eventMask: CGEventMask { events.reduce(0) { $0 | (CGEventMask(1) << $1.rawValue) } }
    public static func shouldRevoke(type: CGEventType, desktopTasks: Set<String>, runnableDesktopTasks: Set<String>? = nil, awaitingApproval: Set<String> = [], synthetic: Bool = false, ownMenuOpen: Bool = false, orbitOwnsKeyboard: Bool = false, pointerInsideOrbit: Bool = false) -> Bool {
        guard !desktopTasks.isEmpty, !synthetic, !ownMenuOpen, events.contains(type) else { return false }
        if [.keyDown, .keyUp, .flagsChanged].contains(type) {
            // Keyboard event.location is the cursor position, not the receiving application.
            return !orbitOwnsKeyboard
        }
        if pointerInsideOrbit { return false }
        // Let the user reach a paused task's approval controls. All other physical input
        // still takes over, and a second runnable desktop task removes this exemption.
        let runnable = (runnableDesktopTasks ?? desktopTasks).intersection(desktopTasks)
        if type == .mouseMoved && runnable.isSubset(of: awaitingApproval) { return false }
        return true
    }
}
