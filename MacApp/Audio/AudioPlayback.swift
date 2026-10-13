import AVFoundation

/// Plays the assistant's streamed PCM16 speech (spec §12).
@MainActor
final class AudioPlayback {
    /// Speaking speed. Time-stretched, so pitch is unchanged.
    static let rate: Float = 1.2
    /// Audio buffered before playback starts, to ride out generation jitter
    /// (TTS shares the GPU thread with the LLM).
    static let cushionSeconds = 0.3

    private var engine: AVAudioEngine?
    private var player = AVAudioPlayerNode()
    private var timePitch = AVAudioUnitTimePitch()
    private var queuedSeconds = 0.0
    private var format: AVAudioFormat?
    private var attached = false
    private var pending = 0
    private var streamEnded = false
    /// Bumped by `stop()` so completions of discarded buffers are ignored.
    private var generation = 0

    /// Called when a finished stream has fully played out (not after `stop()`).
    var onFinished: (() -> Void)?

    var isPlaying: Bool { pending > 0 }

    func attach(to engine: AVAudioEngine) {
        detach()
        self.engine = engine
        player = AVAudioPlayerNode()
        timePitch = AVAudioUnitTimePitch()
        timePitch.rate = Self.rate
        engine.attach(player)
        engine.attach(timePitch)
        format = nil
        connect(sampleRate: 24_000)
        attached = true
    }

    func detach() {
        stop()
        attached = false
        engine = nil
    }

    /// A new utterance is starting at `sampleRate`.
    func begin(sampleRate: Int) {
        guard attached else { return }
        stop()
        if format?.sampleRate != Double(sampleRate) { connect(sampleRate: Double(sampleRate)) }
    }

    func enqueue(_ data: Data) {
        guard attached, engine?.isRunning == true, let format else { return }
        let frames = data.count / MemoryLayout<Int16>.size
        guard frames > 0,
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames)),
              let dst = buffer.floatChannelData?[0]
        else { return }
        buffer.frameLength = AVAudioFrameCount(frames)
        data.withUnsafeBytes { raw in
            for i in 0 ..< frames {
                dst[i] = Float(Int16(littleEndian: raw.loadUnaligned(fromByteOffset: i * 2, as: Int16.self))) / 32768
            }
        }
        pending += 1
        queuedSeconds += Double(frames) / format.sampleRate
        let gen = generation
        player.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { @Sendable _ in
            Task { @MainActor [weak self] in self?.bufferPlayed(gen) }
        }
        if !player.isPlaying && queuedSeconds >= Self.cushionSeconds { player.play() }
    }

    /// The backend has sent the whole utterance.
    func endOfStream() {
        streamEnded = true
        if pending == 0 { finish() } else if !player.isPlaying { player.play() }
    }

    /// Stop immediately and drop anything queued (barge-in, cancel).
    func stop() {
        generation += 1
        pending = 0
        queuedSeconds = 0
        streamEnded = false
        if attached { player.stop() }
    }

    private func bufferPlayed(_ gen: Int) {
        guard gen == generation else { return }
        pending -= 1
        if pending == 0 && streamEnded { finish() }
    }

    private func finish() {
        streamEnded = false
        onFinished?()
    }

    private func connect(sampleRate: Double) {
        guard let engine else { return }
        let format = AVAudioFormat(standardFormatWithSampleRate: sampleRate, channels: 1)!
        // The time-pitch unit doesn't resample: its input and output formats
        // must match, or the engine fails to start (-10868).
        engine.disconnectNodeOutput(player)
        engine.disconnectNodeOutput(timePitch)
        engine.connect(player, to: timePitch, format: format)
        engine.connect(timePitch, to: engine.mainMixerNode, format: format)
        self.format = format
    }
}
