import SwiftUI

/// What the agent did this turn: one collapsed line, expandable.
struct ToolStatusView: View {
    let tools: [LiveTool]
    @State private var expanded = false

    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            VStack(alignment: .leading, spacing: 3) {
                ForEach(tools) { tool in
                    HStack(spacing: 6) {
                        icon(tool)
                        Text(tool.label)
                        if tool.done && !tool.ok {
                            Text(tool.summary).foregroundStyle(.red).lineLimit(2)
                        }
                        Spacer()
                        if tool.done { Text("\(tool.ms) ms").foregroundStyle(.tertiary) }
                    }
                }
            }
            .font(.caption)
            .padding(.leading, 4)
        } label: {
            HStack(spacing: 6) {
                if tools.contains(where: { !$0.done }) {
                    ProgressView().controlSize(.mini)
                } else if tools.contains(where: { !$0.ok }) {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                } else {
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                }
                Text(headline).foregroundStyle(.secondary)
            }
            .font(.caption)
        }
    }

    private var headline: String {
        let labels = tools.map(\.label).reduce(into: [String]()) { if !$0.contains($1) { $0.append($1) } }
        return labels.joined(separator: " · ")
    }

    @ViewBuilder private func icon(_ tool: LiveTool) -> some View {
        if !tool.done {
            ProgressView().controlSize(.mini)
        } else {
            Image(systemName: tool.ok ? "checkmark" : "xmark").foregroundStyle(tool.ok ? .green : .red)
        }
    }
}

/// The current email draft, so edits by voice can be seen.
struct DraftCard: View {
    let draft: DraftView

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Label("Draft — not sent", systemImage: "envelope").font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            Text("To: \(draft.to)").font(.callout)
            Text(draft.subject).font(.callout.weight(.medium))
            Divider()
            Text(draft.body).font(.callout).textSelection(.enabled)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.4), in: .rect(cornerRadius: 10))
    }
}
