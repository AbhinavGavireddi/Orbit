import XCTest
@testable import OrbitCore

final class SafetyTests: XCTestCase {
    func testLocalControlPhrasesAreAnchoredAndPunctuationTolerant() {
        XCTAssertEqual(PhraseMatcher.match("Orbit, wake up!"), .wake)
        XCTAssertEqual(PhraseMatcher.match("ORBIT STOP."), .stop)
        XCTAssertEqual(PhraseMatcher.match("Goodbye Orbit"), .goodbye)
        XCTAssertEqual(PhraseMatcher.match("Finish dictation."), .finishDictation)
        XCTAssertNil(PhraseMatcher.match("My notes say stop spending so much"))
        XCTAssertNil(PhraseMatcher.match("Orbit wake up next Tuesday"))
    }
    func testStandbyKeepsLocalWakeAndGoodbyeClosesCloudCapture() {
        var state = Lifecycle()
        XCTAssertTrue(state.capturesMicrophone)
        XCTAssertFalse(state.streamsMicrophone)
        state.wake()
        XCTAssertTrue(state.awake)
        XCTAssertTrue(state.streamsMicrophone)
        state.goodbye()
        XCTAssertFalse(state.awake)
        XCTAssertTrue(state.capturesMicrophone)
        XCTAssertFalse(state.streamsMicrophone)
    }
    func testMuteStopsAllCaptureWhilePreservingAwakeState() {
        var state = Lifecycle(); state.wake(); state.setMuted(true)
        XCTAssertTrue(state.awake)
        XCTAssertFalse(state.capturesMicrophone)
        XCTAssertFalse(state.streamsMicrophone)
        state.setMuted(false)
        XCTAssertTrue(state.streamsMicrophone)
    }
    func testLockSuspendsAllCaptureAndUnlockRestoresOnlyLocalWake() {
        var state = Lifecycle(); state.wake(); state.suspend(); state.suspend()
        XCTAssertFalse(state.awake)
        XCTAssertFalse(state.capturesMicrophone)
        XCTAssertFalse(state.streamsMicrophone)
        state.resumeLocal()
        XCTAssertTrue(state.capturesMicrophone)
        XCTAssertFalse(state.streamsMicrophone)
        state.wake(); XCTAssertTrue(state.streamsMicrophone)
        state.setMuted(true); state.suspend(); state.resumeLocal()
        XCTAssertFalse(state.capturesMicrophone)
    }
    func makeCommand(session: String = "session", task: String = "task", action: String = "action", fence: Int = 1, expiry: Double = 200) -> Command {
        Command(sessionID: session, taskID: task, actionID: action, fence: fence, expiresAt: expiry)
    }
    func testCommandRejectsUnknownTaskWrongSessionExpiredOrRegressedFence() {
        let guardrail = CommandGuard(); guardrail.activate(sessionID: "session"); guardrail.register(taskID: "task")
        XCTAssertEqual(guardrail.check(makeCommand(session: "old"), now: 100), .reject)
        XCTAssertEqual(guardrail.check(makeCommand(task: "unannounced"), now: 100), .reject)
        XCTAssertEqual(guardrail.check(makeCommand(expiry: 99), now: 100), .reject)
        XCTAssertEqual(guardrail.check(makeCommand(fence: 10), now: 100), .execute)
        XCTAssertEqual(guardrail.check(makeCommand(action: "second", fence: 9), now: 100), .reject)
    }
    func testDuplicateReturnsCachedResultWithoutReexecuting() {
        let guardrail = CommandGuard(); guardrail.activate(sessionID: "session"); guardrail.register(taskID: "task")
        let command = makeCommand(); let result = Data("done".utf8)
        XCTAssertEqual(guardrail.check(command, now: 100), .execute)
        // Also block an in-flight duplicate, before completion.
        XCTAssertEqual(guardrail.check(command, now: 100), .reject)
        guardrail.complete(command, result: result)
        XCTAssertEqual(guardrail.check(command, now: 100), .duplicate(result))
        XCTAssertEqual(guardrail.check(makeCommand(task: "different"), now: 100), .reject)
    }
    func testRevocationIsImmediateAndCannotBeUndoneByStaleTaskEvent() {
        let guardrail = CommandGuard(); guardrail.activate(sessionID: "session"); guardrail.register(taskID: "task")
        guardrail.revoke(taskID: "task"); guardrail.register(taskID: "task")
        XCTAssertEqual(guardrail.check(makeCommand(), now: 100), .reject)
        guardrail.revokeAll(); guardrail.register(taskID: "new")
        XCTAssertEqual(guardrail.check(makeCommand(task: "new"), now: 100), .reject)
        guardrail.activate(sessionID: "new-session"); guardrail.register(taskID: "new")
        XCTAssertEqual(guardrail.check(makeCommand(session: "new-session", task: "new"), now: 100), .execute)
    }
    func testResearchOnlyPhysicalInputLeavesItsTaskAndSessionActive() {
        let guardrail = CommandGuard(); guardrail.activate(sessionID: "session")
        guardrail.register(taskID: "research", kind: .research)
        XCTAssertTrue(guardrail.desktopTaskIDs.isEmpty)
        XCTAssertTrue(guardrail.revokeDesktopTasks().isEmpty)
        XCTAssertTrue(guardrail.isTaskActive("research"))
        XCTAssertEqual(guardrail.check(makeCommand(task: "research"), now: 100), .execute)
    }
    func testPhysicalTakeoverRevokesOnlyDesktopTasksAndRejectsStaleReregistration() {
        let guardrail = CommandGuard(); guardrail.activate(sessionID: "session")
        guardrail.register(taskID: "research", kind: .research)
        guardrail.register(taskID: "automation", kind: .automation)
        guardrail.register(taskID: "dictation", kind: .dictation)
        XCTAssertEqual(guardrail.desktopTaskIDs, ["automation", "dictation"])
        XCTAssertEqual(guardrail.revokeDesktopTasks(), ["automation", "dictation"])
        guardrail.register(taskID: "automation", kind: .automation)
        guardrail.register(taskID: "dictation", kind: .dictation)
        XCTAssertFalse(guardrail.isTaskActive("automation"))
        XCTAssertFalse(guardrail.isTaskActive("dictation"))
        XCTAssertTrue(guardrail.isTaskActive("research"))
        XCTAssertTrue(guardrail.desktopTaskIDs.isEmpty)
        XCTAssertEqual(guardrail.check(makeCommand(task: "research"), now: 100), .execute)
        guardrail.register(taskID: "new-automation", kind: .automation)
        XCTAssertEqual(guardrail.desktopTaskIDs, ["new-automation"])
    }
    func testMutationsRequireApprovalAndUnknownActionsNeverExecute() {
        XCTAssertTrue(ActionPolicy.validateComputer(actions: [["type": "move", "x": 1, "y": 1]], approved: false))
        XCTAssertFalse(ActionPolicy.validateComputer(actions: [["type": "click", "x": 1, "y": 1]], approved: false))
        XCTAssertTrue(ActionPolicy.validateComputer(actions: [["type": "click", "x": 1, "y": 1]], approved: true))
        XCTAssertFalse(ActionPolicy.validateComputer(actions: [["type": "shell", "command": "ls"]], approved: true))
        XCTAssertFalse(ActionPolicy.validateComputer(actions: Array(repeating: ["type": "wait", "seconds": 1], count: 9), approved: true))
        XCTAssertFalse(ActionPolicy.validateComputer(actions: [["type": "wait", "seconds": 600]], approved: true))
        XCTAssertFalse(ActionPolicy.validateComputer(actions: [["type": "wait", "seconds": "invalid"]], approved: true))
        XCTAssertFalse(ActionPolicy.validateComputer(actions: [["type": "wait", "seconds": true]], approved: true))
    }
    func testAllowlistRejectsURLConfusionAndArtifactTraversal() {
        XCTAssertNotNil(ActionPolicy.spotifyURL("https://open.spotify.com/track/abc"))
        for url in ["http://open.spotify.com", "https://open.spotify.com.evil.com", "https://user@open.spotify.com", "file:///tmp/x"] {
            XCTAssertNil(ActionPolicy.spotifyURL(url))
        }
        XCTAssertEqual(ActionPolicy.artifactFilename("report.pdf"), "report.pdf")
        for filename in ["../secret", "/etc/passwd", ".", "..", "a/b.pdf", "run.command", "evil.app", "x\\y.pdf"] {
            XCTAssertNil(ActionPolicy.artifactFilename(filename))
        }
    }
    func testRetinaMappingUsesPixelRatioAndQuartzOriginWithoutFlippingY() {
        let map = PixelMapping(width: 3024, height: 1964, originX: -1512, originY: 40, pointWidth: 1512, pointHeight: 982)
        let result = map.point(x: 1512, y: 982)
        XCTAssertEqual(result?.0, -756)
        XCTAssertEqual(result?.1, 531)
        XCTAssertNil(map.point(x: -1, y: 0))
        XCTAssertNil(map.point(x: 3024, y: 0))
        XCTAssertNil(map.point(x: .nan, y: 0))
    }
    func testPlaybackGapsCannotBeReportedAsHeardAudio() {
        var clock = PlaybackClock()
        let first = clock.enqueue(item: "reply", index: 0, frames: 2400, renderedFrame: nil)
        XCTAssertEqual(first.start, 0)
        XCTAssertEqual(clock.playedMilliseconds(first, renderedFrame: 1440, latencyFrames: 240), 50)
        XCTAssertEqual(clock.playedMilliseconds(first, renderedFrame: 5000, latencyFrames: 0), 100)
        clock.completed(first)
        // 400 ms of output-clock silence must not make this new audio look played.
        let afterGap = clock.enqueue(item: "reply", index: 0, frames: 2400, renderedFrame: 12000)
        XCTAssertGreaterThanOrEqual(afterGap.start, 12000)
        XCTAssertEqual(clock.playedMilliseconds(afterGap, renderedFrame: 12000, latencyFrames: 240), 100)
        XCTAssertEqual(clock.playedMilliseconds(afterGap, renderedFrame: afterGap.start + 1440, latencyFrames: 240), 150)
        clock.clear()
        let fresh = clock.enqueue(item: "next", index: 0, frames: 2400, renderedFrame: nil)
        XCTAssertEqual(fresh.start, 0)
        XCTAssertEqual(clock.playedMilliseconds(fresh, renderedFrame: 100, latencyFrames: 240), 0)
    }
    func testDelayedSpokenOrTypedYesCannotAuthorizeANewConfirmation() {
        let fresh = ApprovalIdentity(taskID: "task", actionID: "fresh-action", version: 3)
        // The recognizer delivers an earlier yes after this confirmation has appeared.
        XCTAssertFalse(ApprovalPolicy.allows(source: .speech, identity: fresh, current: [fresh]))
        XCTAssertFalse(ApprovalPolicy.allows(source: .typed, identity: fresh, current: [fresh]))
        XCTAssertTrue(ApprovalPolicy.allows(source: .button, identity: fresh, current: [fresh]))
        let stale = ApprovalIdentity(taskID: "task", actionID: "fresh-action", version: 2)
        XCTAssertFalse(ApprovalPolicy.allows(source: .button, identity: stale, current: [fresh]))
        XCTAssertFalse(ApprovalPolicy.allows(source: .button, identity: fresh, current: []))
    }
    func testListenTapIsRequiredOnlyWhenAnActionPostsInput() {
        XCTAssertFalse(TakeoverPolicy.requiresListenAccess(type: "open_app"))
        XCTAssertFalse(TakeoverPolicy.requiresListenAccess(type: "open_url"))
        XCTAssertFalse(TakeoverPolicy.requiresListenAccess(type: "notes_create"))
        XCTAssertFalse(TakeoverPolicy.requiresListenAccess(type: "dictation_start"))
        XCTAssertFalse(TakeoverPolicy.requiresListenAccess(type: "screenshot"))
        XCTAssertFalse(TakeoverPolicy.requiresListenAccess(type: "ax_snapshot"))
        XCTAssertFalse(TakeoverPolicy.requiresListenAccess(type: "ax_perform"))
        XCTAssertFalse(TakeoverPolicy.requiresListenAccess(type: "computer", actions: [["type": "screenshot"]]))
        XCTAssertFalse(TakeoverPolicy.requiresListenAccess(type: "computer", actions: [["type": "wait"], ["type": "screenshot"]]))
        XCTAssertTrue(TakeoverPolicy.requiresListenAccess(type: "computer", actions: [["type": "screenshot"], ["type": "click"]]))
        XCTAssertTrue(TakeoverPolicy.requiresListenAccess(type: "computer", actions: [["type": "type"]]))
        XCTAssertTrue(TakeoverPolicy.requiresListenAccess(type: "computer"))
        XCTAssertTrue(TakeoverPolicy.requiresListenAccess(type: "unknown"))
    }
    func testHeldDragAndModifierInputRevokesDesktopControlButNotResearch() {
        let inputs: [CGEventType] = [.leftMouseDragged, .rightMouseDragged, .otherMouseDragged, .flagsChanged]
        for input in inputs {
            XCTAssertNotEqual(TakeoverPolicy.eventMask & (CGEventMask(1) << input.rawValue), 0)
            XCTAssertTrue(TakeoverPolicy.shouldRevoke(type: input, desktopTasks: ["task"]))
            XCTAssertFalse(TakeoverPolicy.shouldRevoke(type: input, desktopTasks: []))
            XCTAssertFalse(TakeoverPolicy.shouldRevoke(type: input, desktopTasks: ["task"], synthetic: true))
            XCTAssertFalse(TakeoverPolicy.shouldRevoke(type: input, desktopTasks: ["task"], ownMenuOpen: true))
        }
    }
    func testCursorOverFaceDoesNotExemptKeyboardInAnotherApplication() {
        for type in [CGEventType.keyDown, .keyUp, .flagsChanged] {
            XCTAssertTrue(TakeoverPolicy.shouldRevoke(type: type, desktopTasks: ["task"], orbitOwnsKeyboard: false, pointerInsideOrbit: true))
            XCTAssertFalse(TakeoverPolicy.shouldRevoke(type: type, desktopTasks: ["task"], orbitOwnsKeyboard: true, pointerInsideOrbit: false))
        }
        XCTAssertFalse(TakeoverPolicy.shouldRevoke(type: .leftMouseDown, desktopTasks: ["task"], pointerInsideOrbit: true))
    }
    func testUserCanMoveToApprovalOnlyWhileEveryDesktopTaskIsPausedForApproval() {
        XCTAssertFalse(TakeoverPolicy.shouldRevoke(type: .mouseMoved, desktopTasks: ["paused"], awaitingApproval: ["paused"]))
        XCTAssertTrue(TakeoverPolicy.shouldRevoke(type: .mouseMoved, desktopTasks: ["paused", "running"], awaitingApproval: ["paused"]))
        XCTAssertTrue(TakeoverPolicy.shouldRevoke(type: .mouseMoved, desktopTasks: ["running"]))
        for type in [CGEventType.leftMouseDown, .leftMouseDragged, .scrollWheel, .keyDown, .flagsChanged] {
            XCTAssertTrue(TakeoverPolicy.shouldRevoke(type: type, desktopTasks: ["paused"], awaitingApproval: ["paused"]))
        }
    }
    func testQueuedDesktopJobDoesNotBlockMovingToCurrentApproval() {
        let guardrail = CommandGuard(); guardrail.activate(sessionID: "session")
        guardrail.register(taskID: "paused", kind: .automation, status: .needsConfirmation, version: 3)
        guardrail.register(taskID: "queued", kind: .dictation, status: .queued, version: 1)
        XCTAssertEqual(guardrail.runnableDesktopTaskIDs, ["paused"])
        XCTAssertFalse(TakeoverPolicy.shouldRevoke(type: .mouseMoved, desktopTasks: guardrail.desktopTaskIDs,
            runnableDesktopTasks: guardrail.runnableDesktopTaskIDs, awaitingApproval: ["paused"]))
        XCTAssertTrue(TakeoverPolicy.shouldRevoke(type: .keyDown, desktopTasks: guardrail.desktopTaskIDs,
            runnableDesktopTasks: guardrail.runnableDesktopTaskIDs, awaitingApproval: ["paused"]))
        // A queued job that becomes runnable removes the pointer-only exemption.
        guardrail.register(taskID: "queued", kind: .dictation, status: .running, version: 2)
        XCTAssertEqual(guardrail.runnableDesktopTaskIDs, ["paused", "queued"])
        XCTAssertFalse(guardrail.register(taskID: "queued", kind: .dictation, status: .queued, version: 1))
        XCTAssertTrue(TakeoverPolicy.shouldRevoke(type: .mouseMoved, desktopTasks: guardrail.desktopTaskIDs,
            runnableDesktopTasks: guardrail.runnableDesktopTaskIDs, awaitingApproval: ["paused"]))
        XCTAssertEqual(guardrail.revokeDesktopTasks(), ["paused", "queued"])
    }
}
