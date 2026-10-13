import Carbon.HIToolbox

/// A system-wide hotkey via Carbon's RegisterEventHotKey (spec §7, §14).
/// It needs no Accessibility permission, unlike event taps.
@MainActor
final class GlobalHotkey {
    private static var actions: [UInt32: @MainActor () -> Void] = [:]
    private static var handlerInstalled = false
    private static var nextID: UInt32 = 1

    private var ref: EventHotKeyRef?
    private let id: UInt32

    /// Defaults to Option+Space.
    init?(keyCode: Int = kVK_Space, modifiers: Int = optionKey, action: @escaping @MainActor () -> Void) {
        Self.installHandler()
        id = Self.nextID
        Self.nextID += 1
        let hotKeyID = EventHotKeyID(signature: OSType(0x5041_4754), id: id) // 'PAGT'
        let status = RegisterEventHotKey(
            UInt32(keyCode), UInt32(modifiers), hotKeyID, GetApplicationEventTarget(), 0, &ref
        )
        guard status == noErr else { return nil } // already taken by another app
        Self.actions[id] = action
    }

    func unregister() {
        if let ref { UnregisterEventHotKey(ref) }
        ref = nil
        Self.actions[id] = nil
    }

    private static func installHandler() {
        guard !handlerInstalled else { return }
        handlerInstalled = true
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, event, _ in
            var hotKeyID = EventHotKeyID()
            GetEventParameter(
                event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                nil, MemoryLayout<EventHotKeyID>.size, nil, &hotKeyID
            )
            let id = hotKeyID.id
            // Carbon delivers application events on the main thread.
            MainActor.assumeIsolated { GlobalHotkey.actions[id]?() }
            return noErr
        }, 1, &spec, nil, nil)
    }
}
