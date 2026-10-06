import XCTest
@testable import OrbitCore

final class PresentationTests: XCTestCase {
    func testApprovalStaysVisibleWhileConversationListens() {
        XCTAssertEqual(PresentationCoordinator.label(conversation: "Listening", approval: true, speaking: false, tasks: 1), "Your approval")
        XCTAssertEqual(PresentationCoordinator.label(conversation: "Listening", approval: false, speaking: false, tasks: 1), "Listening · working")
        XCTAssertEqual(PresentationCoordinator.label(conversation: "Listening", approval: false, speaking: true, tasks: 1), "Speaking")
    }
    func testGenericGoalsParticipateInTakeoverAndCancellation() {
        let guardrail = CommandGuard()
        guardrail.activate(sessionID: "session")
        guard let kind = TaskKind(rawValue: "goal") else { return XCTFail("Generic goal kind is missing") }
        XCTAssertTrue(guardrail.register(taskID: "goal", kind: kind))
        XCTAssertEqual(guardrail.revokeDesktopTasks(), Set(["goal"]))
        XCTAssertFalse(guardrail.isTaskActive("goal"))
    }
}
