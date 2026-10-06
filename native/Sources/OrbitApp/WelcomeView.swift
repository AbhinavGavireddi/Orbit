import SwiftUI

extension Notification.Name {
    static let orbitReplaceKeys = Notification.Name("orbit.replaceKeys")
}

@MainActor final class SetupState: ObservableObject {
    @Published var starting = false
    @Published var failure = ""
}

struct WelcomeView: View {
    @ObservedObject var setup: SetupState
    var onStart: (String, String) -> Void
    @State private var openai = ""
    @State private var jev = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Set up Orbit").font(.system(size: 28, weight: .medium, design: .rounded))
            Text("Paste your OpenAI key. Jev is optional and adds advisory action-risk assessment. They stay in this Mac’s keychain. macOS will then ask for the microphone, speech recognition, Accessibility, Screen Recording, and Input Monitoring. Allow each one. If a Settings pane opens, switch Orbit on. You do this once for this copy of Orbit.")
                .foregroundStyle(.secondary)
            if setup.starting {
                Text("Starting Orbit on this Mac…").font(.callout)
            }
            if !setup.failure.isEmpty {
                Text(setup.failure).font(.callout).textSelection(.enabled)
                    .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                    .background(.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
            }
            SecureField("OpenAI API key", text: $openai).textFieldStyle(.roundedBorder)
            SecureField("Jev API key (optional)", text: $jev).textFieldStyle(.roundedBorder)
            Button(setup.starting ? "Starting…" : "Start Orbit") { onStart(openai, jev) }
                .disabled(setup.starting || openai.trimmingCharacters(in: .whitespacesAndNewlines).count < 8)
                .keyboardShortcut(.defaultAction)
        }
        .padding(28)
        .frame(width: 540)
    }
}
