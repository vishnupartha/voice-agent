import Foundation

// Mirrors backend/api/schemas.py — keep in sync.

enum ClientMessage: Encodable, Sendable {
    case sessionStart(sessionID: String, conversationID: String?)
    case userMessage(text: String)
    case audioStart
    case audioStop
    case cancel
    case approvalResponse(approvalID: String, approved: Bool)
    case bridgeResponse(requestID: String, ok: Bool, result: JSONValue?, error: String?)

    private enum CodingKeys: String, CodingKey {
        case type, sessionID = "session_id", conversationID = "conversation_id", text
        case approvalID = "approval_id", approved
        case requestID = "request_id", ok, result, error
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case let .sessionStart(sessionID, conversationID):
            try c.encode("session.start", forKey: .type)
            try c.encode(sessionID, forKey: .sessionID)
            try c.encodeIfPresent(conversationID, forKey: .conversationID)
        case let .userMessage(text):
            try c.encode("user.message", forKey: .type)
            try c.encode(text, forKey: .text)
        case .audioStart:
            try c.encode("audio.start", forKey: .type)
        case .audioStop:
            try c.encode("audio.stop", forKey: .type)
        case .cancel:
            try c.encode("conversation.cancel", forKey: .type)
        case let .approvalResponse(approvalID, approved):
            try c.encode("approval.response", forKey: .type)
            try c.encode(approvalID, forKey: .approvalID)
            try c.encode(approved, forKey: .approved)
        case let .bridgeResponse(requestID, ok, result, error):
            try c.encode("bridge.response", forKey: .type)
            try c.encode(requestID, forKey: .requestID)
            try c.encode(ok, forKey: .ok)
            try c.encodeIfPresent(result, forKey: .result)
            try c.encodeIfPresent(error, forKey: .error)
        }
    }
}

struct ToolActivity: Decodable, Sendable, Equatable, Identifiable {
    var id: String { "\(tool)-\(ms)-\(summary)" }
    let tool: String
    let ok: Bool
    let summary: String
    let ms: Int
}

struct ApprovalField: Decodable, Sendable, Equatable, Hashable {
    let label: String
    let value: String
}

struct DraftView: Decodable, Sendable, Equatable {
    let draftId: String
    let to: String
    let subject: String
    let body: String
}

struct TurnMetrics: Decodable, Sendable, Equatable {
    let firstTokenMs: Int?
    let totalMs: Int
    let tokens: Int
}

/// A backend event. Unknown fields are ignored; unknown types decode as `.unknown`.
struct AgentEvent: Decodable, Sendable {
    enum Kind: String, Decodable, Sendable {
        case sessionReady = "session.ready"
        case speechStarted = "input.speech_started"
        case transcriptPartial = "transcript.partial"
        case transcriptFinal = "transcript.final"
        case agentThinking = "agent.thinking"
        case toolStarted = "tool.started"
        case toolCompleted = "tool.completed"
        case textDelta = "assistant.text.delta"
        case textFinal = "assistant.text.final"
        case audioStart = "assistant.audio.start"
        case audioEnd = "assistant.audio.end"
        case approvalRequired = "approval.required"
        case approvalResolved = "approval.resolved"
        case draftUpdated = "draft.updated"
        case openURL = "system.open_url"
        case bridgeRequest = "bridge.request"
        case error
        case unknown

        init(from decoder: Decoder) throws {
            self = Kind(rawValue: try decoder.singleValueContainer().decode(String.self)) ?? .unknown
        }
    }

    let type: Kind
    let sessionId: String
    let eventId: String
    let timestamp: Double

    let conversationId: String?
    let delta: String?
    let text: String?
    let cancelled: Bool?
    let interrupted: Bool?
    let sampleRate: Int?
    let metrics: TurnMetrics?
    let code: String?
    let message: String?

    // tools
    let callId: String?
    let tool: String?
    let label: String?
    let ok: Bool?
    let summary: String?
    let ms: Int?
    let tools: [ToolActivity]?
    // approvals
    let approvalId: String?
    let title: String?
    let fields: [ApprovalField]?
    let approved: Bool?
    // drafts, links
    let draft: DraftView?
    let url: String?
}

struct ConnectionsResponse: Decodable, Sendable {
    struct Google: Decodable, Sendable { let configured: Bool; let connected: Bool }
    let google: Google
}

/// A choice between engines (voice or brain) and which one is active.
struct EngineSettings: Decodable, Sendable, Equatable {
    struct Engine: Decodable, Sendable, Equatable, Identifiable {
        let id: String
        let name: String
        let available: Bool
    }

    let engine: String
    let engines: [Engine]
}

/// What's set up (from the backend's /setup), for the Settings window.
struct SetupStatus: Decodable, Sendable, Equatable {
    struct Google: Decodable, Sendable, Equatable { let configured: Bool; let connected: Bool }
    let geminiKey: Bool
    let brainReady: Bool
    let google: Google

    enum CodingKeys: String, CodingKey {
        case geminiKey = "gemini_key", brainReady = "brain_ready", google
    }
}

struct HealthResponse: Decodable, Sendable {
    struct Models: Decodable, Sendable { let stt: Bool; let llm: Bool; let tts: Bool }
    let status: String
    let version: String
    let models: Models
}

/// The backend asking the app to do something only it can (EventKit, ...).
/// Decoded without snake-case conversion so `params` keys arrive as sent.
struct BridgeRequest: Decodable, Sendable {
    let requestID: String
    let method: String
    let params: JSONValue

    enum CodingKeys: String, CodingKey {
        case requestID = "request_id", method, params
    }
}

/// Any JSON value, for bridge params and results whose shape depends on the method.
enum JSONValue: Codable, Sendable, Equatable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() {
            self = .null
        } else if let b = try? c.decode(Bool.self) {
            self = .bool(b)
        } else if let n = try? c.decode(Double.self) {
            self = .number(n)
        } else if let s = try? c.decode(String.self) {
            self = .string(s)
        } else if let a = try? c.decode([JSONValue].self) {
            self = .array(a)
        } else {
            self = .object(try c.decode([String: JSONValue].self))
        }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .null: try c.encodeNil()
        case let .bool(b): try c.encode(b)
        case let .number(n): try c.encode(n)
        case let .string(s): try c.encode(s)
        case let .array(a): try c.encode(a)
        case let .object(o): try c.encode(o)
        }
    }

    subscript(key: String) -> JSONValue? {
        if case let .object(o) = self { o[key] } else { nil }
    }

    var string: String? { if case let .string(s) = self { s } else { nil } }
    var number: Double? { if case let .number(n) = self { n } else { nil } }
    var bool: Bool? { if case let .bool(b) = self { b } else { nil } }
}
