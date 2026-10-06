import Foundation

@MainActor public final class FinalFrameDrain {
    private var sender: Task<Void, Never>?
    private var deadline: Task<Void, Never>?
    private var closeSocket: (() -> Void)?
    public init(timeoutNanoseconds: UInt64 = 250_000_000,
                send: @escaping () async throws -> Void, close: @escaping () -> Void) {
        closeSocket = close
        sender = Task { [self] in
            try? await send()
            finish()
        }
        deadline = Task { [self] in
            do { try await Task.sleep(nanoseconds: timeoutNanoseconds) }
            catch { return }
            finish()
        }
    }
    private func finish() {
        guard let close = closeSocket else { return }
        closeSocket = nil
        sender?.cancel(); sender = nil
        deadline?.cancel(); deadline = nil
        // Socket cancellation is bounded even if send does not cooperate with
        // Swift task cancellation. A late send completion cannot close twice.
        close()
    }
}
