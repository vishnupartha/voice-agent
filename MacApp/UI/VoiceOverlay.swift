import AppKit
import SwiftUI

/// The floating voice window that appears on Option+Space (spec §14). It is a
/// non-activating panel, so it never steals focus from the app the user is in.
@MainActor
final class VoiceOverlayController {
    private var panel: NSPanel?
    private let state: AppState

    init(state: AppState) {
        self.state = state
    }

    func show() {
        let panel = panel ?? makePanel()
        self.panel = panel
        if let screen = NSScreen.main {
            let frame = screen.visibleFrame
            panel.setFrameTopLeftPoint(NSPoint(x: frame.midX - panel.frame.width / 2, y: frame.maxY - 24))
        }
        panel.orderFrontRegardless()
    }

    func hide() {
        panel?.orderOut(nil)
    }

    private func makePanel() -> NSPanel {
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 440, height: 150),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isMovableByWindowBackground = true
        panel.hidesOnDeactivate = false
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        let host = NSHostingView(rootView: VoiceOverlay(state: state))
        host.sizingOptions = [.preferredContentSize]
        panel.contentView = host
        return panel
    }
}

struct VoiceOverlay: View {
    let state: AppState

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            VoiceOrb(phase: state.voice)
                .frame(width: 36, height: 36)
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text(title).font(.headline)
                    Spacer()
                    if state.voice == .listening || state.voice == .hearing {
                        MicLevelMeter(level: state.micLevel)
                    }
                }
                if let line = transcriptLine {
                    Text(line)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
                if let reply = replyLine {
                    Text(reply)
                        .font(.callout)
                        .lineLimit(3)
                }
                if let approval = state.pendingApproval {
                    OverlayApproval(approval: approval) { state.answerApproval($0) }
                }
                if let error = state.lastError {
                    Text(error).font(.caption).foregroundStyle(.red).lineLimit(2)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            Button("Close", systemImage: "xmark") { state.stopVoice() }
                .labelStyle(.iconOnly)
                .buttonStyle(.borderless)
                .help("Stop listening (⌥Space)")
        }
        .padding(16)
        .frame(width: 440)
        .glassEffect(in: .rect(cornerRadius: 20))
    }

    private var title: String {
        switch state.voice {
        case .off: "Personal Agent"
        case .listening: "Listening…"
        case .hearing: "Hearing you…"
        case .processing: "Thinking…"
        case .speaking: "Speaking…"
        }
    }

    private var transcriptLine: String? {
        if !state.partialTranscript.isEmpty { return state.partialTranscript }
        guard state.voice == .processing || state.voice == .speaking,
              let last = state.turns.last(where: { $0.role == .user }) else { return nil }
        return "“\(last.text)”"
    }

    private var replyLine: String? {
        guard state.voice == .processing || state.voice == .speaking,
              let last = state.turns.last, last.role == .assistant, !last.text.isEmpty else { return nil }
        return last.text
    }
}

/// Compact approval in the voice panel: say "send it", or click.
struct OverlayApproval: View {
    let approval: PendingApproval
    let answer: (Bool) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(approval.title).font(.callout.weight(.semibold))
            ForEach(approval.fields.filter { $0.label != "Body" }.prefix(3), id: \.self) {
                Text("\($0.label): \($0.value)").font(.caption).lineLimit(1)
            }
            if let body = approval.fields.first(where: { $0.label == "Body" }) {
                Text(body.value).font(.caption).foregroundStyle(.secondary).lineLimit(3)
            }
            HStack {
                Button("Cancel") { answer(false) }
                Button("Approve") { answer(true) }.buttonStyle(.borderedProminent)
                Text("or say “send it”").font(.caption2).foregroundStyle(.secondary)
            }
            .controlSize(.small)
        }
        .padding(8)
        .background(.quaternary.opacity(0.5), in: .rect(cornerRadius: 10))
    }
}

/// Shows whether the mic is actually picking anything up.
struct MicLevelMeter: View {
    let level: Float

    var body: some View {
        // Peak level on a rough dB scale: -60 dB empty, 0 dB full.
        let db = 20 * log10(max(level, 0.000_1))
        let fraction = CGFloat(min(max((db + 60) / 60, 0), 1))
        Capsule()
            .fill(.quaternary)
            .frame(width: 60, height: 6)
            .overlay(alignment: .leading) {
                Capsule().fill(.green).frame(width: 60 * fraction, height: 6)
            }
            .animation(.linear(duration: 0.08), value: fraction)
            .help("Microphone level")
    }
}

/// Pulsing indicator for the voice phase.
struct VoiceOrb: View {
    let phase: AppState.VoicePhase

    var body: some View {
        Circle()
            .fill(color.gradient)
            .overlay {
                Image(systemName: symbol)
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(.white)
                    .symbolEffect(.pulse, isActive: phase == .hearing || phase == .processing)
            }
            .symbolEffect(.breathe, isActive: phase == .listening)
    }

    private var color: Color {
        switch phase {
        case .off: .gray
        case .listening: .blue
        case .hearing: .green
        case .processing: .orange
        case .speaking: .purple
        }
    }

    private var symbol: String {
        switch phase {
        case .off: "mic.slash.fill"
        case .listening, .hearing: "mic.fill"
        case .processing: "ellipsis"
        case .speaking: "waveform"
        }
    }
}
