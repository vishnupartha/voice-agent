import SwiftUI
import UniformTypeIdentifiers

/// Setup without a terminal: the Gemini key and the Google OAuth client file.
/// Opens by itself on first run (no brain yet).
struct SettingsView: View {
    let state: AppState
    @State private var geminiKey = ""
    @State private var saving = false
    @State private var message: String?
    @State private var choosingGoogleFile = false

    var body: some View {
        Form {
            if let setup = state.setup {
                brainSection(setup)
                googleSection(setup)
            } else {
                Text(state.backend == .ready ? "Loading…" : "Waiting for the backend to start…")
                    .foregroundStyle(.secondary)
            }
            if let message {
                Text(message).font(.callout).foregroundStyle(.red)
            }
        }
        .formStyle(.grouped)
        .frame(width: 520)
        .task { await state.refreshSetup() }
        .fileImporter(isPresented: $choosingGoogleFile, allowedContentTypes: [.json]) { result in
            guard case let .success(url) = result else { return }
            Task { message = await state.uploadGoogleClient(url) }
        }
    }

    private func brainSection(_ setup: SetupStatus) -> some View {
        Section {
            if setup.geminiKey {
                LabeledContent("Gemini API key") {
                    HStack {
                        Label("Saved in Keychain", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                        Button("Remove") { Task { message = await state.removeGeminiKey() } }
                    }
                }
            } else {
                SecureField("Gemini API key", text: $geminiKey, prompt: Text("AIza…"))
                HStack {
                    Link("Get a free key at Google AI Studio", destination: URL(string: "https://aistudio.google.com/apikey")!)
                    Spacer()
                    Button(saving ? "Checking…" : "Save Key") {
                        saving = true
                        Task {
                            message = await state.saveGeminiKey(geminiKey)
                            if message == nil { geminiKey = "" }
                            saving = false
                        }
                    }
                    .disabled(geminiKey.trimmingCharacters(in: .whitespaces).isEmpty || saving)
                    .keyboardShortcut(.defaultAction)
                }
            }
        } header: {
            Text("Brain")
        } footer: {
            Text("Gemini Flash answers questions and uses your tools. It's free with a Google account; your "
                + "questions, and any email or calendar details it reads, are sent to Google. Prefer fully "
                + "local? Download the Qwen3.5 model (see README) and pick it from the Brain menu.")
        }
    }

    private func googleSection(_ setup: SetupStatus) -> some View {
        Section {
            LabeledContent("Status", value: setup.google.connected ? "Connected"
                : setup.google.configured ? "Ready to connect" : "Not set up")
            HStack {
                Button(setup.google.configured ? "Replace Client File…" : "Choose Client File…") {
                    choosingGoogleFile = true
                }
                if setup.google.configured, !setup.google.connected {
                    Button("Connect Google…") { state.connectGoogle() }
                }
                Spacer()
                Link("Setup guide", destination: URL(string: "https://github.com/vishnupartha/voice-agent#connect-google-optional")!)
            }
        } header: {
            Text("Gmail and Calendar (optional)")
        } footer: {
            Text("Google requires each user to create their own free OAuth client. The guide takes about five "
                + "minutes; then choose the downloaded client_secret JSON file here. It's stored in your Keychain.")
        }
    }
}
