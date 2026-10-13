import Foundation

/// Thin async wrapper over URLSessionWebSocketTask.
/// Text frames are JSON; binary frames are PCM16 audio.
final class WebSocketClient: Sendable {
    enum Frame: Sendable { case text(String), binary(Data) }

    private let task: URLSessionWebSocketTask

    init(url: URL, token: String) {
        var request = URLRequest(url: url)
        request.setValue(token, forHTTPHeaderField: BackendConfig.tokenHeader)
        task = URLSession.shared.webSocketTask(with: request)
        task.maximumMessageSize = 16 * 1024 * 1024
    }

    func connect() { task.resume() }

    func close() { task.cancel(with: .normalClosure, reason: nil) }

    func send(json: some Encodable) async throws {
        let data = try JSONEncoder().encode(json)
        try await task.send(.string(String(decoding: data, as: UTF8.self)))
    }

    func send(binary: Data) async throws {
        try await task.send(.data(binary))
    }

    /// Yields frames until the socket closes; finishes by throwing on error.
    func frames() -> AsyncThrowingStream<Frame, Error> {
        AsyncThrowingStream { continuation in
            let task = self.task
            let reader = Task {
                do {
                    while !Task.isCancelled {
                        switch try await task.receive() {
                        case let .string(s): continuation.yield(.text(s))
                        case let .data(d): continuation.yield(.binary(d))
                        @unknown default: break
                        }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in reader.cancel() }
        }
    }
}
