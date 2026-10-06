import Foundation

public struct PlaybackClock {
    public struct Segment: Equatable {
        public let item: String, index: Int, start: Int64, length: Int64, itemStart: Int64
    }
    private var nextFrame: Int64 = 0
    private var itemFrames: [String: Int64] = [:]
    public private(set) var segments: [Segment] = []
    public init() {}
    public mutating func enqueue(item: String, index: Int, frames: Int64, renderedFrame: Int64?) -> Segment {
        let key = "\(item):\(index)"
        // Explicit scheduling includes gaps in the output clock. A short lead keeps newly
        // arrived buffers out of the render deadline while queued buffers stay contiguous.
        let start = max(nextFrame, renderedFrame.map { max(0, $0) + 480 } ?? 0)
        let segment = Segment(item: item, index: index, start: start, length: frames, itemStart: itemFrames[key, default: 0])
        nextFrame = start + frames; itemFrames[key, default: 0] += frames
        segments.append(segment); return segment
    }
    public func playedMilliseconds(_ segment: Segment, renderedFrame: Int64, latencyFrames: Int64) -> Int {
        let heard = max(0, min(segment.length, renderedFrame - max(0, latencyFrames) - segment.start))
        return Int((segment.itemStart + heard) * 1000 / 24000)
    }
    public mutating func completed(_ segment: Segment) { segments.removeAll { $0 == segment } }
    public mutating func clear() { nextFrame = 0; itemFrames.removeAll(); segments.removeAll() }
}
