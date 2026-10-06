import Foundation
import AVFoundation
import Speech
import OSLog
import OrbitCore

// All graph mutation, converters and playback accounting share one queue. The same
// engine renders the assistant and captures the processed microphone, providing the
// voice-processing unit with its own output reference for acoustic echo cancellation.
final class AudioIO {
    typealias Played = (String, Int, Int)
    private let queue = DispatchQueue(label: "dev.orbit.audio", qos: .userInitiated)
    private let logger = Logger(subsystem: "dev.orbit.assistant", category: "local-speech")
    private let captureHandoff: AudioCaptureHandoff
    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private let voiceFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 24000, channels: 1, interleaved: false)!
    private let playbackFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 24000, channels: 1, interleaved: false)!
    private var speechFormat: AVAudioFormat?
    private var speechToken: UUID?
    private var voiceConverter: AVAudioConverter?
    private var monoVoiceConverter: AVAudioConverter?
    private var speechConverter: AVAudioConverter?
    private var downmixChannels = false
    private var tapInstalled = false
    private var gate = AudioLifecycleGate()
    private var admission = PlaybackAdmission()
    private var interruption = SpeechInterruptionGate()
    private var clock = PlaybackClock()
    private var timer: DispatchSourceTimer?
    private var onSpeechBuffer: ((AVAudioPCMBuffer) -> Void)?
    private var lastInputMeter = 0.0, lastOutputMeter = 0.0
    private var lastEnergyTelemetry = 0.0
    private var routeObserver: NSObjectProtocol?
    private var routeRecovery = AudioRouteRecovery()
    private var routeRecoveryWork: DispatchWorkItem?
    var onPCM: ((Data, Int, Int, Int) -> Void)?
    var onPlayed: ((String, Int, Int, Int) -> Void)?
    var onError: ((String) -> Void)?
    var onCaptureFailure: ((String) -> Void)?
    var onInputMeter: ((Float, Int) -> Void)?
    var onOutputMeter: ((Float, Int) -> Void)?
    var onPlaybackChanged: ((Bool, Int) -> Void)?
    var onLocalInterruption: (([Played], Int, Int) -> Void)?

    init() {
        captureHandoff = AudioCaptureHandoff(queue: queue)
        engine.attach(player)
        engine.connect(player, to: engine.mainMixerNode, format: playbackFormat)
        engine.mainMixerNode.installTap(onBus: 0, bufferSize: 1024, format: nil) { [weak self] buffer, _ in
            let level = Self.rms(buffer)
            self?.queue.async { [weak self] in
                guard let self, !self.clock.segments.isEmpty else { return }
                let now = ProcessInfo.processInfo.systemUptime
                guard now - self.lastOutputMeter >= 0.08 else { return }
                self.lastOutputMeter = now
                self.onOutputMeter?(level, self.gate.playbackGeneration)
            }
        }
        routeObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange,
            object: engine,
            queue: nil
        ) { [weak self] _ in
            self?.recoverCaptureAfterRouteChange()
        }
    }
    func configureSpeech(format: AVAudioFormat?, token: UUID, sink: ((AVAudioPCMBuffer) -> Void)?) {
        queue.async {
            self.speechFormat = format; self.speechToken = token
            self.speechConverter = nil; self.onSpeechBuffer = sink
        }
    }
    func setStreaming(_ value: Bool) { queue.async {
        self.gate.setStreaming(value)
        self.captureHandoff.update(self.gate, at: mach_absolute_time())
    } }
    func acceptsCapture(_ generation: Int) -> Bool { queue.sync { gate.acceptsCapture(generation) } }
    func acceptsPlayback(_ generation: Int) -> Bool { queue.sync { gate.acceptsPlayback(generation) } }
    func acceptsSession(_ generation: Int) -> Bool { queue.sync { gate.acceptsSession(generation) } }
    func acceptsStream(capture: Int, stream: Int, session: Int) -> Bool { queue.sync { gate.acceptsStream(capture: capture, stream: stream, session: session) } }

    func startCapture() {
        queue.async { [self] in
            guard let generation = gate.beginCapture() else { return }
            captureHandoff.update(gate, at: mach_absolute_time())
            armCapture(generation: generation)
        }
    }
    private func recoverCaptureAfterRouteChange() {
        queue.async { [self] in
            _ = routeRecovery.noteChange(now: ProcessInfo.processInfo.systemUptime)
            routeRecoveryWork?.cancel()
            let work = DispatchWorkItem { [weak self] in
                guard let self else { return }
                self.queue.async {
                    guard self.routeRecovery.shouldFire(
                        now: ProcessInfo.processInfo.systemUptime,
                        intendedCapture: self.gate.capturing,
                        engineRunning: self.engine.isRunning
                    ), let generation = self.gate.recoveryGeneration() else { return }
                    self.logger.notice("engine_route_change recovering_capture=true")
                    self.armCapture(generation: generation)
                }
            }
            routeRecoveryWork = work
            queue.asyncAfter(deadline: .now() + .milliseconds(350), execute: work)
        }
    }
    private func armCapture(generation: Int) {
        do {
            // Voice-processing mode can change only while the engine is stopped.
            // Enabling microphone capture during output retires that output first.
            if engine.isRunning {
                let hadPlayback = !clock.segments.isEmpty
                let heard = clearPlaybackLocked()
                for (item, index, ms) in heard { onPlayed?(item, index, ms, gate.sessionGeneration) }
                if hadPlayback { onLocalInterruption?(heard, gate.sessionGeneration, gate.playbackGeneration) }
                engine.stop()
            }
            if tapInstalled { engine.inputNode.removeTap(onBus: 0); tapInstalled = false }
            let input = engine.inputNode
            if !input.isVoiceProcessingEnabled {
                do { try input.setVoiceProcessingEnabled(true) }
                catch { onError?("Voice processing unavailable; use headphones until echo cancellation is verified: \(error.localizedDescription)") }
            }
            logger.notice("capture_route=system_default")
            let format = input.outputFormat(forBus: 0)
            guard format.sampleRate > 0, format.channelCount > 0 else {
                throw OrbitError("No usable microphone input. Select one in System Settings → Sound.")
            }
            speechConverter = nil
            monoVoiceConverter = nil
            downmixChannels = CaptureDownmixPolicy.useLoudestChannel(channelCount: Int(format.channelCount))
            if downmixChannels {
                voiceConverter = nil
                logger.notice("capture_downmix=loudest_channel source_channels=\(Int(format.channelCount), privacy: .public)")
            } else {
                voiceConverter = AVAudioConverter(from: format, to: voiceFormat)
                guard voiceConverter != nil else { throw OrbitError("The selected microphone cannot produce Orbit's 24 kHz audio format.") }
            }
            input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, time in
                guard let self else { return }
                self.captureHandoff.submit(capture: generation, capturedAt: time.isHostTimeValid ? time.hostTime : nil,
                    copy: { Self.copy(buffer) }, consume: { [weak self] copy, identity in
                        self?.process(copy, identity: identity)
                    })
            }
            tapInstalled = true
            engine.prepare(); try engine.start()
        } catch {
            gate.stopCapture()
            captureHandoff.update(gate, at: mach_absolute_time())
            if tapInstalled { engine.inputNode.removeTap(onBus: 0); tapInstalled = false }
            engine.stop()
            onCaptureFailure?(error.localizedDescription)
        }
    }
    func stopCapture() {
        // Mute waits for the input tap and hardware engine to stop. Do not implement
        // mute as merely dropping packets or voiceProcessingInputMuted.
        queue.sync {
            gate.stopCapture(); speechToken = nil
            captureHandoff.update(gate, at: mach_absolute_time())
            if tapInstalled { engine.inputNode.removeTap(onBus: 0); tapInstalled = false }
            _ = clearPlaybackLocked()
            engine.stop(); engine.reset()
            if engine.inputNode.isVoiceProcessingEnabled { try? engine.inputNode.setVoiceProcessingEnabled(false) }
            voiceConverter = nil; monoVoiceConverter = nil; speechConverter = nil; downmixChannels = false; onSpeechBuffer = nil
            interruption = SpeechInterruptionGate()
        }
    }
    private static func copy(_ buffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        guard let copy = AVAudioPCMBuffer(pcmFormat: buffer.format, frameCapacity: buffer.frameLength) else { return nil }
        copy.frameLength = buffer.frameLength
        let source = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: buffer.audioBufferList))
        let destination = UnsafeMutableAudioBufferListPointer(copy.mutableAudioBufferList)
        for index in 0..<source.count {
            if let from = source[index].mData, let to = destination[index].mData { memcpy(to, from, Int(source[index].mDataByteSize)) }
        }
        return copy
    }
    private static func rms(_ buffer: AVAudioPCMBuffer) -> Float {
        guard let samples = buffer.floatChannelData?[0], buffer.frameLength > 0 else { return 0 }
        var sum: Float = 0
        for index in 0..<Int(buffer.frameLength) { sum += samples[index] * samples[index] }
        return sqrt(sum / Float(buffer.frameLength))
    }
    private static func formatAwareRMS(_ buffer: AVAudioPCMBuffer, channel: Int) -> Double {
        let channelCount = Int(buffer.format.channelCount)
        guard buffer.frameLength > 0, channel >= 0, channel < channelCount else { return 0 }
        let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: buffer.audioBufferList))
        var firstChannel = 0
        for audioBuffer in buffers {
            let channelsInBuffer = Int(audioBuffer.mNumberChannels)
            guard channelsInBuffer > 0 else { continue }
            defer { firstChannel += channelsInBuffer }
            guard channel >= firstChannel, channel < firstChannel + channelsInBuffer, let data = audioBuffer.mData else { continue }
            let localChannel = channel - firstChannel
            let sampleSize: Int
            switch buffer.format.commonFormat {
            case .pcmFormatFloat32: sampleSize = MemoryLayout<Float>.stride
            case .pcmFormatInt16: sampleSize = MemoryLayout<Int16>.stride
            default: return 0
            }
            let availableSamples = Int(audioBuffer.mDataByteSize) / sampleSize
            guard availableSamples > localChannel else { return 0 }
            let availableFrames = (availableSamples - 1 - localChannel) / channelsInBuffer + 1
            let frameCount = min(Int(buffer.frameLength), availableFrames)
            guard frameCount > 0 else { return 0 }
            var sum = 0.0
            switch buffer.format.commonFormat {
            case .pcmFormatFloat32:
                let samples = data.assumingMemoryBound(to: Float.self)
                for frame in 0..<frameCount {
                    let value = Double(samples[localChannel + frame * channelsInBuffer])
                    sum += value * value
                }
            case .pcmFormatInt16:
                let samples = data.assumingMemoryBound(to: Int16.self)
                for frame in 0..<frameCount {
                    let value = Double(samples[localChannel + frame * channelsInBuffer]) / 32768.0
                    sum += value * value
                }
            default: return 0
            }
            return sqrt(sum / Double(frameCount))
        }
        return 0
    }
    private static func maxFormatAwareRMS(_ buffer: AVAudioPCMBuffer) -> Double {
        (0..<Int(buffer.format.channelCount)).reduce(0) { max($0, formatAwareRMS(buffer, channel: $1)) }
    }
    private func convert(_ input: AVAudioPCMBuffer, converter: AVAudioConverter?, format: AVAudioFormat) -> AVAudioPCMBuffer? {
        guard let converter else { return nil }
        let capacity = AVAudioFrameCount(ceil(Double(input.frameLength) * format.sampleRate / input.format.sampleRate) + 64)
        guard let result = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: capacity) else { return nil }
        var supplied = false; var error: NSError?
        converter.convert(to: result, error: &error) { _, status in
            if supplied { status.pointee = .noDataNow; return nil }
            supplied = true; status.pointee = .haveData; return input
        }
        guard error == nil, result.frameLength > 0 else { return nil }
        return result
    }
    private struct MonoCapture {
        let buffer: AVAudioPCMBuffer
        let channel: Int
    }
    private static func monoLoudest(_ buffer: AVAudioPCMBuffer) -> MonoCapture? {
        let channels = Int(buffer.format.channelCount)
        guard channels > 0, buffer.frameLength > 0,
              let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: buffer.format.sampleRate, channels: 1, interleaved: false),
              let mono = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: buffer.frameLength),
              let destination = mono.floatChannelData?[0] else { return nil }
        let energies = (0..<channels).map { formatAwareRMS(buffer, channel: $0) }
        let channel = CaptureDownmixPolicy.loudestChannel(energies)
        let frames = copyChannel(buffer, channel: channel, into: destination, capacity: Int(buffer.frameLength))
        guard frames > 0 else { return nil }
        mono.frameLength = AVAudioFrameCount(frames)
        return MonoCapture(buffer: mono, channel: channel)
    }
    private static func copyChannel(_ buffer: AVAudioPCMBuffer, channel: Int, into destination: UnsafeMutablePointer<Float>, capacity: Int) -> Int {
        let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: buffer.audioBufferList))
        var firstChannel = 0
        for audioBuffer in buffers {
            let channelsInBuffer = Int(audioBuffer.mNumberChannels)
            guard channelsInBuffer > 0 else { continue }
            defer { firstChannel += channelsInBuffer }
            guard channel >= firstChannel, channel < firstChannel + channelsInBuffer, let data = audioBuffer.mData else { continue }
            let local = channel - firstChannel
            switch buffer.format.commonFormat {
            case .pcmFormatFloat32:
                let samples = data.assumingMemoryBound(to: Float.self)
                let available = Int(audioBuffer.mDataByteSize) / MemoryLayout<Float>.stride
                guard available > local else { return 0 }
                let frames = min(capacity, (available - 1 - local) / channelsInBuffer + 1, Int(buffer.frameLength))
                for frame in 0..<frames { destination[frame] = samples[local + frame * channelsInBuffer] }
                return frames
            case .pcmFormatInt16:
                let samples = data.assumingMemoryBound(to: Int16.self)
                let available = Int(audioBuffer.mDataByteSize) / MemoryLayout<Int16>.stride
                guard available > local else { return 0 }
                let frames = min(capacity, (available - 1 - local) / channelsInBuffer + 1, Int(buffer.frameLength))
                for frame in 0..<frames { destination[frame] = Float(samples[local + frame * channelsInBuffer]) / 32768 }
                return frames
            default:
                return 0
            }
        }
        return 0
    }
    private func process(_ buffer: AVAudioPCMBuffer, identity: AudioCaptureIdentity) {
        let mixed = downmixChannels ? Self.monoLoudest(buffer) : nil
        let render = mixed?.buffer ?? buffer
        if mixed != nil, monoVoiceConverter == nil || monoVoiceConverter?.inputFormat.sampleRate != render.format.sampleRate {
            monoVoiceConverter = AVAudioConverter(from: render.format, to: voiceFormat)
            speechConverter = nil
        }
        var speechPCM: AVAudioPCMBuffer?
        if let format = speechFormat {
            if speechConverter == nil { speechConverter = AVAudioConverter(from: render.format, to: format) }
            speechPCM = convert(render, converter: speechConverter, format: format)
            if let speechPCM { onSpeechBuffer?(speechPCM) }
        }
        let pcm = convert(render, converter: mixed == nil ? voiceConverter : monoVoiceConverter, format: voiceFormat)
        let now = ProcessInfo.processInfo.systemUptime
        if now - lastEnergyTelemetry >= 1 {
            lastEnergyTelemetry = now
            let sourceRMS = Self.maxFormatAwareRMS(buffer)
            let voiceRMS = pcm.map(Self.maxFormatAwareRMS) ?? 0
            let voiceFrames = Int(pcm?.frameLength ?? 0)
            let engineRunning = engine.isRunning
            let voiceProcessing = engine.inputNode.isVoiceProcessingEnabled
            let downmixChannel = mixed?.channel ?? -1
            if let speechPCM {
                logger.notice("source_max_rms=\(sourceRMS, privacy: .public) source_channels=\(Int(buffer.format.channelCount), privacy: .public) source_sample_rate=\(buffer.format.sampleRate, privacy: .public) source_frames=\(Int(buffer.frameLength), privacy: .public) voice_rms=\(voiceRMS, privacy: .public) voice_frames=\(voiceFrames, privacy: .public) speech_rms=\(Self.maxFormatAwareRMS(speechPCM), privacy: .public) speech_frames=\(Int(speechPCM.frameLength), privacy: .public) engine_running=\(engineRunning, privacy: .public) voice_processing=\(voiceProcessing, privacy: .public) downmix_channel=\(downmixChannel, privacy: .public)")
            } else {
                logger.notice("source_max_rms=\(sourceRMS, privacy: .public) source_channels=\(Int(buffer.format.channelCount), privacy: .public) source_sample_rate=\(buffer.format.sampleRate, privacy: .public) source_frames=\(Int(buffer.frameLength), privacy: .public) voice_rms=\(voiceRMS, privacy: .public) voice_frames=\(voiceFrames, privacy: .public) engine_running=\(engineRunning, privacy: .public) voice_processing=\(voiceProcessing, privacy: .public) downmix_channel=\(downmixChannel, privacy: .public)")
            }
        }
        guard let pcm, let samples = pcm.int16ChannelData?[0] else { return }
        if now - lastInputMeter >= 0.08 {
            lastInputMeter = now
            var sum: Float = 0
            for index in 0..<Int(pcm.frameLength) { let value = Float(samples[index]) / 32768; sum += value * value }
            onInputMeter?(sqrt(sum / Float(max(1, pcm.frameLength))), identity.capture)
        }
        if gate.acceptsStream(capture: identity.capture, stream: identity.stream, session: identity.session) {
            onPCM?(Data(bytes: samples, count: Int(pcm.frameLength) * 2), identity.capture, identity.stream, identity.session)
        }
    }
    func observeSpeech(_ detected: Bool, token: UUID) {
        queue.async { [self] in
            guard speechToken == token, gate.capturing, gate.streaming else { return }
            guard interruption.observe(speech: detected, hasPlayback: !clock.segments.isEmpty) else { return }
            let heard = clearPlaybackLocked()
            // Playback has already stopped locally; this notification only informs the
            // gateway and sends the last actually-heard position on the UI actor.
            onLocalInterruption?(heard, gate.sessionGeneration, gate.playbackGeneration)
        }
    }
    func play(_ data: Data, item: String, index: Int) {
        queue.async { [self] in
            guard admission.accepts(item: item), data.count % 2 == 0, data.count > 0, data.count <= 2_400_000 else { return }
            do {
                guard gate.capturing || !engine.inputNode.isVoiceProcessingEnabled else {
                    throw OrbitError("Playback is paused because the microphone route could not be disabled safely.")
                }
                if AudioEngineRecoveryPolicy.shouldRecover(intendedCapture: gate.capturing, engineRunning: engine.isRunning),
                   let generation = gate.recoveryGeneration() {
                    armCapture(generation: generation)
                }
                if !engine.isRunning { engine.prepare(); try engine.start() }
                let count = data.count / 2
                guard let buffer = AVAudioPCMBuffer(pcmFormat: playbackFormat, frameCapacity: AVAudioFrameCount(count)), let samples = buffer.floatChannelData?[0] else { return }
                buffer.frameLength = AVAudioFrameCount(count)
                data.withUnsafeBytes { bytes in
                    for n in 0..<count { samples[n] = Float(Int16(littleEndian: bytes.loadUnaligned(fromByteOffset: n * 2, as: Int16.self))) / 32768 }
                }
                let rendered = player.lastRenderTime.flatMap { player.playerTime(forNodeTime: $0)?.sampleTime }
                let segment = clock.enqueue(item: item, index: index, frames: Int64(count), renderedFrame: player.isPlaying ? rendered : nil)
                let generation = gate.playbackGeneration, session = gate.sessionGeneration
                player.scheduleBuffer(buffer, at: AVAudioTime(sampleTime: segment.start, atRate: 24000), options: [], completionCallbackType: .dataPlayedBack) { [weak self] _ in
                    self?.queue.async {
                        guard let self, self.gate.acceptsPlayback(generation) else { return }
                        self.onPlayed?(item, index, Int((segment.itemStart + segment.length) * 1000 / 24000), session)
                        self.clock.completed(segment)
                        if self.clock.segments.isEmpty {
                            self.timer?.cancel(); self.timer = nil
                            self.onOutputMeter?(0, generation); self.onPlaybackChanged?(false, generation)
                        }
                    }
                }
                if !player.isPlaying { player.play() }
                onPlaybackChanged?(true, generation)
                startProgressTimer()
            } catch { onError?("Playback failed: \(error.localizedDescription)") }
        }
    }
    private func startProgressTimer() {
        guard timer == nil else { return }
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now(), repeating: .milliseconds(100))
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            for (item, index, milliseconds) in self.progress() { self.onPlayed?(item, index, milliseconds, self.gate.sessionGeneration) }
        }
        timer.resume(); self.timer = timer
    }
    private func progress() -> [Played] {
        guard player.isPlaying, let rendered = player.lastRenderTime, let position = player.playerTime(forNodeTime: rendered) else { return [] }
        let latency = Int64(engine.outputNode.presentationLatency * 24000)
        return clock.segments.filter { position.sampleTime >= $0.start }.map { segment in
            (segment.item, segment.index, clock.playedMilliseconds(segment, renderedFrame: position.sampleTime, latencyFrames: latency))
        }
    }
    private func clearPlaybackLocked(blockingItem: String? = nil) -> [Played] {
        let heard = progress()
        admission.block(items: clock.segments.map(\.item) + [blockingItem].compactMap { $0 })
        gate.clearPlayback(); player.stop(); player.reset(); clock.clear()
        timer?.cancel(); timer = nil
        onOutputMeter?(0, gate.playbackGeneration); onPlaybackChanged?(false, gate.playbackGeneration)
        return heard
    }
    @discardableResult func clearPlayback(blockingItem: String? = nil) -> [Played] {
        queue.sync { clearPlaybackLocked(blockingItem: blockingItem) }
    }
    func resetSession() {
        queue.sync {
            _ = clearPlaybackLocked(); gate.resetSession()
            captureHandoff.update(gate, at: mach_absolute_time())
            admission.reset(); interruption = SpeechInterruptionGate()
        }
    }
}

@available(macOS 26.0, *)
actor LocalSpeech {
    private let logger = Logger(subsystem: "dev.orbit.assistant", category: "local-speech")
    private var analyzer: SpeechAnalyzer?
    private var continuation: AsyncStream<AnalyzerInput>.Continuation?
    private var resultsTask: Task<Void, Never>?
    private var detectorTask: Task<Void, Never>?
    private var epoch = UUID()

    func start(request: UUID, installAssets: Bool, onText: @escaping (String, Bool) -> Void,
               onSpeechActivity: @escaping (Bool) -> Void,
               onFailure: @escaping () -> Void = {}) async throws -> (AVAudioFormat, AsyncStream<AnalyzerInput>.Continuation) {
        epoch = request
        let previous = detach()
        await previous?.cancelAndFinishNow()
        try requireCurrent(request)
        guard SpeechTranscriber.isAvailable,
              let locale = await SpeechTranscriber.supportedLocale(equivalentTo: Locale(identifier: "en-US")) else {
            throw OrbitError("On-device English speech transcription is unavailable on this Mac. Use typed controls.")
        }
        try requireCurrent(request)
        let transcriber = SpeechTranscriber(locale: locale, preset: .progressiveTranscription)
        let detector = SpeechDetector(detectionOptions: .init(sensitivityLevel: .medium), reportResults: true)
        let modules: [any SpeechModule] = [transcriber, detector]
        let status = await AssetInventory.status(forModules: modules)
        try requireCurrent(request)
        if status != .installed {
            guard installAssets else { throw OrbitError("Download Apple's on-device English speech assets in Orbit Settings to enable local wake listening.") }
            guard status != .unsupported else { throw OrbitError("Apple speech assets do not support this device.") }
            if let installation = try await AssetInventory.assetInstallationRequest(supporting: modules) { try await installation.downloadAndInstall() }
            try requireCurrent(request)
            guard await AssetInventory.status(forModules: modules) == .installed else { throw OrbitError("Speech assets are not installed yet. Try again after the download finishes.") }
        }
        guard let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: modules) else { throw OrbitError("No compatible local speech audio format.") }
        try requireCurrent(request)
        let pair = AsyncStream<AnalyzerInput>.makeStream(bufferingPolicy: .bufferingNewest(16))
        let analyzer = SpeechAnalyzer(modules: modules)
        try await analyzer.prepareToAnalyze(in: format)
        try requireCurrent(request)
        resultsTask = Task {
            do {
                for try await result in transcriber.results {
                    guard !Task.isCancelled else { break }
                    let plainText = String(result.text.characters)
                    let tokenCount = plainText.split(whereSeparator: \.isWhitespace).count
                    logger.info("result_final=\(result.isFinal, privacy: .public) token_count=\(tokenCount, privacy: .public) character_count=\(plainText.count, privacy: .public) phrase_match=\(PhraseMatcher.match(plainText) != nil, privacy: .public)")
                    onText(plainText, result.isFinal)
                }
            } catch {
                if !Task.isCancelled {
                    logger.error("transcriber_stream_ended")
                    onFailure()
                }
            }
        }
        detectorTask = Task {
            var lastSpeechDetected: Bool?
            do {
                for try await result in detector.results {
                    guard !Task.isCancelled else { break }
                    let speechDetected = result.speechDetected
                    if lastSpeechDetected != speechDetected {
                        logger.info("speech_detected=\(speechDetected, privacy: .public)")
                        lastSpeechDetected = speechDetected
                    }
                    onSpeechActivity(speechDetected)
                }
            } catch {
                if !Task.isCancelled {
                    logger.error("detector_stream_ended")
                }
            }
        }
        self.analyzer = analyzer; continuation = pair.continuation
        do { try await analyzer.start(inputSequence: pair.stream); try requireCurrent(request) }
        catch {
            if epoch == request { let failed = detach(); await failed?.cancelAndFinishNow() }
            throw error
        }
        return (format, pair.continuation)
    }
    private func requireCurrent(_ request: UUID) throws {
        guard epoch == request, !Task.isCancelled else { throw CancellationError() }
    }
    private func detach() -> SpeechAnalyzer? {
        continuation?.finish(); continuation = nil
        resultsTask?.cancel(); resultsTask = nil; detectorTask?.cancel(); detectorTask = nil
        let old = analyzer; analyzer = nil; return old
    }
    func stop(ifRequest request: UUID) async {
        guard epoch == request else { return }
        epoch = UUID(); let old = detach(); await old?.cancelAndFinishNow()
    }
}
