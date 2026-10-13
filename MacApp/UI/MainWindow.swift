import SwiftUI

struct MainWindow: View {
    let state: AppState
    @State private var draft = ""
    @FocusState private var inputFocused: Bool

    var body: some View {
        VStack(spacing: 0) {
            StatusBanner(state: state)
            ConversationView(turns: state.turns, partialTranscript: state.partialTranscript)
            if let error = state.lastError {
                Text(error)
                    .font(.callout)
                    .foregroundStyle(.red)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal)
                    .padding(.bottom, 6)
            }
            Divider()
            inputBar
        }
        .frame(minWidth: 480, minHeight: 420)
        .toolbar {
            ToolbarItem {
                Button("New Conversation", systemImage: "square.and.pencil") { state.newConversation() }
            }
        }
        .onAppear { inputFocused = true }
        .sheet(item: Binding(get: { state.pendingApproval }, set: { if $0 == nil { state.answerApproval(false) } })) {
            ApprovalView(approval: $0) { state.answerApproval($0) }
        }
    }

    private var inputBar: some View {
        HStack(spacing: 8) {
            TextField("Message Personal Agent", text: $draft, axis: .vertical)
                .textFieldStyle(.plain)
                .lineLimit(1 ... 6)
                .focused($inputFocused)
                .onSubmit(submit)
            Button(
                state.voice == .off ? "Talk" : "Stop Listening",
                systemImage: state.voice == .off ? "mic.circle" : "mic.circle.fill"
            ) { state.toggleVoice() }
                .labelStyle(.iconOnly)
                .font(.title2)
                .foregroundStyle(state.voice == .off ? Color.secondary : Color.accentColor)
                .disabled(state.backend != .ready)
                .help("Talk to the assistant (⌥Space)")
            if state.phase == .idle {
                Button("Send", systemImage: "arrow.up.circle.fill", action: submit)
                    .labelStyle(.iconOnly)
                    .font(.title2)
                    .disabled(draft.trimmingCharacters(in: .whitespaces).isEmpty || state.backend != .ready)
                    .keyboardShortcut(.return, modifiers: .command)
            } else {
                Button("Stop", systemImage: "stop.circle.fill") { state.cancel() }
                    .labelStyle(.iconOnly)
                    .font(.title2)
                    .keyboardShortcut(.escape, modifiers: [])
            }
        }
        .buttonStyle(.plain)
        .padding(12)
    }

    private func submit() {
        state.send(draft)
        draft = ""
    }
}

private struct StatusBanner: View {
    let state: AppState

    var body: some View {
        switch state.backend {
        case .ready:
            EmptyView()
        case .starting:
            banner(Label("Starting backend…", systemImage: "hourglass"), tint: .secondary)
        case let .unavailable(reason):
            banner(
                HStack {
                    Label("Backend unavailable: \(reason)", systemImage: "exclamationmark.triangle.fill")
                    Spacer()
                    Button("Retry") { Task { await state.start() } }
                },
                tint: .orange
            )
        }
    }

    private func banner(_ content: some View, tint: Color) -> some View {
        content
            .font(.callout)
            .foregroundStyle(tint)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(10)
            .background(.quaternary.opacity(0.5))
    }
}
