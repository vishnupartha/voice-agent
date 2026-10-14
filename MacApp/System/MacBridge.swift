import Foundation

/// Runs the backend's bridge requests: things only the app can do, such as
/// EventKit, which needs a permission granted to the app itself.
@MainActor
final class MacBridge {
    private let reminders = Reminders()

    func handle(_ method: String, _ params: JSONValue) async throws -> JSONValue {
        if method.hasPrefix("reminders.") { return try await reminders.handle(method, params) }
        throw BridgeError("unknown method \(method)")
    }
}

/// A failure reported back to the backend; the message reaches the model.
struct BridgeError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}
