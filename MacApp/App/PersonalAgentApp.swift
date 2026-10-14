import AppKit
import SwiftUI

@main
struct PersonalAgentApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        MenuBarExtra("Personal Agent", systemImage: "circle.circle.fill") {
            MenuBarMenu(state: appDelegate.state)
        }

        Window("Personal Agent", id: "main") {
            MainWindow(state: appDelegate.state)
        }
        .defaultSize(width: 700, height: 760)
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let state = AppState()
    private var overlay: VoiceOverlayController?
    private var hotkey: GlobalHotkey?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let overlay = VoiceOverlayController(state: state)
        self.overlay = overlay
        state.onVoiceActiveChanged = { active in active ? overlay.show() : overlay.hide() }
        hotkey = GlobalHotkey { [weak self] in self?.state.hotkeyPressed() }
        Task {
            await state.start()
            #if DEBUG
            // `open PersonalAgent.app --args -autoStartVoice YES` opens the mic on
            // launch, for checking the audio path without pressing the hotkey.
            if UserDefaults.standard.bool(forKey: "autoStartVoice") { await state.startVoice() }
            #endif
        }
    }

    // Closing the window keeps the app in the menu bar (spec §84).
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationWillTerminate(_ notification: Notification) {
        state.shutdown()
    }
}

private struct MenuBarMenu: View {
    let state: AppState
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Text("Personal Agent — \(statusText)")
        Divider()
        Button(state.voice == .off ? "Talk to Assistant  ⌥Space" : "Stop Listening  ⌥Space") {
            state.toggleVoice()
        }
        googleItem
        Button("Open Dashboard") {
            openWindow(id: "main")
            NSApp.activate()
        }
        .keyboardShortcut("o")
        if case .unavailable = state.backend {
            Button("Restart Backend") { Task { await state.start() } }
        }
        Divider()
        Button("Quit") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
    }

    @ViewBuilder private var googleItem: some View {
        switch state.google {
        case .connected:
            Button("Google: Connected — Disconnect") { state.disconnectGoogle() }
        case .connecting:
            Text("Google: waiting for sign-in in your browser…")
        case .notConfigured:
            // The client secret may be saved while the app is running; let the user re-check.
            Button("Google: not set up — Check Again") { Task { await state.refreshConnections() } }
        case .disconnected, .unknown:
            Button("Connect Google…") { state.connectGoogle() }
                .disabled(state.backend != .ready)
        }
    }

    private var statusText: String {
        switch state.backend {
        case .starting: "Starting…"
        case .ready: "Ready"
        case .unavailable: "Backend unavailable"
        }
    }
}
