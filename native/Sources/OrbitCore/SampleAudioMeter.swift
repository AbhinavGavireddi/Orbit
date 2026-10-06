import Foundation

/// Reads PCM from a WAV file and returns per-hop RMS peaks — used to prove meter floors without a live mic.
public enum SampleAudioMeter {
    public static func rmsPeaks(wavURL: URL, hop: Int = 1024) throws -> [Float] {
        let data = try Data(contentsOf: wavURL)
        guard data.count > 44, String(data: data.prefix(4), encoding: .ascii) == "RIFF" else {
            throw NSError(domain: "SampleAudioMeter", code: 1, userInfo: [NSLocalizedDescriptionKey: "Not a RIFF/WAV file"])
        }
        // Minimal PCM s16le WAV reader (fixture is generated that way).
        let channels = Int(data[22]) | (Int(data[23]) << 8)
        let bits = Int(data[34]) | (Int(data[35]) << 8)
        guard channels >= 1, bits == 16 else {
            throw NSError(domain: "SampleAudioMeter", code: 2, userInfo: [NSLocalizedDescriptionKey: "Expected 16-bit PCM WAV"])
        }
        var dataOffset = 12
        var pcmOffset = 44
        var pcmSize = data.count - 44
        while dataOffset + 8 <= data.count {
            let id = String(data: data[dataOffset..<dataOffset+4], encoding: .ascii) ?? ""
            let size = Int(data[dataOffset+4]) | (Int(data[dataOffset+5]) << 8) | (Int(data[dataOffset+6]) << 16) | (Int(data[dataOffset+7]) << 24)
            if id == "data" {
                pcmOffset = dataOffset + 8
                pcmSize = size
                break
            }
            dataOffset += 8 + size + (size % 2)
        }
        let end = min(data.count, pcmOffset + pcmSize)
        let sampleBytes = end - pcmOffset
        let sampleCount = sampleBytes / 2
        guard sampleCount > 0 else { return [] }
        var peaks: [Float] = []
        var i = 0
        while i < sampleCount {
            let frameEnd = min(sampleCount, i + hop)
            var sum: Float = 0
            var n = 0
            var j = i
            while j < frameEnd {
                let o = pcmOffset + j * 2
                let lo = Int(data[o])
                let hi = Int(data[o + 1])
                var s = lo | (hi << 8)
                if s >= 0x8000 { s -= 0x10000 }
                let f = Float(s) / Float(Int16.max)
                sum += f * f
                n += 1
                j += channels // step by frame if mono; for stereo this undersamples L only via hop — fixture is mono
            }
            peaks.append(sqrt(sum / Float(max(1, n))))
            i = frameEnd
        }
        return peaks
    }
}
