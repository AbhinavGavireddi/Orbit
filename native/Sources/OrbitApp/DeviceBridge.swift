import AppKit
import ApplicationServices
import ScreenCaptureKit
import OrbitCore

struct OrbitError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

@MainActor final class DeviceBridge {
    static let syntheticMarker: Int64 = 0x4F52424954
    let guardrail: CommandGuard
    private(set) var noteID: String?
    private var createdNotes = Set<String>()
    private var chunks = Set<String>()
    private var scriptProcess: Process?
    private var scriptTaskID: String?
    private var boundTaskID: String?
    private var mapping: PixelMapping?
    private var captureDisplay: CGDirectDisplayID?
    private var approvedFocus = ApprovedActionFocus()
    var downloadArtifact: ((String, String) async throws -> URL)?
    var takeoverAvailable = false
    /// Recreate the listen tap and report whether it is running. Set by the app delegate.
    var refreshTakeover: (() -> Bool)?
    /// Ask macOS for Input Monitoring once. Set by the app delegate.
    var requestTakeover: (() -> Void)?

    init(guardrail: CommandGuard) { self.guardrail = guardrail }
    func bindApprovedFocus(actionID: String, taskID: String, processID: Int32) -> Bool {
        approvedFocus.bind(actionID: actionID, taskID: taskID, processID: processID)
    }
    func cancel() {
        scriptProcess?.terminate(); scriptProcess = nil; scriptTaskID = nil
        noteID = nil; boundTaskID = nil; chunks.removeAll(); approvedFocus = ApprovedActionFocus()
    }
    func cancel(taskIDs: Set<String>) {
        approvedFocus.revoke(taskIDs: taskIDs)
        if let id = scriptTaskID, taskIDs.contains(id) {
            scriptProcess?.terminate(); scriptProcess = nil; scriptTaskID = nil
        }
        if let id = boundTaskID, taskIDs.contains(id) {
            noteID = nil; boundTaskID = nil; chunks.removeAll()
        }
    }
    func reset() { cancel(); createdNotes.removeAll(); mapping = nil; captureDisplay = nil }
    func requireValid(_ command: Command) throws {
        guard guardrail.isValid(command) else { throw OrbitError("Action cancelled, expired or superseded.") }
    }
    private func ensureTakeover() async throws {
        if refreshTakeover?() ?? takeoverAvailable { return }
        requestTakeover?()
        let deadline = Date().addingTimeInterval(8)
        while Date() < deadline {
            try await Task.sleep(nanoseconds: 250_000_000)
            if refreshTakeover?() ?? takeoverAvailable { return }
        }
        throw OrbitError("Enable Input Monitoring for Orbit in System Settings. Orbit checks again as soon as that permission is on.")
    }
    func execute(_ action: [String: Any], command: Command, approved: Bool) async throws -> [String: Any] {
        try requireValid(command)
        guard let type = action["type"] as? String else { throw OrbitError("Missing action type.") }
        let params = action["params"] as? [String: Any] ?? [:]
        if TakeoverPolicy.requiresListenAccess(type: type, actions: params["actions"] as? [[String: Any]] ?? []) {
            try await ensureTakeover()
        }
        switch type {
        case "screenshot": return try await screenshot(command: command)
        case "open_app":
            let bundle = try string(params, "bundle_id")
            guard ["com.apple.Notes", "com.google.Chrome"].contains(bundle),
                  let app = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundle) else { throw OrbitError("Application is unavailable or not allowed.") }
            let configuration = NSWorkspace.OpenConfiguration(); configuration.activates = true
            let opened = try await NSWorkspace.shared.openApplication(at: app, configuration: configuration)
            try requireValid(command)
            guard ApplicationLaunchPolicy.accepts(requested: bundle, actual: opened.bundleIdentifier, authorityValid: guardrail.isValid(command)) else {
                throw OrbitError("macOS did not confirm the requested application launch.")
            }
            return ["app_bundle_id": opened.bundleIdentifier!, "ok": true]
        case "open_url":
            guard let url = ActionPolicy.spotifyURL(try string(params, "url")),
                  let chrome = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.google.Chrome") else { throw OrbitError("Only HTTPS open.spotify.com URLs in installed Chrome are allowed.") }
            let opened = try await NSWorkspace.shared.open([url], withApplicationAt: chrome, configuration: .init())
            try requireValid(command)
            guard ApplicationLaunchPolicy.accepts(requested: "com.google.Chrome", actual: opened.bundleIdentifier, authorityValid: guardrail.isValid(command)) else {
                throw OrbitError("macOS did not confirm navigation in Google Chrome.")
            }
            return ["app_bundle_id": opened.bundleIdentifier!, "ok": true]
        case "notes_create":
            let title = try string(params, "title")
            guard title.utf16.count <= 300 else { throw OrbitError("Note title is too long.") }
            let id = try await appleScript(Self.createNoteScript, arguments: ["<h1>" + Self.html(title) + "</h1><div><br></div>"], taskID: command.taskID)
            try requireValid(command)
            guard id.hasPrefix("x-coredata://") else { throw OrbitError("Notes did not return a valid note ID.") }
            createdNotes.insert(id)
            return ["note_id": id]
        case "dictation_start":
            let id = try string(params, "note_id")
            guard createdNotes.contains(id) else { throw OrbitError("Dictation target was not created in this session.") }
            try requireAccessibility()
            _ = try await appleScript(Self.showNoteScript, arguments: [id], taskID: command.taskID)
            try requireValid(command)
            noteID = id; boundTaskID = command.taskID; chunks.removeAll()
            return ["note_id": id]
        case "dictation_insert":
            try await insert(note: try string(params, "note_id"), text: try string(params, "text"), chunk: try string(params, "chunk_id"), taskID: command.taskID)
            return [:]
        case "dictation_stop": cancel(taskIDs: [command.taskID]); return [:]
        case "open_artifact":
            let id = try string(params, "artifact_id"), filename = try string(params, "filename")
            guard UUID(uuidString: id) != nil, ActionPolicy.artifactFilename(filename) != nil, let downloadArtifact else { throw OrbitError("Invalid artifact descriptor.") }
            let url = try await downloadArtifact(id, filename)
            try requireValid(command)
            NSWorkspace.shared.open(url)
            return ["opened": true]
        case "ax_snapshot":
            try requireAccessibility()
            return axSnapshot()
        case "ax_perform":
            try requireAccessibility()
            if (params["mutating"] as? Bool ?? true), !approved {
                throw OrbitError("A mutating control still needs the on-screen approval button.")
            }
            return try axPerform(params)
        case "computer":
            if let rawChecks = params["safety_checks"] {
                guard let checks = rawChecks as? [Any] else { throw OrbitError("Malformed provider safety checks.") }
                if !checks.isEmpty, !approved { throw OrbitError("Provider safety checks require explicit approval.") }
            }
            guard let actions = params["actions"] as? [[String: Any]], ActionPolicy.validateComputer(actions: actions, approved: approved) else { throw OrbitError("Computer action is unsupported, too large or lacks approval.") }
            try requireAccessibility()
            var result: [String: Any] = [:]
            for step in actions {
                try requireValid(command)
                if !["move", "wait", "screenshot"].contains(step["type"] as? String ?? "") { try requireApprovedFocus(command) }
                if let image = try await computer(step, command: command) { result = image }
            }
            return result
        default: throw OrbitError("Unsupported native action: \(type).")
        }
    }
    private func axSnapshot() -> [String: Any] {
        guard let app = NSWorkspace.shared.frontmostApplication else { return ["controls": []] }
        let bundle = app.bundleIdentifier ?? ""
        var controls: [[String: Any]] = []
        walk(AXUIElementCreateApplication(app.processIdentifier), depth: 0, path: [], appBundle: bundle, controls: &controls)
        return ["controls": controls, "app": bundle, "pid": Int(app.processIdentifier)]
    }
    private func walk(_ element: AXUIElement, depth: Int, path: [Int], appBundle: String, controls: inout [[String: Any]]) {
        if depth > 6 || controls.count >= 80 { return }
        let role = axString(element, kAXRoleAttribute)
        let title = axString(element, kAXTitleAttribute)
        let description = axString(element, kAXDescriptionAttribute)
        let interesting = ["AXButton", "AXTextField", "AXTextArea", "AXMenuItem", "AXCheckBox", "AXLink", "AXPopUpButton", "AXRadioButton"]
        if interesting.contains(role), (!title.isEmpty || !description.isEmpty) {
            let ref: [String: Any] = ["version": 1, "app_bundle_id": appBundle, "role": role,
                                      "title": title, "description": description, "child_path": path]
            controls.append(["id": String(controls.count), "role": role, "title": title,
                             "description": description, "ax_ref": ref])
        }
        var children: CFTypeRef?
        AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &children)
        for (index, child) in ((children as? [AXUIElement]) ?? []).prefix(40).enumerated() {
            walk(child, depth: depth + 1, path: path + [index], appBundle: appBundle, controls: &controls)
        }
    }
    private func axString(_ element: AXUIElement, _ attribute: String) -> String {
        var value: CFTypeRef?
        AXUIElementCopyAttributeValue(element, attribute as CFString, &value)
        return value as? String ?? ""
    }
    private func axPerform(_ params: [String: Any]) throws -> [String: Any] {
        let role = params["role"] as? String ?? ""
        let title = params["title"] as? String ?? ""
        let description = params["description"] as? String ?? ""
        let action = params["action"] as? String ?? "AXPress"
        guard let app = NSWorkspace.shared.frontmostApplication else { throw OrbitError("No frontmost application.") }
        let root = AXUIElementCreateApplication(app.processIdentifier)
        let element: AXUIElement
        if let ref = params["ax_ref"] as? [String: Any] { element = try resolve(ref, root: root, app: app) }
        else if let found = find(root, role: role, title: title, description: description, depth: 0) { element = found }
        else { throw OrbitError("Accessibility control is no longer on screen.") }
        if action == "AXSetValue" {
            let value = params["value"] as? String ?? ""
            guard AXUIElementSetAttributeValue(element, kAXValueAttribute as CFString, value as CFString) == .success else {
                throw OrbitError("Accessibility set-value failed.")
            }
            return ["ok": true]
        }
        guard action == "AXPress" else { throw OrbitError("Unknown accessibility action.") }
        guard AXUIElementPerformAction(element, kAXPressAction as CFString) == .success else {
            throw OrbitError("Accessibility action failed.")
        }
        return ["ok": true]
    }
    private func resolve(_ ref: [String: Any], root: AXUIElement, app: NSRunningApplication) throws -> AXUIElement {
        guard number(ref["version"]).map({ Int($0) }) == 1 else { throw OrbitError("Accessibility reference is unsupported.") }
        if let bundle = ref["app_bundle_id"] as? String, !bundle.isEmpty, bundle != app.bundleIdentifier {
            throw OrbitError("The approved application changed. Review a new action before continuing.")
        }
        var element = root
        for value in (ref["child_path"] as? [Any] ?? []) {
            guard let numeric = number(value) else { throw OrbitError("Accessibility reference is invalid.") }
            let raw = Int(numeric)
            guard raw >= 0 && raw < 40 else { throw OrbitError("Accessibility reference is invalid.") }
            var children: CFTypeRef?
            AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &children)
            let list = (children as? [AXUIElement]) ?? []
            guard raw < list.count else { throw OrbitError("Accessibility control moved. Review a new action before continuing.") }
            element = list[raw]
        }
        guard matches(element, ref: ref) else {
            throw OrbitError("Accessibility control changed. Review a new action before continuing.")
        }
        return element
    }
    private func number(_ value: Any?) -> Double? {
        if let value = value as? Double { return value }
        if let value = value as? Int { return Double(value) }
        if let value = value as? NSNumber { return value.doubleValue }
        return nil
    }
    private func matches(_ element: AXUIElement, ref: [String: Any]) -> Bool {
        let role = ref["role"] as? String ?? ""
        let title = ref["title"] as? String ?? ""
        let description = ref["description"] as? String ?? ""
        guard !role.isEmpty, (!title.isEmpty || !description.isEmpty), axString(element, kAXRoleAttribute) == role else { return false }
        if !title.isEmpty, axString(element, kAXTitleAttribute) != title { return false }
        if !description.isEmpty, axString(element, kAXDescriptionAttribute) != description { return false }
        return true
    }
    private func find(_ element: AXUIElement, role: String, title: String, description: String, depth: Int) -> AXUIElement? {
        if depth > 6 { return nil }
        if axString(element, kAXRoleAttribute) == role {
            let titleMatches = !title.isEmpty && axString(element, kAXTitleAttribute) == title
            let descriptionMatches = !description.isEmpty && axString(element, kAXDescriptionAttribute) == description
            if titleMatches || descriptionMatches { return element }
        }
        var children: CFTypeRef?
        AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &children)
        for child in ((children as? [AXUIElement]) ?? []).prefix(40) {
            if let match = find(child, role: role, title: title, description: description, depth: depth + 1) { return match }
        }
        return nil
    }
    private func requireApprovedFocus(_ command: Command) throws {
        guard approvedFocus.allows(actionID: command.actionID, currentPID: NSWorkspace.shared.frontmostApplication?.processIdentifier) else {
            throw OrbitError("The approved application's focus changed. Review a new action before continuing.")
        }
    }
    func insert(note: String, text: String, chunk: String, taskID: String) async throws {
        guard guardrail.isTaskActive(taskID), boundTaskID == taskID, noteID == note, createdNotes.contains(note), !chunk.isEmpty else { throw OrbitError("Dictation task or target is no longer active.") }
        guard !chunks.contains(chunk) else { return }
        guard text.utf16.count <= 10000 else { throw OrbitError("Dictation chunk exceeds the safe size limit.") }
        try requireFocusedNotesEditor()
        // Reserve before dispatch. Ambiguous failure stops dictation; it is never retried blindly.
        chunks.insert(chunk)
        do {
            _ = try await appleScript(Self.appendNoteScript, arguments: [note, "<div>" + Self.html(text).replacingOccurrences(of: "\n", with: "<br>") + "</div>"], taskID: taskID)
        } catch { if boundTaskID == taskID { noteID = nil; boundTaskID = nil }; throw error }
    }
    private func requireAccessibility() throws {
        guard AXIsProcessTrusted() else { throw OrbitError("Enable Orbit in System Settings → Privacy & Security → Accessibility.") }
    }
    private func requireFocusedNotesEditor() throws {
        try requireAccessibility()
        guard let app = NSWorkspace.shared.frontmostApplication, app.bundleIdentifier == "com.apple.Notes" else { throw OrbitError("Dictation paused: bring the bound note in Apple Notes to the front.") }
        var element: CFTypeRef?
        let appElement = AXUIElementCreateApplication(app.processIdentifier)
        guard AXUIElementCopyAttributeValue(appElement, kAXFocusedUIElementAttribute as CFString, &element) == .success,
              let element, CFGetTypeID(element) == AXUIElementGetTypeID() else { throw OrbitError("Cannot verify the focused Notes editor.") }
        let focused = unsafeBitCast(element, to: AXUIElement.self)
        var role: CFTypeRef?
        guard AXUIElementCopyAttributeValue(focused, kAXRoleAttribute as CFString, &role) == .success,
              (role as? String) == kAXTextAreaRole else { throw OrbitError("Click inside the bound note's body before dictating. Orbit will not type into an uncertain field.") }
    }
    func screenshot(command: Command) async throws -> [String: Any] {
        try requireValid(command)
        guard CGPreflightScreenCaptureAccess() else { throw OrbitError("Enable Screen Recording for Orbit in System Settings.") }
        guard #available(macOS 14.4, *) else { throw OrbitError("Screen capture requires macOS 14.4 or newer.") }
        let displayID = CGMainDisplayID()
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        try requireValid(command)
        guard let display = content.displays.first(where: { $0.displayID == displayID }) else { throw OrbitError("Main display unavailable.") }
        let configuration = SCStreamConfiguration()
        configuration.width = CGDisplayPixelsWide(displayID); configuration.height = CGDisplayPixelsHigh(displayID)
        configuration.showsCursor = true
        let image = try await SCScreenshotManager.captureImage(contentFilter: SCContentFilter(display: display, excludingWindows: []), configuration: configuration)
        try requireValid(command)
        guard let png = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:]) else { throw OrbitError("Could not encode screenshot.") }
        let bounds = CGDisplayBounds(displayID)
        mapping = PixelMapping(width: Double(image.width), height: Double(image.height), originX: bounds.minX, originY: bounds.minY, pointWidth: bounds.width, pointHeight: bounds.height)
        captureDisplay = displayID
        return ["image_base64": png.base64EncodedString(), "width": image.width, "height": image.height, "app_bundle_id": NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? ""]
    }
    private func position(_ params: [String: Any], x: String = "x", y: String = "y") throws -> CGPoint {
        guard captureDisplay == CGMainDisplayID(), let mapping, let px = ActionPolicy.number(params[x]), let py = ActionPolicy.number(params[y]),
              let point = mapping.point(x: px, y: py) else { throw OrbitError("Take a fresh screenshot before pointing; coordinates must be inside its pixel bounds.") }
        let bounds = CGDisplayBounds(CGMainDisplayID())
        guard bounds.minX == mapping.originX, bounds.minY == mapping.originY, bounds.width == mapping.pointWidth, bounds.height == mapping.pointHeight,
              CGDisplayPixelsWide(CGMainDisplayID()) == Int(mapping.width), CGDisplayPixelsHigh(CGMainDisplayID()) == Int(mapping.height) else { throw OrbitError("Display configuration changed; take a fresh screenshot.") }
        return CGPoint(x: point.0, y: point.1)
    }
    private func post(_ event: CGEvent?) throws {
        guard let event else { throw OrbitError("Could not create input event.") }
        event.setIntegerValueField(.eventSourceUserData, value: Self.syntheticMarker)
        event.post(tap: .cghidEventTap)
    }
    private func computer(_ params: [String: Any], command: Command) async throws -> [String: Any]? {
        let type = try string(params, "type")
        switch type {
        case "screenshot": return try await screenshot(command: command)
        case "wait": try await Task.sleep(nanoseconds: UInt64((ActionPolicy.number(params["seconds"]) ?? 0) * 1_000_000_000))
        case "move": try post(CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: try position(params), mouseButton: .left))
        case "click", "double_click":
            let point = try position(params)
            guard ["left", "right"].contains(params["button"] as? String ?? "left") else { throw OrbitError("Only left and right mouse buttons are supported.") }
            let right = (params["button"] as? String) == "right"
            let button: CGMouseButton = right ? .right : .left
            for count in 1...(type == "double_click" ? 2 : 1) {
                try requireValid(command); try requireApprovedFocus(command)
                let down = CGEvent(mouseEventSource: nil, mouseType: right ? .rightMouseDown : .leftMouseDown, mouseCursorPosition: point, mouseButton: button)
                down?.setIntegerValueField(.mouseEventClickState, value: Int64(count)); try post(down)
                let up = CGEvent(mouseEventSource: nil, mouseType: right ? .rightMouseUp : .leftMouseUp, mouseCursorPosition: point, mouseButton: button)
                up?.setIntegerValueField(.mouseEventClickState, value: Int64(count)); try post(up)
            }
        case "drag":
            let path: [CGPoint]
            if let points = params["path"] as? [[String: Any]] {
                guard (2...64).contains(points.count) else { throw OrbitError("Drag path must contain 2–64 points.") }
                path = try points.map { try position($0) }
            } else {
                let start = try position(params), end = try position(params, x: "to_x", y: "to_y")
                path = (0...12).map { n in let t = Double(n)/12; return CGPoint(x: start.x+(end.x-start.x)*t, y: start.y+(end.y-start.y)*t) }
            }
            let start = path[0], end = path[path.count-1]
            try post(CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: start, mouseButton: .left))
            defer { try? post(CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: CGEvent(source: nil)?.location ?? end, mouseButton: .left)) }
            for point in path.dropFirst() {
                try await Task.sleep(nanoseconds: 16_000_000); try requireValid(command); try requireApprovedFocus(command)
                try post(CGEvent(mouseEventSource: nil, mouseType: .leftMouseDragged, mouseCursorPosition: point, mouseButton: .left))
            }
        case "scroll":
            if params["x"] != nil { try post(CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: try position(params), mouseButton: .left)) }
            let dx = max(-2000, min(2000, params["dx"] as? Int ?? params["scroll_x"] as? Int ?? 0))
            let dy = max(-2000, min(2000, params["dy"] as? Int ?? params["scroll_y"] as? Int ?? 0))
            try post(CGEvent(scrollWheelEvent2Source: nil, units: .pixel, wheelCount: 2, wheel1: Int32(dy), wheel2: Int32(dx), wheel3: 0))
        case "keypress":
            let keys = try (params["keys"] as? [String] ?? [string(params, "key")])
            try keypress(keys)
        case "type":
            let text = try string(params, "text")
            let units = Array(text.utf16)
            for offset in stride(from: 0, to: units.count, by: 20) {
                try requireValid(command); try requireApprovedFocus(command)
                let part = Array(units[offset..<min(offset + 20, units.count)])
                let event = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: true)
                part.withUnsafeBufferPointer { event?.keyboardSetUnicodeString(stringLength: part.count, unicodeString: $0.baseAddress!) }
                try post(event)
                try post(CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: false))
                await Task.yield()
            }
        default: throw OrbitError("Unsupported computer action.")
        }
        return nil
    }
    private func keypress(_ raw: [String]) throws {
        let codes: [String: CGKeyCode] = ["a":0,"s":1,"d":2,"f":3,"h":4,"g":5,"z":6,"x":7,"c":8,"v":9,"b":11,"q":12,"w":13,"e":14,"r":15,"y":16,"t":17,"1":18,"2":19,"3":20,"4":21,"6":22,"5":23,"9":25,"7":26,"8":28,"0":29,"o":31,"u":32,"i":34,"p":35,"l":37,"j":38,"k":40,"n":45,"m":46,"enter":36,"return":36,"tab":48,"space":49,"backspace":51,"delete":51,"escape":53,"left":123,"right":124,"down":125,"up":126,"home":115,"end":119,"pageup":116,"pagedown":121]
        var flags = CGEventFlags(); var key: CGKeyCode?
        guard raw.count <= 5 else { throw OrbitError("Key chord is too large.") }
        for part in raw.map({ $0.lowercased() }) {
            switch part {
            case "command", "cmd", "meta": flags.insert(.maskCommand)
            case "shift": flags.insert(.maskShift)
            case "option", "alt": flags.insert(.maskAlternate)
            case "control", "ctrl": flags.insert(.maskControl)
            default:
                guard key == nil, let code = codes[part] else { throw OrbitError("Unsupported key chord.") }
                key = code
            }
        }
        guard let key else { throw OrbitError("Key chord has no supported key.") }
        let down = CGEvent(keyboardEventSource: nil, virtualKey: key, keyDown: true); down?.flags = flags; try post(down)
        let up = CGEvent(keyboardEventSource: nil, virtualKey: key, keyDown: false); up?.flags = flags; try post(up)
    }
    private func string(_ values: [String: Any], _ key: String) throws -> String {
        guard let value = values[key] as? String, !value.isEmpty else { throw OrbitError("Missing \(key).") }; return value
    }
    private static func html(_ value: String) -> String {
        value.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;").replacingOccurrences(of: ">", with: "&gt;").replacingOccurrences(of: "\"", with: "&quot;")
    }
    private func appleScript(_ source: String, arguments: [String], taskID: String) async throws -> String {
        // Only static scripts in this binary are executable; all user text is argv data.
        let process = Process(); process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        process.arguments = ["-e", source, "--"] + arguments
        let output = Pipe(), error = Pipe(); process.standardOutput = output; process.standardError = error
        scriptProcess = process; scriptTaskID = taskID
        defer { if scriptProcess === process { scriptProcess = nil; scriptTaskID = nil } }
        return try await withCheckedThrowingContinuation { continuation in
            process.terminationHandler = { process in
                let text = String(data: output.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
                let message = String(data: error.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
                if process.terminationStatus == 0 { continuation.resume(returning: text.trimmingCharacters(in: .whitespacesAndNewlines)) }
                else { continuation.resume(throwing: OrbitError("Apple Notes automation failed: " + message.prefix(700))) }
            }
            do { try process.run() } catch { continuation.resume(throwing: error) }
        }
    }
    private static let createNoteScript = """
    on run argv
      tell application "Notes"
        set targetNote to make new note at default folder of default account with properties {body:item 1 of argv}
        show targetNote
        activate
        return id of targetNote
      end tell
    end run
    """
    private static let showNoteScript = """
    on run argv
      tell application "Notes"
        set targetNote to first note whose id is item 1 of argv
        show targetNote
        activate
        return id of targetNote
      end tell
    end run
    """
    private static let appendNoteScript = """
    on run argv
      tell application "Notes"
        if frontmost is false then error "Notes is not frontmost"
        set chosenNotes to selection
        if (count of chosenNotes) is not 1 then error "Select exactly the bound note"
        set targetNote to item 1 of chosenNotes
        if id of targetNote is not item 1 of argv then error "The selected note changed"
        if password protected of targetNote then error "Locked note is unsupported"
        set body of targetNote to (body of targetNote) & (item 2 of argv)
        return "appended"
      end tell
    end run
    """
}
