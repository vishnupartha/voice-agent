import AVFoundation

/// Taps the microphone and converts it to the wire format: 16 kHz mono PCM16 (spec §11).
@MainActor
final class AudioCapture {
    enum CaptureError: LocalizedError {
        case noConverter(String)
        var errorDescription: String? {
            if case let .noConverter(format) = self { return "Can't convert mic format \(format) to 16 kHz PCM." }
            return nil
        }
    }

    private var tapInstalled = false

    /// `onAudio` gets 16 kHz PCM16 chunks; `onLevel` gets each chunk's peak (0...1).
    func installTap(
        on input: AVAudioInputNode,
        format: AVAudioFormat,
        onAudio: @escaping @Sendable (Data) -> Void,
        onLevel: @escaping @Sendable (Float) -> Void
    ) throws {
        guard let converter = PCM16Converter(from: format) else {
            throw CaptureError.noConverter(format.description)
        }
        // Explicitly @Sendable: the tap runs on the audio thread, and a closure
        // inferred as main-actor-isolated would trap there.
        input.installTap(onBus: 0, bufferSize: 2048, format: format) { @Sendable buffer, _ in
            if let data = converter.convert(buffer) {
                onAudio(data)
                onLevel(PCM16Converter.peak(data))
            }
        }
        tapInstalled = true
    }

    func removeTap(from input: AVAudioInputNode) {
        guard tapInstalled else { return }
        input.removeTap(onBus: 0)
        tapInstalled = false
    }
}

/// Converts mic buffers to 16 kHz mono PCM16. Used only from the audio tap
/// thread, one buffer at a time.
///
/// With voice processing on, macOS exposes the mic as several channels (3 or 9
/// on Apple Silicon Macs) and puts the echo-cancelled voice in channel 0.
/// AVAudioConverter can't downmix that layout to mono and outputs silence, so
/// channel 0 is copied out first and only then resampled.
private final class PCM16Converter: @unchecked Sendable {
    static let target = AVAudioFormat(
        commonFormat: .pcmFormatInt16, sampleRate: 16_000, channels: 1, interleaved: true
    )!

    private let mono: AVAudioFormat
    private let converter: AVAudioConverter

    init?(from format: AVAudioFormat) {
        guard format.commonFormat == .pcmFormatFloat32,
              let mono = AVAudioFormat(standardFormatWithSampleRate: format.sampleRate, channels: 1),
              let converter = AVAudioConverter(from: mono, to: Self.target)
        else { return nil }
        self.mono = mono
        self.converter = converter
    }

    static func peak(_ data: Data) -> Float {
        data.withUnsafeBytes { raw in
            var peak: Int16 = 0
            for i in stride(from: 0, to: raw.count - 1, by: 2) {
                let v = raw.loadUnaligned(fromByteOffset: i, as: Int16.self)
                peak = max(peak, v == .min ? .max : abs(v))
            }
            return Float(peak) / Float(Int16.max)
        }
    }

    func convert(_ buffer: AVAudioPCMBuffer) -> Data? {
        guard let channels = buffer.floatChannelData, buffer.frameLength > 0,
              let channel0 = AVAudioPCMBuffer(pcmFormat: mono, frameCapacity: buffer.frameLength),
              let dst = channel0.floatChannelData?[0]
        else { return nil }
        channel0.frameLength = buffer.frameLength
        // Non-interleaved Float32: channel 0 is contiguous.
        dst.update(from: channels[0], count: Int(buffer.frameLength))

        let ratio = Self.target.sampleRate / mono.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 32
        guard let out = AVAudioPCMBuffer(pcmFormat: Self.target, frameCapacity: capacity) else { return nil }
        // The input block runs synchronously inside convert(), on this thread.
        nonisolated(unsafe) var supplied = false
        nonisolated(unsafe) let input = channel0
        var error: NSError?
        converter.convert(to: out, error: &error) { _, status in
            if supplied {
                status.pointee = .noDataNow
                return nil
            }
            supplied = true
            status.pointee = .haveData
            return input
        }
        guard error == nil, out.frameLength > 0, let samples = out.int16ChannelData else { return nil }
        return Data(bytes: samples[0], count: Int(out.frameLength) * MemoryLayout<Int16>.size)
    }
}
