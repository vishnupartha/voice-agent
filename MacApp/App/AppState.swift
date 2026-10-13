import AppKit
import Foundation
import Observation
import os

struct Turn: Identifiable, Equatable {
    enum Role { case user, assistant }
    let id = UUID()
    let role: Role
    var text: String
    var isStreaming = false
    var cancelled = false
    var metrics: TurnMetrics?
    var tools: [LiveTool] = []
    var draft: DraftView?
}

/// One tool call as the UI shows it (spec §17): running, then done or failed.
struct LiveTool: Identifiable, Equatable {
    let id: String
    let label: String
    var done = false
    var ok = true
    var summary = ""
    var ms = 0
}

/// An action waiting for the user's yes (spec §19, §72).
struct PendingApproval: Identifiable, Equatable {
    let id: String
    let title: String
    let fields: [ApprovalField]
}

@MainActor
@Observable
final class AppState {
    enum BackendStatus: Equatable {
        case starting, ready, unavailable(String)
    }

    enum Phase: Equatable { case idle, thinking, responding }

    /// Voice interaction state (spec §13). `.off` means the mic is closed.
    enum VoicePhase: Equatable { case off, listening, hearing, processing, speaking }

    private(set) var backend: BackendStatus = .starting
    private(set) var phase: Phase = .idle
    private(set) var turns: [Turn] = []
    private(set) var lastError: String?
    private(set) var voice: VoicePhase = .off
    private(set) var partialTranscript = ""
    /// Recent mic peak level (0...1), for the overlay meter.
    private(set) var micLevel: Float = 0
    private(set) var pendingApproval: PendingApproval?
    enum GoogleStatus: Equatable { case unknown, notConfigured, disconnected, connecting, connected }
    private(set) var google: GoogleStatus = .unknown
    private var silentChunks = 0
    private let micGate = MicGate()

    /// Called when voice mode turns on or off, to show or hide the overlay.
    var onVoiceActiveChanged: ((Bool) -> Void)?

    private let backendManager = BackendManager()
    private let client = AgentClient()
    private let audio = AudioSession()
    private var conversationID: String?
    private var token: String?

    init() {
        client.onEvent = { [weak self] in self?.handle($0) }
        client.onAudio = { [weak self] in self?.audio.playback.enqueue($0) }
        audio.playback.onFinished = { [weak self] in self?.speechFinished() }
        client.onDisconnect = { [weak self] error in
            guard let self else { return }
            self.stopVoice()
            self.phase = .idle
            self.backend = .unavailable(error?.localizedDescription ?? "Disconnected")
        }
    }

    func start() async {
        backend = .starting
        do {
            let token = try await backendManager.ensureRunning()
            self.token = token
            try await client.connect(token: token, conversationID: conversationID)
            backend = .ready
            await refreshConnections()
        } catch {
            backend = .unavailable(error.localizedDescription)
        }
    }

    func send(_ text: String) {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, backend == .ready else { return }
        finishStreamingTurn(cancelled: true)
        turns.append(Turn(role: .user, text: text))
        lastError = nil
        Task { await sendOrReport(.userMessage(text: text)) }
    }

    func cancel() {
        Task { await sendOrReport(.cancel) }
    }

    func newConversation() {
        turns = []
        conversationID = nil
        Task { await start() }
    }

    // MARK: Approvals

    func answerApproval(_ approved: Bool) {
        guard let approval = pendingApproval else { return }
        pendingApproval = nil
        Task { await sendOrReport(.approvalResponse(approvalID: approval.id, approved: approved)) }
    }

    // MARK: Google

    func refreshConnections() async {
        guard let data = try? await api("GET", "/connections"),
              let c = try? JSONDecoder().decode(ConnectionsResponse.self, from: data)
        else { return }
        google = !c.google.configured ? .notConfigured : c.google.connected ? .connected : .disconnected
    }

    /// Opens Google's consent page in the browser; the backend finishes the
    /// sign-in on its callback, and this polls until it sees the connection.
    func connectGoogle() {
        Task {
            do {
                _ = try await api("POST", "/auth/google/start")
            } catch {
                lastError = error.localizedDescription
                return
            }
            google = .connecting
            for _ in 0 ..< 150 { // up to 5 minutes
                try? await Task.sleep(for: .seconds(2))
                await refreshConnections()
                if google == .connected { return }
                google = .connecting
            }
            await refreshConnections()
        }
    }

    func disconnectGoogle() {
        Task {
            _ = try? await api("POST", "/auth/google/disconnect")
            await refreshConnections()
        }
    }

    private struct APIError: LocalizedError {
        let errorDescription: String?
    }

    private func api(_ method: String, _ path: String) async throws -> Data {
        guard let token else { throw APIError(errorDescription: "Backend not ready") }
        var request = URLRequest(url: BackendConfig.httpURL(path: path))
        request.httpMethod = method
        request.setValue(token, forHTTPHeaderField: BackendConfig.tokenHeader)
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200 ..< 300).contains(http.statusCode) else {
            struct Detail: Decodable { let detail: String }
            let detail = (try? JSONDecoder().decode(Detail.self, from: data))?.detail
            throw APIError(errorDescription: detail ?? "Request failed")
        }
        return data
    }

    // MARK: Voice

    /// Option+Space (spec §14): start talking; while the assistant is working
    /// or speaking, cut it off and listen; while listening, close the mic.
    func hotkeyPressed() {
        switch voice {
        case .off: Task { await startVoice() }
        case .processing, .speaking: interruptAndListen()
        case .listening, .hearing: stopVoice()
        }
    }

    func toggleVoice() {
        if voice == .off { Task { await startVoice() } } else { stopVoice() }
    }

    func startVoice() async {
        guard voice == .off else { return }
        if backend != .ready { await start() }
        guard backend == .ready, let input = client.audioInput else { return }
        lastError = nil
        silentChunks = 0
        setVoice(.listening)
        do {
            try await startAudio(input: input, mode: nil)
            await sendOrReport(.audioStart)
        } catch {
            lastError = error.localizedDescription
            setVoice(.off)
        }
    }

    func stopVoice() {
        guard voice != .off else { return }
        if audio.isRunning { Task { await sendOrReport(.audioStop) } }
        audio.stop()
        partialTranscript = ""
        micLevel = 0
        setVoice(.off)
    }

    private func startAudio(input: AsyncStream<Data>.Continuation, mode: AudioSession.Mode?) async throws {
        silentChunks = 0
        let gate = micGate
        try await audio.start(
            mode: mode,
            onAudio: { chunk in if gate.isOpen { input.yield(chunk) } },
            onLevel: { level in Task { @MainActor [weak self] in self?.updateLevel(level) } }
        )
        updateMicGate()
    }

    /// Without echo cancellation the mic would hear the assistant and cut it
    /// off, so in plain mode mic audio isn't sent while it speaks.
    private func updateMicGate() {
        micGate.isOpen = audio.mode != .plain || voice != .speaking
    }

    private func updateLevel(_ level: Float) {
        // Fast attack, slow decay, so the meter reads naturally.
        micLevel = max(level, micLevel * 0.8)
        // A real mic always has some noise; exact digital silence means macOS is
        // blocking the input (spec §85: say so rather than silently fail).
        if level > 0 {
            silentChunks = 0
        } else if voice != .off {
            silentChunks += 1
            if silentChunks == 20, audio.mode == .voiceProcessing, let input = client.audioInput {
                // Voice processing sometimes runs but delivers only zeros;
                // fall back to plain capture rather than going deaf.
                audio.stop()
                Task {
                    do { try await startAudio(input: input, mode: .plain) } catch {
                        lastError = error.localizedDescription
                    }
                }
            } else if silentChunks == 40 { // ~2 s of tap callbacks (2048 frames at 48 kHz)
                lastError = "The microphone is delivering silence. Check System Settings → Privacy & "
                    + "Security → Microphone, and that the right input device is selected in Sound settings."
            }
        }
    }

    private func interruptAndListen() {
        audio.playback.stop()
        cancel()
        setVoice(.listening)
    }

    private func speechFinished() {
        if voice == .speaking || voice == .processing { setVoice(.listening) }
    }

    private func setVoice(_ new: VoicePhase) {
        let wasActive = voice != .off
        voice = new
        updateMicGate()
        if wasActive != (new != .off) { onVoiceActiveChanged?(new != .off) }
    }

    func shutdown() {
        audio.stop()
        client.disconnect()
        backendManager.stop()
    }

    private func sendOrReport(_ message: ClientMessage) async {
        do { try await client.send(message) } catch { lastError = error.localizedDescription }
    }

    private func handle(_ event: AgentEvent) {
        switch event.type {
        case .sessionReady:
            conversationID = event.conversationId
        case .speechStarted:
            if event.interrupted == true { audio.playback.stop() }
            partialTranscript = ""
            if voice != .off { setVoice(.hearing) }
        case .transcriptPartial:
            partialTranscript = event.text ?? ""
        case .transcriptFinal:
            partialTranscript = ""
            let text = event.text ?? ""
            if !text.isEmpty {
                finishStreamingTurn(cancelled: true)
                turns.append(Turn(role: .user, text: text))
            }
            if voice != .off { setVoice(text.isEmpty ? .listening : .processing) }
        case .audioStart:
            audio.playback.begin(sampleRate: event.sampleRate ?? 24_000)
            if voice != .off { setVoice(.speaking) }
        case .audioEnd:
            if event.interrupted == true {
                audio.playback.stop()
                speechFinished()
            } else {
                audio.playback.endOfStream()
            }
        case .agentThinking:
            phase = .thinking
            turns.append(Turn(role: .assistant, text: "", isStreaming: true))
        case .textDelta:
            phase = .responding
            if let i = turns.indices.last, turns[i].isStreaming { turns[i].text += event.delta ?? "" }
        case .textFinal:
            phase = .idle
            if let i = turns.indices.last, turns[i].isStreaming {
                turns[i].text = event.text ?? turns[i].text
                turns[i].isStreaming = false
                turns[i].cancelled = event.cancelled ?? false
                turns[i].metrics = event.metrics
            }
        case .toolStarted:
            if let i = streamingIndex(), let id = event.callId {
                turns[i].tools.append(LiveTool(id: id, label: event.label ?? event.tool ?? "Tool"))
            }
        case .toolCompleted:
            if let i = streamingIndex(), let j = turns[i].tools.firstIndex(where: { $0.id == event.callId }) {
                turns[i].tools[j].done = true
                turns[i].tools[j].ok = event.ok ?? true
                turns[i].tools[j].summary = event.summary ?? ""
                turns[i].tools[j].ms = event.ms ?? 0
            }
        case .draftUpdated:
            if let i = streamingIndex() { turns[i].draft = event.draft }
        case .approvalRequired:
            if let id = event.approvalId {
                pendingApproval = PendingApproval(id: id, title: event.title ?? "Approve", fields: event.fields ?? [])
                NSApp.requestUserAttention(.informationalRequest)
            }
        case .approvalResolved:
            if pendingApproval?.id == event.approvalId { pendingApproval = nil }
        case .openURL:
            if let s = event.url, let url = URL(string: s), ["http", "https"].contains(url.scheme) {
                NSWorkspace.shared.open(url)
            }
        case .error:
            phase = .idle
            if voice == .processing || voice == .speaking { setVoice(.listening) }
            finishStreamingTurn(cancelled: true)
            lastError = event.message ?? event.code ?? "Unknown error"
        default:
            break
        }
    }

    private func streamingIndex() -> Int? {
        guard let i = turns.indices.last, turns[i].isStreaming else { return nil }
        return i
    }

    private func finishStreamingTurn(cancelled: Bool) {
        guard let i = streamingIndex() else { return }
        turns[i].isStreaming = false
        turns[i].cancelled = cancelled
        if turns[i].text.isEmpty && turns[i].tools.isEmpty { turns.remove(at: i) }
    }
}

/// Read on the audio thread, written on the main actor.
final class MicGate: @unchecked Sendable {
    private let lock = OSAllocatedUnfairLock(initialState: true)
    var isOpen: Bool {
        get { lock.withLock { $0 } }
        set { lock.withLock { $0 = newValue } }
    }
}
