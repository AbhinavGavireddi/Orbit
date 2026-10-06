import Foundation

public struct AudioCaptureIdentity {
    public let capture: Int, stream: Int, session: Int
}

// The input tap copies on its real-time callback before handing owned data to
// the audio queue. Lifecycle snapshots are synchronized without waiting for
// that queue, which may be stopping the input tap itself.
public final class AudioCaptureHandoff {
    private let queue: DispatchQueue
    private let lock = NSLock()
    private var lifecycle = AudioLifecycleGate()
    private var boundary: UInt64 = 0
    public init(queue: DispatchQueue) { self.queue = queue }
    public func update(_ value: AudioLifecycleGate, at hostTime: UInt64) {
        lock.lock(); defer { lock.unlock() }
        if value.capturing != lifecycle.capturing || value.streamGeneration != lifecycle.streamGeneration
            || value.sessionGeneration != lifecycle.sessionGeneration { boundary = hostTime }
        lifecycle = value
    }
    private func identity(capture: Int, capturedAt: UInt64?) -> AudioCaptureIdentity? {
        lock.lock(); defer { lock.unlock() }
        guard lifecycle.acceptsCapture(capture), let capturedAt, capturedAt >= boundary else { return nil }
        return AudioCaptureIdentity(capture: capture, stream: lifecycle.streamGeneration, session: lifecycle.sessionGeneration)
    }
    private func accepts(_ identity: AudioCaptureIdentity) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return lifecycle.acceptsCapture(identity.capture) && lifecycle.streamGeneration == identity.stream
            && lifecycle.sessionGeneration == identity.session
    }
    public func submit<Buffer>(capture: Int, capturedAt: UInt64?, copy: () -> Buffer?,
                               consume: @escaping (Buffer, AudioCaptureIdentity) -> Void) {
        // Bind before copying. A suspended callback must never relabel old PCM
        // using the session or stream that happens to be current on delivery.
        guard let identity = identity(capture: capture, capturedAt: capturedAt), let buffer = copy() else { return }
        queue.async {
            guard self.accepts(identity) else { return }
            consume(buffer, identity)
        }
    }
}
