import AVFoundation
import os

/// One AVAudioEngine for both microphone and speaker (spec §12).
///
/// Preferred mode is voice processing: capture and playback share the engine
/// with Apple's voice-processing I/O unit, which cancels the assistant's own
/// voice from the mic so it can't hear (or barge in on) itself. That unit is
/// picky about formats and fails on some device setups, so there's a plain
/// fallback mode without echo cancellation; in it the app stops forwarding mic
/// audio while the assistant speaks (half duplex) and the hotkey interrupts.
@MainActor
final class AudioSession {
    enum Mode: String { case voiceProcessing, plain }

    enum AudioError: LocalizedError {
        case permissionDenied
        case unavailable(String)

        var errorDescription: String? {
            switch self {
            case .permissionDenied:
                "Microphone permission denied. Allow it in System Settings → Privacy & Security → Microphone."
            case let .unavailable(detail): "Microphone unavailable: \(detail)"
            }
        }
    }

    private let log = Logger(subsystem: "com.vishnupartha.PersonalAgent", category: "audio")
    private var engine: AVAudioEngine?
    private let capture = AudioCapture()
    let playback = AudioPlayback()
    private(set) var mode: Mode?

    var isRunning: Bool { engine?.isRunning ?? false }

    static func requestMicrophoneAccess() async -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: true
        case .notDetermined: await AVCaptureDevice.requestAccess(for: .audio)
        default: false
        }
    }

    /// Starts the mic, trying voice processing first unless `mode` is given.
    /// The callbacks run on the audio thread.
    func start(
        mode requested: Mode? = nil,
        onAudio: @escaping @Sendable (Data) -> Void,
        onLevel: @escaping @Sendable (Float) -> Void
    ) async throws {
        guard !isRunning else { return }
        guard await Self.requestMicrophoneAccess() else { throw AudioError.permissionDenied }
        var lastError: Error?
        for mode in requested.map({ [$0] }) ?? [.voiceProcessing, .plain] {
            do {
                try startEngine(mode: mode, onAudio: onAudio, onLevel: onLevel)
                self.mode = mode
                log.notice("audio started in \(mode.rawValue, privacy: .public) mode")
                return
            } catch {
                log.error("audio start (\(mode.rawValue, privacy: .public)) failed: \(error, privacy: .public)")
                stop()
                lastError = error
            }
        }
        throw AudioError.unavailable(lastError?.localizedDescription ?? "unknown error")
    }

    func stop() {
        if let engine {
            capture.removeTap(from: engine.inputNode)
            playback.detach()
            engine.stop()
            if mode == .voiceProcessing { try? engine.inputNode.setVoiceProcessingEnabled(false) }
        }
        engine = nil
        mode = nil
    }

    // A fresh engine per start: toggling voice processing on a used engine
    // leaves its graph in states that fail to initialize.
    private func startEngine(
        mode: Mode,
        onAudio: @escaping @Sendable (Data) -> Void,
        onLevel: @escaping @Sendable (Float) -> Void
    ) throws {
        let engine = AVAudioEngine()
        self.engine = engine
        let input = engine.inputNode
        if mode == .voiceProcessing {
            try input.setVoiceProcessingEnabled(true)
            input.voiceProcessingOtherAudioDuckingConfiguration = .init(
                enableAdvancedDucking: true, duckingLevel: .min
            )
            // The voice-processing unit requires its client-side input and
            // output formats to agree (else -10875 at start), so drive the
            // output at the mic's sample rate.
            let rate = input.outputFormat(forBus: 0).sampleRate
            let out = AVAudioFormat(standardFormatWithSampleRate: rate, channels: 1)!
            engine.connect(engine.mainMixerNode, to: engine.outputNode, format: out)
        }
        let format = input.outputFormat(forBus: 0)
        log.notice("mic format (\(mode.rawValue, privacy: .public)): \(format.description, privacy: .public)")
        playback.attach(to: engine)
        try capture.installTap(on: input, format: format, onAudio: onAudio, onLevel: onLevel)
        engine.prepare()
        try engine.start()
    }
}
