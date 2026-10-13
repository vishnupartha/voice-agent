import SwiftUI

/// Confirmation for high-impact actions (spec §19). Saying "send it" works
/// too; either way only this exact pending approval is answered.
struct ApprovalView: View {
    let approval: PendingApproval
    let answer: (Bool) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(approval.title).font(.title2.weight(.semibold))
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(approval.fields, id: \.self) { field in
                        if field.label == "Body" {
                            Divider()
                            Text(field.value).textSelection(.enabled)
                        } else {
                            HStack(alignment: .firstTextBaseline) {
                                Text("\(field.label):").foregroundStyle(.secondary)
                                Text(field.value).textSelection(.enabled)
                            }
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: 320)
            Text("You can also say “send it” or “cancel”.").font(.caption).foregroundStyle(.secondary)
            HStack {
                Button("Cancel", role: .cancel) { answer(false) }
                    .keyboardShortcut(.cancelAction)
                Spacer()
                Button(confirmLabel) { answer(true) }
                    .keyboardShortcut(.defaultAction)
                    .buttonStyle(.borderedProminent)
            }
        }
        .padding(20)
        .frame(width: 460)
    }

    private var confirmLabel: String {
        switch approval.title {
        case "Send Email": "Send"
        case "Add Event": "Add"
        case "Delete Event": "Delete"
        default: "Confirm"
        }
    }
}
