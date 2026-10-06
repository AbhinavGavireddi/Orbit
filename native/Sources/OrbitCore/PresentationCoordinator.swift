import Foundation

public enum PresentationCoordinator {
    public static func label(conversation: String, approval: Bool, speaking: Bool, tasks: Int) -> String {
        if approval { return "Your approval" }
        if speaking { return "Speaking" }
        if tasks > 0 && conversation == "Listening" { return "Listening · working" }
        return conversation
    }
}
