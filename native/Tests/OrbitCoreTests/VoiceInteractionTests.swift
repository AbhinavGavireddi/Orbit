import XCTest
@testable import OrbitCore

final class VoiceInteractionTests: XCTestCase {
    func testPartialWakeAndControlWordsCannotActBeforeSentenceIsFinal() {
        for text in ["Orbit wake up", "stop", "goodbye", "finish dictation"] {
            XCTAssertNil(LocalControlPolicy.match(text, final: false, awake: true))
        }
        XCTAssertNil(LocalControlPolicy.match("Orbit wake up tomorrow", final: true, awake: false))
        XCTAssertEqual(LocalControlPolicy.match("Orbit, wake up!", final: true, awake: false), .wake)
        XCTAssertNil(LocalControlPolicy.match("stop", final: true, awake: false))
        XCTAssertEqual(LocalControlPolicy.match("Orbit stop.", final: true, awake: true), .stop)
    }

    func testMicArrayUsesTheLoudestChannelBecauseTheConverterEmitsSilence() {
        XCTAssertFalse(CaptureDownmixPolicy.useLoudestChannel(channelCount: 1))
        XCTAssertFalse(CaptureDownmixPolicy.useLoudestChannel(channelCount: 2))
        XCTAssertTrue(CaptureDownmixPolicy.useLoudestChannel(channelCount: 9))
        XCTAssertEqual(CaptureDownmixPolicy.loudestChannel([0, 0.002, 0.12, 0]), 2)
        XCTAssertEqual(CaptureDownmixPolicy.loudestChannel([]), 0)
    }

    func testRouteChangeRecoversOnlyAnIntendedStoppedCapture() {
        XCTAssertTrue(AudioEngineRecoveryPolicy.shouldRecover(intendedCapture: true, engineRunning: false))
        XCTAssertFalse(AudioEngineRecoveryPolicy.shouldRecover(intendedCapture: true, engineRunning: true))
        XCTAssertFalse(AudioEngineRecoveryPolicy.shouldRecover(intendedCapture: false, engineRunning: false))
        XCTAssertFalse(AudioEngineRecoveryPolicy.shouldRecover(intendedCapture: false, engineRunning: true))
    }

    func testCaptureRecoveryReusesTheOpenGenerationInsteadOfOpeningASecondCapture() {
        var gate = AudioLifecycleGate()
        let generation = gate.beginCapture()!
        XCTAssertNil(gate.beginCapture(), "A second startCapture must not win while capture is already intended.")
        XCTAssertEqual(gate.recoveryGeneration(), generation)
        gate.stopCapture()
        XCTAssertNil(gate.recoveryGeneration())
    }

    func testEmptyLocalTranscriptIsIgnoredAndAnalyzerFailureRestarts() {
        XCTAssertEqual(LocalSpeechLifecyclePolicy.decide(text: "", failed: false), .ignore)
        XCTAssertEqual(LocalSpeechLifecyclePolicy.decide(text: "   ", failed: false), .ignore)
        XCTAssertEqual(LocalSpeechLifecyclePolicy.decide(text: "Orbit, wake up", failed: false), .use("Orbit, wake up"))
        XCTAssertEqual(LocalSpeechLifecyclePolicy.decide(text: "Orbit, wake up", failed: true), .restart)
        XCTAssertEqual(LocalSpeechLifecyclePolicy.decide(text: "", failed: true), .restart)
    }

    func testLocalSpeechRestartBudgetAllowsAFewRecoveriesThenHolds() {
        var budget = LocalSpeechRestartBudget()
        XCTAssertTrue(budget.allow(now: 1))
        XCTAssertTrue(budget.allow(now: 2))
        XCTAssertTrue(budget.allow(now: 3))
        XCTAssertFalse(budget.allow(now: 4))
        XCTAssertTrue(budget.allow(now: 14))
    }

    func testRouteChangeRecoveryWaitsForTheRouteToSettle() {
        var recovery = AudioRouteRecovery()
        _ = recovery.noteChange(now: 1.0, settle: 0.35)
        XCTAssertFalse(recovery.shouldFire(now: 1.2, intendedCapture: true, engineRunning: false))
        _ = recovery.noteChange(now: 1.3, settle: 0.35)
        XCTAssertFalse(recovery.shouldFire(now: 1.5, intendedCapture: true, engineRunning: false))
        XCTAssertTrue(recovery.shouldFire(now: 1.65, intendedCapture: true, engineRunning: false))
        XCTAssertFalse(recovery.shouldFire(now: 1.7, intendedCapture: true, engineRunning: false))
        _ = recovery.noteChange(now: 2.0, settle: 0.35)
        XCTAssertFalse(recovery.shouldFire(now: 2.4, intendedCapture: true, engineRunning: true))
    }

    func testStoppedCaptureRejectsQueuedBuffersEvenAfterCaptureRestarts() {
        var gate = AudioLifecycleGate()
        let first = gate.beginCapture()!
        XCTAssertTrue(gate.acceptsCapture(first))
        XCTAssertNil(gate.beginCapture())
        gate.stopCapture()
        XCTAssertFalse(gate.acceptsCapture(first))
        let second = gate.beginCapture()!
        XCTAssertFalse(gate.acceptsCapture(first))
        XCTAssertTrue(gate.acceptsCapture(second))
    }

    func testClearedPlaybackRejectsOldCompletionAndMeterCallbacks() {
        var gate = AudioLifecycleGate()
        let before = gate.playbackGeneration
        gate.clearPlayback()
        XCTAssertFalse(gate.acceptsPlayback(before))
        XCTAssertTrue(gate.acceptsPlayback(gate.playbackGeneration))
    }

    func testInterruptedItemCannotResumeFromLateAudioButNewItemCan() {
        var admission = PlaybackAdmission()
        XCTAssertTrue(admission.accepts(item: "old-answer"))
        admission.block(items: ["old-answer"])
        XCTAssertFalse(admission.accepts(item: "old-answer"))
        XCTAssertTrue(admission.accepts(item: "new-answer"))
        admission.reset()
        XCTAssertTrue(admission.accepts(item: "old-answer"))
    }

    func testLocalSpeechInterruptsOncePerUtteranceOnlyWhenAudioIsPending() {
        var gate = SpeechInterruptionGate()
        XCTAssertFalse(gate.observe(speech: true, hasPlayback: false))
        XCTAssertTrue(gate.observe(speech: true, hasPlayback: true))
        XCTAssertFalse(gate.observe(speech: true, hasPlayback: true))
        XCTAssertFalse(gate.observe(speech: false, hasPlayback: true))
        XCTAssertTrue(gate.observe(speech: true, hasPlayback: true))
    }

    func testApprovalCannotRestoreAnUnknownOrChangedApplication() {
        XCTAssertEqual(FocusRestorationPolicy.decision(orbitPID: 1, currentPID: 1, originalPID: nil), .reject)
        XCTAssertEqual(FocusRestorationPolicy.decision(orbitPID: 1, currentPID: 1, originalPID: 2), .restore(2))
        XCTAssertEqual(FocusRestorationPolicy.decision(orbitPID: 1, currentPID: 2, originalPID: 2), .ready)
        XCTAssertEqual(FocusRestorationPolicy.decision(orbitPID: 1, currentPID: 3, originalPID: 2), .reject)
        XCTAssertEqual(FocusRestorationPolicy.decision(orbitPID: 1, currentPID: nil, originalPID: 2), .reject)
    }

    func testEveryPreviewFlagStaysOfflineIncludingUnknownScenario() {
        XCTAssertNil(PreviewScenario.parse(arguments: ["Orbit"]))
        XCTAssertEqual(PreviewScenario.parse(arguments: ["Orbit", "--preview"]), .listening)
        XCTAssertEqual(PreviewScenario.parse(arguments: ["Orbit", "--preview", "speaking"]), .speaking)
        XCTAssertEqual(PreviewScenario.parse(arguments: ["Orbit", "--preview=approval"]), .approval)
        XCTAssertEqual(PreviewScenario.parse(arguments: ["Orbit", "--preview=typo"]), .listening)
    }

    func testDetailsRemainExpandedForPendingApprovalAfterManualCollapse() {
        // Approvals always force expand.
        XCTAssertTrue(PanelPresentation.isExpanded(requested: false, hasApproval: true, hasIssue: false, hasTasks: false))
        // Issues/tasks no longer pin the panel — chevron can minimize (auto-open sets requested=true separately).
        XCTAssertFalse(PanelPresentation.isExpanded(requested: false, hasApproval: false, hasIssue: true, hasTasks: false))
        XCTAssertFalse(PanelPresentation.isExpanded(requested: false, hasApproval: false, hasIssue: false, hasTasks: true))
        XCTAssertTrue(PanelPresentation.isExpanded(requested: true, hasApproval: false, hasIssue: true, hasTasks: false))
        XCTAssertFalse(PanelPresentation.isExpanded(requested: false, hasApproval: false, hasIssue: false, hasTasks: false))
    }

    func testApprovedActionStaysBoundToItsOriginalApplicationAndTask() {
        var focus = ApprovedActionFocus()
        focus.bind(actionID: "approved", taskID: "task", processID: 22)
        XCTAssertTrue(focus.allows(actionID: "approved", currentPID: 22))
        XCTAssertFalse(focus.allows(actionID: "approved", currentPID: 33))
        XCTAssertFalse(focus.allows(actionID: "different-action", currentPID: 22))
        focus.revoke(taskIDs: ["other-task"])
        XCTAssertTrue(focus.allows(actionID: "approved", currentPID: 22))
        focus.revoke(taskIDs: ["task"])
        XCTAssertFalse(focus.allows(actionID: "approved", currentPID: 22))
    }

    func testLaunchReceiptRequiresTheRequestedApplicationAndCurrentAuthority() {
        XCTAssertTrue(ApplicationLaunchPolicy.accepts(requested: "com.google.Chrome", actual: "com.google.Chrome", authorityValid: true))
        XCTAssertFalse(ApplicationLaunchPolicy.accepts(requested: "com.google.Chrome", actual: "com.apple.Notes", authorityValid: true))
        XCTAssertFalse(ApplicationLaunchPolicy.accepts(requested: "com.google.Chrome", actual: nil, authorityValid: true))
        XCTAssertFalse(ApplicationLaunchPolicy.accepts(requested: "com.google.Chrome", actual: "com.google.Chrome", authorityValid: false))
    }

    func testAnApprovedActionCannotBeReboundToAnotherApplication() {
        var focus = ApprovedActionFocus()
        focus.bind(actionID: "immutable-action", taskID: "task", processID: 22)
        focus.bind(actionID: "immutable-action", taskID: "task", processID: 33)
        XCTAssertTrue(focus.allows(actionID: "immutable-action", currentPID: 22))
        XCTAssertFalse(focus.allows(actionID: "immutable-action", currentPID: 33))
    }

    func testAudioCapturedBeforeStreamingPauseCannotLeakIntoResumedStream() {
        var gate = AudioLifecycleGate()
        let capture = gate.beginCapture()!
        gate.setStreaming(true)
        let stream = gate.streamGeneration, session = gate.sessionGeneration
        XCTAssertTrue(gate.acceptsStream(capture: capture, stream: stream, session: session))
        gate.setStreaming(false); gate.setStreaming(true)
        XCTAssertFalse(gate.acceptsStream(capture: capture, stream: stream, session: session))
        XCTAssertTrue(gate.acceptsStream(capture: capture, stream: gate.streamGeneration, session: session))
    }

    func testHeardAudioMayFinishReportingAfterClearButNeverInANewSession() {
        var gate = AudioLifecycleGate()
        let session = gate.sessionGeneration
        gate.clearPlayback()
        XCTAssertTrue(gate.acceptsSession(session))
        gate.resetSession()
        XCTAssertFalse(gate.acceptsSession(session))
    }

    func testDelayedInputCopyCannotCrossAStreamingPauseOrNewSession() {
        for resetSession in [false, true] {
            let queue = DispatchQueue(label: "test.audio.handoff")
            let handoff = AudioCaptureHandoff(queue: queue)
            var gate = AudioLifecycleGate()
            let capture = gate.beginCapture()!
            gate.setStreaming(true); handoff.update(gate, at: 100)
            let copying = DispatchSemaphore(value: 0), resume = DispatchSemaphore(value: 0), submitted = DispatchSemaphore(value: 0)
            var delivered: [String] = []
            DispatchQueue.global().async {
                handoff.submit(capture: capture, capturedAt: 110, copy: {
                    copying.signal(); _ = resume.wait(timeout: .now() + 2)
                    return "stale PCM"
                }, consume: { buffer, _ in delivered.append(buffer) })
                submitted.signal()
            }
            XCTAssertEqual(copying.wait(timeout: .now() + 2), .success)
            if resetSession { gate.resetSession() } else { gate.setStreaming(false) }
            gate.setStreaming(true); handoff.update(gate, at: 120)
            resume.signal()
            XCTAssertEqual(submitted.wait(timeout: .now() + 2), .success)
            queue.sync { XCTAssertEqual(delivered, []) }
            handoff.submit(capture: capture, capturedAt: 130, copy: { "fresh PCM" }, consume: { buffer, identity in
                XCTAssertEqual(identity.session, resetSession ? 1 : 0)
                delivered.append(buffer)
            })
            queue.sync { XCTAssertEqual(delivered, ["fresh PCM"]) }
        }
    }

    func testLateInputCallbackMustUseAudioCaptureTimeBeforeReadingNewLifecycle() {
        let queue = DispatchQueue(label: "test.audio.capture-time")
        let handoff = AudioCaptureHandoff(queue: queue)
        var gate = AudioLifecycleGate()
        let capture = gate.beginCapture()!
        gate.setStreaming(true); handoff.update(gate, at: 100)
        gate.resetSession(); gate.setStreaming(true); handoff.update(gate, at: 120)
        var delivered: [String] = []
        for capturedAt: UInt64? in [110, nil, 130] {
            handoff.submit(capture: capture, capturedAt: capturedAt, copy: { "PCM" }, consume: { buffer, _ in delivered.append(buffer) })
        }
        queue.sync { XCTAssertEqual(delivered, ["PCM"]) }
    }

    @MainActor func testFinalControlFrameCompletesBeforeDetachedSocketCloses() async {
        let closed = expectation(description: "old socket closes")
        var events: [String] = []
        var oldSocketOpen = true
        _ = FinalFrameDrain(send: {
            guard oldSocketOpen else { throw NSError(domain: "closed", code: 1) }
            events.append("final control")
            await Task.yield()
            events.append("send completed")
        }, close: {
            oldSocketOpen = false; events.append("close"); closed.fulfill()
        })
        await fulfillment(of: [closed], timeout: 1)
        XCTAssertEqual(events, ["final control", "send completed", "close"])
    }

    @MainActor func testFinalControlTimeoutClosesOnceEvenWhenSendIgnoresTaskCancellation() async {
        let closed = expectation(description: "bounded close")
        var release: CheckedContinuation<Void, Never>?
        var events: [String] = []
        var open = true
        _ = FinalFrameDrain(timeoutNanoseconds: 10_000_000, send: {
            guard open else { throw NSError(domain: "closed", code: 1) }
            events.append("send")
            await withCheckedContinuation { release = $0 }
            events.append("late completion")
        }, close: {
            open = false; events.append("close"); release?.resume(); release = nil; closed.fulfill()
        })
        XCTAssertTrue(open, "Disconnect must not discard the final frame synchronously")
        await fulfillment(of: [closed], timeout: 1)
        await Task.yield()
        XCTAssertEqual(events, ["send", "close", "late completion"])
    }

    @MainActor func testFailedFinalSendCannotCloseTheReconnectedSocket() async {
        let closed = expectation(description: "failed old socket closes")
        var connections = ["old": true, "new": true]
        var sends = 0, closes = 0
        _ = FinalFrameDrain(send: {
            sends += 1
            throw NSError(domain: "disconnected", code: 1)
        }, close: {
            connections["old"] = false; closes += 1; closed.fulfill()
        })
        await fulfillment(of: [closed], timeout: 1)
        await Task.yield()
        XCTAssertEqual(sends, 1)
        XCTAssertEqual(closes, 1)
        XCTAssertEqual(connections, ["old": false, "new": true])
    }
    func testListeningStatusRequiresAuthorizedCaptureAndLiveMeter() {
        XCTAssertEqual(
            ListeningStatusPolicy.label(microphoneAuthorized: false, speechAuthorized: true, captureReady: true, recentMeterPeak: 0.5),
            ListeningStatusPolicy.needsMicrophone
        )
        XCTAssertEqual(
            ListeningStatusPolicy.label(microphoneAuthorized: true, speechAuthorized: false, captureReady: true, recentMeterPeak: 0.5),
            ListeningStatusPolicy.needsSpeech
        )
        XCTAssertEqual(
            ListeningStatusPolicy.label(microphoneAuthorized: true, speechAuthorized: true, captureReady: false, recentMeterPeak: 0.5),
            ListeningStatusPolicy.preparingSpeech
        )
        XCTAssertEqual(
            ListeningStatusPolicy.label(microphoneAuthorized: true, speechAuthorized: true, captureReady: true, recentMeterPeak: 0),
            ListeningStatusPolicy.ready
        )
        XCTAssertEqual(
            ListeningStatusPolicy.label(microphoneAuthorized: true, speechAuthorized: true, captureReady: true, recentMeterPeak: ListeningStatusPolicy.liveMeterFloor),
            ListeningStatusPolicy.listening
        )
        XCTAssertTrue(ListeningStatusPolicy.isPermissionState(ListeningStatusPolicy.needsSpeech))
        XCTAssertFalse(ListeningStatusPolicy.isPermissionState(ListeningStatusPolicy.ready))
    }

    func testPermissionSnapshotCacheReadsOnlyOnRefresh() {
        var reads = 0
        let cache = PermissionSnapshotCache {
            reads += 1
            return PermissionSnapshot(microphoneAuthorized: reads == 1, speechAuthorized: true)
        }
        XCTAssertEqual(reads, 1)
        XCTAssertTrue(cache.current.microphoneAuthorized)
        _ = cache.current
        _ = cache.current
        XCTAssertEqual(reads, 1)
        cache.refresh()
        XCTAssertEqual(reads, 2)
        XCTAssertFalse(cache.current.microphoneAuthorized)
        _ = cache.current
        XCTAssertEqual(reads, 2)
    }

    func testPolicyVisibilityGateEmitsOnlyOnTransitions() {
        var gate = PolicyVisibilityGate()
        XCTAssertNil(gate.observe(shouldReveal: false))
        XCTAssertNil(gate.observe(shouldReveal: false))
        XCTAssertEqual(gate.observe(shouldReveal: true), true)
        XCTAssertNil(gate.observe(shouldReveal: true))
        XCTAssertNil(gate.observe(shouldReveal: true))
        XCTAssertEqual(gate.observe(shouldReveal: false), false)
        XCTAssertNil(gate.observe(shouldReveal: false))
    }

    func testPolicyVisibilityGateSyncPreventsRedundantReveal() {
        var gate = PolicyVisibilityGate()
        XCTAssertNil(gate.observe(shouldReveal: false))
        gate.sync(revealed: true)
        XCTAssertNil(gate.observe(shouldReveal: true))
    }

    func testListeningPresentationSeparatesStandbyFromWake() {
        for status in [ListeningStatusPolicy.needsMicrophone, ListeningStatusPolicy.needsSpeech] {
            XCTAssertTrue(ListeningStatusPolicy.shouldReveal(status: status, explicitlyAwake: false))
            XCTAssertTrue(ListeningStatusPolicy.shouldReveal(status: status, explicitlyAwake: true))
        }
        for status in [ListeningStatusPolicy.preparingSpeech, ListeningStatusPolicy.ready, ListeningStatusPolicy.listening] {
            XCTAssertFalse(ListeningStatusPolicy.shouldReveal(status: status, explicitlyAwake: false))
            XCTAssertTrue(ListeningStatusPolicy.shouldReveal(status: status, explicitlyAwake: true))
        }
    }

    func testSampleAudioPeaksDriveListeningStatus() throws {
        // Fixture: short 440Hz tone whose frame RMS exceeds the live-meter floor.
        let url = Bundle.module.url(forResource: "sample_speech", withExtension: "wav")
            ?? URL(fileURLWithPath: #filePath)
                .deletingLastPathComponent()
                .appendingPathComponent("Fixtures/sample_speech.wav")
        XCTAssertTrue(FileManager.default.fileExists(atPath: url.path), "missing sample_speech.wav at \(url.path)")
        let peaks = try SampleAudioMeter.rmsPeaks(wavURL: url, hop: 1024)
        XCTAssertFalse(peaks.isEmpty)
        XCTAssertTrue(peaks.contains { $0 >= ListeningStatusPolicy.liveMeterFloor }, "sample audio should exceed meter floor; max=\(peaks.max() ?? -1)")
        let quiet = ListeningStatusPolicy.label(microphoneAuthorized: true, speechAuthorized: true, captureReady: true, recentMeterPeak: 0)
        XCTAssertEqual(quiet, ListeningStatusPolicy.ready)
        let live = ListeningStatusPolicy.label(microphoneAuthorized: true, speechAuthorized: true, captureReady: true, recentMeterPeak: peaks.max() ?? 0)
        XCTAssertEqual(live, ListeningStatusPolicy.listening)
    }

}
