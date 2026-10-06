import Foundation

final class BundledRuntime {
    static var root: URL? { Bundle.main.resourceURL?.appendingPathComponent("orbit-runtime", isDirectory: true) }
    static var isBundled: Bool {
        guard let root else { return false }
        return FileManager.default.fileExists(atPath: root.appendingPathComponent("app/scripts/host.py").path)
    }

    private var process: Process?
    private var ready = false

    func start() async throws {
        guard let root = Self.root else { throw OrbitError("This copy of Orbit is missing its built-in services.") }
        stop()
        SecretStore.ensureTokens()
        let python = root.appendingPathComponent("python/bin/python3")
        let redis = root.appendingPathComponent("bin/valkey-server")
        let host = root.appendingPathComponent("app/scripts/host.py")
        let site = root.appendingPathComponent("site")
        guard FileManager.default.fileExists(atPath: python.path),
              FileManager.default.fileExists(atPath: redis.path) else {
            throw OrbitError("This copy of Orbit is missing its built-in services.")
        }
        let data = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Orbit/runtime", isDirectory: true)
        try FileManager.default.createDirectory(at: data, withIntermediateDirectories: true)
        let logURL = data.appendingPathComponent("logs/host.log")
        try FileManager.default.createDirectory(at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        let output = Pipe()
        let errorLog = try FileHandle(forWritingTo: logURL)
        let process = Process()
        process.executableURL = python
        process.arguments = ["-B", host.path]
        process.environment = [
            "HOME": NSHomeDirectory(), "USER": NSUserName(), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "TMPDIR": NSTemporaryDirectory(), "ORBIT_RUNTIME": root.appendingPathComponent("app").path,
            "ORBIT_PYTHON": python.path, "ORBIT_REDIS_BIN": redis.path, "ORBIT_SITE_PACKAGES": site.path,
            "ORBIT_DATA_DIR": data.path, "ORBIT_DEVICE_TOKEN": SecretStore.deviceToken,
            "ORBIT_SERVICE_TOKEN": SecretStore.serviceToken, "OPENAI_API_KEY": SecretStore.openai,
            "TYPESAFE_API_KEY": SecretStore.jev, "PYTHONDONTWRITEBYTECODE": "1",
        ]
        process.standardOutput = output
        process.standardError = errorLog
        self.process = process
        ready = false
        try process.run()
        try await waitUntilReady(output: output, logURL: logURL)
    }

    func stop() {
        guard let process, process.isRunning else { self.process = nil; return }
        process.terminate()
        process.waitUntilExit()
        self.process = nil
        ready = false
    }

    private func waitUntilReady(output: Pipe, logURL: URL) async throws {
        let current = process
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            let gate = ResumeOnce()
            let timeout = DispatchWorkItem {
                gate.resume(continuation, result: .failure(OrbitError("Orbit did not finish starting. The log is in \(logURL.path).")))
                current?.terminate()
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + 50, execute: timeout)
            output.fileHandleForReading.readabilityHandler = { handle in
                let data = handle.availableData
                guard !data.isEmpty, let text = String(data: data, encoding: .utf8), text.contains("ORBIT_READY") else { return }
                self.ready = true
                handle.readabilityHandler = nil
                timeout.cancel()
                gate.resume(continuation, result: .success(()))
            }
            current?.terminationHandler = { process in
                guard process.terminationStatus != 0 || !self.ready else { return }
                timeout.cancel()
                gate.resume(continuation, result: .failure(OrbitError("Orbit's built-in services stopped. The log is in \(logURL.path).")))
            }
        }
    }
}

private final class ResumeOnce: @unchecked Sendable {
    private let lock = NSLock()
    private var resumed = false
    func resume(_ continuation: CheckedContinuation<Void, Error>, result: Result<Void, Error>) {
        lock.lock()
        defer { lock.unlock() }
        guard !resumed else { return }
        resumed = true
        switch result {
        case .success: continuation.resume()
        case .failure(let error): continuation.resume(throwing: error)
        }
    }
}
