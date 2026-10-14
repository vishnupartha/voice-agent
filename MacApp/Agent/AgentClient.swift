import Foundation

/// One session with the backend over /ws/voice: JSON events plus binary PCM16
/// audio in both directions (spec §11).
@MainActor
final class AgentClient {
    private var socket: WebSocketClient?
    private var readTask: Task<Void, Never>?
    private var audioTask: Task<Void, Never>?
    let sessionID = UUID().uuidString

    var onEvent: ((AgentEvent) -> Void)?
    var onAudio: ((Data) -> Void)?
    var onDisconnect: ((Error?) -> Void)?
    var onBridgeRequest: ((BridgeRequest) -> Void)?

    /// Microphone chunks yielded here are sent in order. Safe to use from the
    /// audio thread.
    private(set) var audioInput: AsyncStream<Data>.Continuation?

    func connect(token: String, conversationID: String?) async throws {
        disconnect()
        let socket = WebSocketClient(url: BackendConfig.wsURL(path: "/ws/voice"), token: token)
        self.socket = socket
        socket.connect()
        let (audio, continuation) = AsyncStream.makeStream(of: Data.self, bufferingPolicy: .bufferingNewest(64))
        audioInput = continuation
        audioTask = Task {
            for await chunk in audio { try? await socket.send(binary: chunk) }
        }
        readTask = Task { [weak self] in
            let decoder = JSONDecoder()
            decoder.keyDecodingStrategy = .convertFromSnakeCase
            let plainDecoder = JSONDecoder()
            do {
                for try await frame in socket.frames() {
                    switch frame {
                    case let .binary(pcm):
                        self?.onAudio?(pcm)
                    case let .text(s):
                        guard let event = try? decoder.decode(AgentEvent.self, from: Data(s.utf8)) else { break }
                        if event.type == .bridgeRequest {
                            if let req = try? plainDecoder.decode(BridgeRequest.self, from: Data(s.utf8)) {
                                self?.onBridgeRequest?(req)
                            }
                        } else {
                            self?.onEvent?(event)
                        }
                    }
                }
                self?.onDisconnect?(nil)
            } catch {
                self?.onDisconnect?(error)
            }
        }
        try await socket.send(json: ClientMessage.sessionStart(sessionID: sessionID, conversationID: conversationID))
    }

    func send(_ message: ClientMessage) async throws {
        guard let socket else { throw URLError(.notConnectedToInternet) }
        try await socket.send(json: message)
    }

    func disconnect() {
        readTask?.cancel()
        readTask = nil
        audioInput?.finish()
        audioInput = nil
        audioTask?.cancel()
        audioTask = nil
        socket?.close()
        socket = nil
    }
}
