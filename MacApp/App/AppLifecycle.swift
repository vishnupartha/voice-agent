import AppKit
import Foundation
import os

enum BackendConfig {
    static let host = "127.0.0.1"
    static let port = 8765
    static let tokenHeader = "x-agent-token"

    static let appSupport = FileManager.default.homeDirectoryForCurrentUser
        .appending(path: "Library/Application Support/PersonalAgent", directoryHint: .isDirectory)
    static let tokenFile = appSupport.appending(path: "agent.token")
    static let logDir = FileManager.default.homeDirectoryForCurrentUser
        .appending(path: "Library/Logs/PersonalAgent", directoryHint: .isDirectory)

    static func httpURL(path: String) -> URL { URL(string: "http://\(host):\(port)\(path)")! }
    static func wsURL(path: String) -> URL { URL(string: "ws://\(host):\(port)\(path)")! }
}

/// Finds or launches the local agent daemon (spec §9, §103, §106).
@MainActor
final class BackendManager {
    enum LaunchError: LocalizedError {
        case noBackendFound
        case unhealthy(String)

        var errorDescription: String? {
            switch self {
            case .noBackendFound: "Couldn't find agentd or a development checkout with uv."
            case let .unhealthy(detail): "Backend didn't become healthy: \(detail)"
            }
        }
    }

    private let log = Logger(subsystem: Bundle.main.bundleIdentifier ?? "PersonalAgent", category: "backend")
    private var process: Process?

    /// Returns the auth token once the backend is healthy.
    func ensureRunning() async throws -> String {
        if process?.isRunning != true, await Self.health() != nil {
            // Not ours: left over from an earlier run (Xcode's Stop doesn't shut it
            // down), so it may be running older code. Replace it.
            try await replaceStale()
        }
        if await Self.health() == nil {
            try launch()
            try await waitForHealth(timeout: .seconds(60))
        }
        return try readToken()
    }

    func stop() {
        guard let process, process.isRunning else { return }
        process.terminate()
        self.process = nil
    }

    nonisolated static func health() async -> HealthResponse? {
        var request = URLRequest(url: BackendConfig.httpURL(path: "/health"))
        request.timeoutInterval = 1
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200
        else { return nil }
        return try? JSONDecoder().decode(HealthResponse.self, from: data)
    }

    private func replaceStale() async throws {
        log.info("stopping a backend from an earlier run")
        var request = URLRequest(url: BackendConfig.httpURL(path: "/shutdown"))
        request.httpMethod = "POST"
        request.timeoutInterval = 2
        request.setValue(try readToken(), forHTTPHeaderField: BackendConfig.tokenHeader)
        _ = try? await URLSession.shared.data(for: request)
        let deadline = ContinuousClock.now + .seconds(10)
        while ContinuousClock.now < deadline {
            if await Self.health() == nil { return }
            try await Task.sleep(for: .milliseconds(200))
        }
        throw LaunchError.unhealthy(
            "an older backend is still running on port \(BackendConfig.port); quit it and try again")
    }

    private func readToken() throws -> String {
        try String(contentsOf: BackendConfig.tokenFile, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func waitForHealth(timeout: Duration) async throws {
        let deadline = ContinuousClock.now + timeout
        while ContinuousClock.now < deadline {
            if await Self.health() != nil { return }
            if let process, !process.isRunning {
                throw LaunchError.unhealthy("process exited with status \(process.terminationStatus)")
            }
            try await Task.sleep(for: .milliseconds(300))
        }
        throw LaunchError.unhealthy("timed out")
    }

    private func launch() throws {
        let process = Process()
        var env = ProcessInfo.processInfo.environment
        // GUI apps inherit a minimal PATH.
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + (env["PATH"] ?? "")
        process.environment = env

        let bundled = Bundle.main.bundleURL.appending(path: "Contents/MacOS/agentd")
        if FileManager.default.isExecutableFile(atPath: bundled.path) {
            process.executableURL = bundled
        } else if let repo = Bundle.main.object(forInfoDictionaryKey: "PARepoRoot") as? String,
                  let uv = ["/opt/homebrew/bin/uv", "/usr/local/bin/uv",
                            NSHomeDirectory() + "/.local/bin/uv"]
                      .first(where: FileManager.default.isExecutableFile(atPath:)),
                  FileManager.default.fileExists(atPath: repo + "/pyproject.toml")
        {
            process.executableURL = URL(filePath: uv)
            process.currentDirectoryURL = URL(filePath: repo, directoryHint: .isDirectory)
            process.arguments = [
                "run", "uvicorn", "backend.api.server:app",
                "--host", BackendConfig.host, "--port", String(BackendConfig.port),
            ]
        } else {
            throw LaunchError.noBackendFound
        }

        try FileManager.default.createDirectory(at: BackendConfig.logDir, withIntermediateDirectories: true)
        let out = BackendConfig.logDir.appending(path: "agentd.stdout.log")
        FileManager.default.createFile(atPath: out.path, contents: nil)
        let handle = try FileHandle(forWritingTo: out)
        process.standardOutput = handle
        process.standardError = handle

        log.info("launching backend: \(process.executableURL?.path ?? "?", privacy: .public)")
        try process.run()
        self.process = process
    }
}
