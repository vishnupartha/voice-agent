import Foundation

// Mirrors backend/api/schemas.py — keep in sync.

enum ClientMessage: Encodable, Sendable {
    case sessionStart(sessionID: String, conversationID: String?)
    case userMessage(text: String)
    case audioStart
    case audioStop
    case cancel
    case approvalResponse(approvalID: String, approved: Bool)

    private enum CodingKeys: String, CodingKey {
        case type, sessionID = "session_id", conversationID = "conversation_id", text
        case approvalID = "approval_id", approved
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

struct HealthResponse: Decodable, Sendable {
    struct Models: Decodable, Sendable { let stt: Bool; let llm: Bool; let tts: Bool }
    let status: String
    let version: String
    let models: Models
}
