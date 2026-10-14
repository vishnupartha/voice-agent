import EventKit
import Foundation

/// Apple Reminders through EventKit, for the backend's reminders.* tools
/// (backend/tools/reminders.py). Due dates travel as Unix timestamps plus
/// `timed`: whether they have a time of day or are for a whole day.
@MainActor
final class Reminders {
    private static let maxListed = 50
    private let store = EKEventStore()

    func handle(_ method: String, _ p: JSONValue) async throws -> JSONValue {
        try await ensureAccess()
        switch method {
        case "reminders.list":
            return try await list(in: p["list"]?.string, dueBefore: p["due_before"]?.number)
        case "reminders.create":
            return try create(p)
        case "reminders.get":
            return Self.describe(try reminder(p))
        case "reminders.complete":
            let r = try reminder(p)
            r.isCompleted = true
            try store.save(r, commit: true)
            return Self.describe(try saved(r))
        case "reminders.delete":
            try store.remove(try reminder(p), commit: true)
            return .object(["deleted": .bool(true)])
        default:
            throw BridgeError("unknown method \(method)")
        }
    }

    private func ensureAccess() async throws {
        switch EKEventStore.authorizationStatus(for: .reminder) {
        case .fullAccess:
            return
        case .notDetermined:
            if try await store.requestFullAccessToReminders() { return }
        default:
            break
        }
        throw BridgeError(
            "Reminders access is off. Turn on Personal Agent in System Settings → Privacy & Security → Reminders.")
    }

    /// The lists called `name` (any case), or nil for all lists.
    private func lists(named name: String?) throws -> [EKCalendar]? {
        guard let name, !name.isEmpty else { return nil }
        let found = store.calendars(for: .reminder).filter {
            $0.title.localizedCaseInsensitiveCompare(name) == .orderedSame
        }
        if found.isEmpty { throw BridgeError("no Reminders list named \(name)") }
        return found
    }

    private func list(in listName: String?, dueBefore: Double?) async throws -> JSONValue {
        let predicate = store.predicateForIncompleteReminders(
            withDueDateStarting: nil,
            ending: dueBefore.map { Date(timeIntervalSince1970: $0) },
            calendars: try lists(named: listName))
        // EventKit calls back on a background queue, so the closure must not be main-actor isolated.
        let found: [JSONValue] = await withCheckedContinuation { cont in
            store.fetchReminders(matching: predicate) { @Sendable reminders in
                let sorted = (reminders ?? []).sorted { Self.dueDate($0) ?? .distantFuture < Self.dueDate($1) ?? .distantFuture }
                cont.resume(returning: sorted.prefix(Self.maxListed).map(Self.describe))
            }
        }
        let names = store.calendars(for: .reminder).map { JSONValue.string($0.title) }
        return .object(["lists": .array(names), "reminders": .array(found)])
    }

    private func create(_ p: JSONValue) throws -> JSONValue {
        guard let title = p["title"]?.string else { throw BridgeError("missing title") }
        guard let list = try lists(named: p["list"]?.string)?.first ?? store.defaultCalendarForNewReminders()
        else { throw BridgeError("there's no Reminders list to add to") }
        let r = EKReminder(eventStore: store)
        r.title = title
        r.notes = p["notes"]?.string
        r.calendar = list
        if let due = p["due"]?.number {
            let date = Date(timeIntervalSince1970: due)
            let timed = p["timed"]?.bool ?? false
            let parts: Set<Calendar.Component> = timed ? [.year, .month, .day, .hour, .minute] : [.year, .month, .day]
            r.dueDateComponents = Calendar.current.dateComponents(parts, from: date)
            // Without an alarm, Reminders shows the time but doesn't alert.
            if timed { r.addAlarm(EKAlarm(absoluteDate: date)) }
        }
        try store.save(r, commit: true)
        return Self.describe(try saved(r))
    }

    /// The reminder as stored, read back after saving, so a reply of success means it's really there.
    private func saved(_ r: EKReminder) throws -> EKReminder {
        guard let stored = store.calendarItem(withIdentifier: r.calendarItemIdentifier) as? EKReminder
        else { throw BridgeError("Reminders didn't keep the change") }
        return stored
    }

    private func reminder(_ p: JSONValue) throws -> EKReminder {
        guard let id = p["id"]?.string, let r = store.calendarItem(withIdentifier: id) as? EKReminder
        else { throw BridgeError("no reminder with that id; list reminders again") }
        return r
    }

    nonisolated private static func dueDate(_ r: EKReminder) -> Date? {
        r.dueDateComponents.flatMap { Calendar.current.date(from: $0) }
    }

    nonisolated private static func describe(_ r: EKReminder) -> JSONValue {
        var o: [String: JSONValue] = [
            "id": .string(r.calendarItemIdentifier),
            "title": .string(r.title ?? ""),
            "list": .string(r.calendar?.title ?? ""),
        ]
        if let due = dueDate(r) {
            o["due"] = .number(due.timeIntervalSince1970)
            o["timed"] = .bool(r.dueDateComponents?.hour != nil)
        }
        if let notes = r.notes, !notes.isEmpty { o["notes"] = .string(notes) }
        return .object(o)
    }
}
