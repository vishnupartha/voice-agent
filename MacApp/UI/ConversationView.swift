import SwiftUI

struct ConversationView: View {
    let turns: [Turn]
    var partialTranscript = ""

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 18) {
                    if turns.isEmpty { suggestions }
                    ForEach(turns) { TurnView(turn: $0).id($0.id) }
                    if !partialTranscript.isEmpty {
                        // Live partial STT (spec §75); only the final transcript is kept.
                        TurnView(turn: Turn(role: .user, text: partialTranscript, isStreaming: true))
                            .opacity(0.6)
                    }
                    Color.clear.frame(height: 1).id("bottom")
                }
                .padding()
            }
            .onChange(of: turns.last?.text) { proxy.scrollTo("bottom", anchor: .bottom) }
            .onChange(of: partialTranscript) { proxy.scrollTo("bottom", anchor: .bottom) }
        }
    }

    private var suggestions: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Try asking").font(.headline)
            ForEach(["Explain something to me.", "What's on my calendar?",
                     "What's important in my email?", "Find me something interesting this week."],
                    id: \.self) { Text("“\($0)”").foregroundStyle(.secondary) }
        }
        .padding(.top, 40)
        .frame(maxWidth: .infinity)
    }
}

private struct TurnView: View {
    let turn: Turn

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(turn.role == .user ? "You" : "Assistant")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            if !turn.tools.isEmpty {
                ToolStatusView(tools: turn.tools)
            }
            if turn.text.isEmpty && turn.isStreaming {
                if turn.tools.isEmpty { ProgressView().controlSize(.small) }
            } else {
                Text(turn.text).textSelection(.enabled)
            }
            if let draft = turn.draft {
                DraftCard(draft: draft)
            }
            if turn.cancelled {
                Text("Stopped").font(.caption).foregroundStyle(.secondary)
            }
            if let m = turn.metrics, let ft = m.firstTokenMs {
                Text("\(ft) ms to first token · \(m.tokens) tokens · \(m.totalMs) ms")
                    .font(.caption2).foregroundStyle(.tertiary)
            }
        }
    }
}
