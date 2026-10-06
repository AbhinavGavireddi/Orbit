import Foundation
import OrbitCore

struct Configuration {
    let taskURL: URL
    let voiceURL: URL
    let token: String
    let deviceID: String
    static let configURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Orbit/config.json")
    static func load() throws -> Configuration {
        let environment = ProcessInfo.processInfo.environment
        let data = try? Data(contentsOf: configURL)
        let saved = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: String] } ?? [:]
        func value(_ key: String, _ fallback: String = "") -> String { environment[key] ?? saved[key] ?? fallback }
        guard let task = URL(string: value("ORBIT_TASK_URL", "http://127.0.0.1:8100")),
              let voice = URL(string: value("ORBIT_VOICE_URL", "ws://127.0.0.1:8101")),
              validURL(task, secure: "https", local: "http"), validURL(voice, secure: "wss", local: "ws") else { throw OrbitError("Invalid service URLs. Use TLS for remote hosts, or local loopback services.") }
        var id = UserDefaults.standard.string(forKey: "orbit.deviceID")
        if id == nil { id = UUID().uuidString; UserDefaults.standard.set(id, forKey: "orbit.deviceID") }
        return Configuration(taskURL: task, voiceURL: voice, token: value("ORBIT_DEVICE_TOKEN"), deviceID: id!)
    }
    private static func validURL(_ url: URL, secure: String, local: String) -> Bool {
        guard let host = url.host, url.user == nil, url.password == nil, url.query == nil, url.fragment == nil else { return false }
        return url.scheme == secure || (url.scheme == local && ["localhost", "127.0.0.1", "::1"].contains(host))
    }
}

final class NoRedirectDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}

@MainActor final class APIClient {
    let configuration: Configuration
    private let session: URLSession
    init(_ configuration: Configuration) {
        self.configuration = configuration
        let settings = URLSessionConfiguration.ephemeral; settings.timeoutIntervalForRequest = 20
        session = URLSession(configuration: settings, delegate: NoRedirectDelegate(), delegateQueue: nil)
    }
    func request(_ path: String, method: String = "POST", body: [String: Any] = [:]) async throws -> [String: Any] {
        var request = URLRequest(url: configuration.taskURL.appendingPathComponent(path))
        request.httpMethod = method
        request.setValue("Bearer " + configuration.token, forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if method != "GET" { request.httpBody = try JSONSerialization.data(withJSONObject: body) }
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse, (200..<300).contains(response.statusCode) else { throw OrbitError("Task service request failed. Check service status and device token.") }
        if data.isEmpty { return [:] }
        return try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
    }
    func artifact(id: String, filename: String) async throws -> URL {
        guard UUID(uuidString: id) != nil, let safeName = ActionPolicy.artifactFilename(filename) else { throw OrbitError("Invalid artifact filename or ID.") }
        var request = URLRequest(url: configuration.taskURL.appendingPathComponent("v1/artifacts/" + id))
        request.setValue("Bearer " + configuration.token, forHTTPHeaderField: "Authorization")
        let (temporary, response) = try await session.download(for: request)
        guard let response = response as? HTTPURLResponse, response.statusCode == 200 else { throw OrbitError("Artifact download failed.") }
        let size = (try temporary.resourceValues(forKeys: [.fileSizeKey])).fileSize ?? 0
        guard size > 0, size <= 50_000_000 else { throw OrbitError("Artifact is empty or exceeds 50 MB.") }
        let directory = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Documents/Orbit/" + id, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let destination = directory.appendingPathComponent(safeName)
        if !FileManager.default.fileExists(atPath: destination.path) { try FileManager.default.moveItem(at: temporary, to: destination) }
        return destination
    }
    func websocketURL(base: URL, path: String, sessionID: String) throws -> URL {
        var components = URLComponents(url: base.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
        if components.scheme == "http" { components.scheme = "ws" }
        if components.scheme == "https" { components.scheme = "wss" }
        components.queryItems = [URLQueryItem(name: "session_id", value: sessionID), URLQueryItem(name: "device_id", value: configuration.deviceID)]
        guard let url = components.url else { throw OrbitError("Invalid websocket URL.") }; return url
    }
}

@MainActor final class SocketLink {
    private var socket: URLSessionWebSocketTask?
    private var reader: Task<Void, Never>?
    private var sender: Task<Void, Never>?
    private var heartbeat: Task<Void, Never>?
    private var pending: [URLSessionWebSocketTask.Message] = []
    private var generation = UUID()
    var onJSON: (([String: Any]) -> Void)?
    var onBinary: ((Data) -> Void)?
    var onFailure: ((String) -> Void)?
    func connect(url: URL, token: String, taskHeartbeat: Bool) {
        close()
        var request = URLRequest(url: url); request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        let socket = URLSession.shared.webSocketTask(with: request); self.socket = socket
        let generation = self.generation; socket.maximumMessageSize = 8_000_000; socket.resume()
        reader = Task { [weak self] in
            do {
                while !Task.isCancelled {
                    let message = try await socket.receive()
                    guard let self, self.generation == generation else { return }
                    switch message {
                    case .data(let data): self.onBinary?(data)
                    case .string(let string):
                        if let data = string.data(using: .utf8), let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] { self.onJSON?(object) }
                    @unknown default: break
                    }
                }
            } catch { if let self, self.generation == generation, !Task.isCancelled { self.fail(error.localizedDescription) } }
        }
        heartbeat = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 10_000_000_000)
                guard !Task.isCancelled, let self, self.generation == generation else { return }
                if taskHeartbeat { self.send(["type": "ping"]) }
                else { socket.sendPing { _ in } }
            }
        }
    }
    func send(_ object: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: object), let text = String(data: data, encoding: .utf8) else { return }
        enqueue(.string(text))
    }
    func send(_ data: Data) { enqueue(.data(data)) }
    private func enqueue(_ message: URLSessionWebSocketTask.Message) {
        guard socket != nil else { return }
        // Fail closed if a disconnected backend stops consuming microphone frames.
        guard pending.count < 200 else { fail("Connection is too slow; outgoing queue was stopped."); return }
        pending.append(message)
        guard sender == nil else { return }
        let generation = self.generation
        sender = Task { [weak self] in
            guard let self else { return }
            do {
                while !self.pending.isEmpty, self.generation == generation, !Task.isCancelled {
                    let next = self.pending.removeFirst()
                    guard let socket = self.socket else { break }
                    try await socket.send(next)
                }
                if self.generation == generation { self.sender = nil }
            } catch { if self.generation == generation, !Task.isCancelled { self.fail(error.localizedDescription) } }
        }
    }
    private func fail(_ reason: String) { close(); onFailure?(reason) }
    func close(finalFrame: [String: Any]? = nil) {
        let oldSocket = socket
        generation = UUID(); reader?.cancel(); reader = nil; sender?.cancel(); sender = nil; heartbeat?.cancel(); heartbeat = nil
        pending.removeAll(); socket = nil
        guard let oldSocket else { return }
        if let finalFrame, let data = try? JSONSerialization.data(withJSONObject: finalFrame),
           let text = String(data: data, encoding: .utf8) {
            // This detached socket owns only its final control. Reconnect may
            // proceed immediately and can never be closed by the old drain.
            _ = FinalFrameDrain(send: { try await oldSocket.send(.string(text)) },
                                close: { oldSocket.cancel(with: .normalClosure, reason: nil) })
        } else { oldSocket.cancel(with: .normalClosure, reason: nil) }
    }
}
