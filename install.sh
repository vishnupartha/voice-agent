#!/usr/bin/env bash
# Builds Personal Agent from this checkout and installs it in Applications.
#
#   ./install.sh               speech models + app (Gemini as the brain, ~3 GB)
#   ./install.sh --with-local  also the local Qwen3.5 brain and Qwen3-TTS voice (~11 GB more)
#   ./install.sh --no-install  build only; don't copy to Applications or launch
#
# Re-run it any time to update after `git pull`. Keep this folder: the app runs
# its backend from here.
set -euo pipefail
cd "$(dirname "$0")"

WITH_LOCAL=0
INSTALL=1
for arg in "$@"; do
  case "$arg" in
    --with-local) WITH_LOCAL=1 ;;
    --no-install) INSTALL=0 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg (see --help)"; exit 1 ;;
  esac
done

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

step "Checking this Mac"
[[ "$(uname -m)" == "arm64" ]] || fail "Personal Agent needs an Apple Silicon Mac (M1 or later)."
macos_major="$(sw_vers -productVersion | cut -d. -f1)"
(( macos_major >= 26 )) || fail "Personal Agent needs macOS 26 (Tahoe) or later; this Mac has $(sw_vers -productVersion)."
xcodebuild -version >/dev/null 2>&1 || fail "Xcode is needed to build the app. Install it from the App Store, open it once, then run:
  sudo xcode-select -s /Applications/Xcode.app"

if ! command -v uv >/dev/null; then
  step "Installing uv (Python package manager)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
if ! command -v xcodegen >/dev/null; then
  command -v brew >/dev/null || fail "XcodeGen is needed. Install Homebrew (https://brew.sh), then re-run this script."
  step "Installing XcodeGen"
  brew install xcodegen
fi

step "Installing Python dependencies"
uv sync

step "Downloading speech models (resumes if interrupted)"
uv run python -m scripts.download_models
if (( WITH_LOCAL )); then
  uv run python -m scripts.download_models llm tts
fi

step "Building the app"
xcodegen generate --quiet
xcodebuild -project PersonalAgent.xcodeproj -scheme PersonalAgent -configuration Release \
  -destination 'platform=macOS,arch=arm64' -derivedDataPath build/DerivedData -quiet build
app="build/DerivedData/Build/Products/Release/PersonalAgent.app"
bundle_id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$app/Contents/Info.plist")"

if (( ! INSTALL )); then
  echo; echo "Built $app"; exit 0
fi

step "Installing"
dest="/Applications"
[[ -w "$dest" ]] || dest="$HOME/Applications"
mkdir -p "$dest"
osascript -e 'quit app "Personal Agent"' >/dev/null 2>&1 || true
pkill -f "uvicorn backend.api.server" >/dev/null 2>&1 || true
rm -rf "$dest/PersonalAgent.app"
cp -R "$app" "$dest/"

# An ad-hoc signed build gets a new signature each time, and macOS then feeds it
# silence instead of the microphone. Resetting the permission makes macOS ask again.
if codesign -dv "$dest/PersonalAgent.app" 2>&1 | grep -q "Signature=adhoc"; then
  tccutil reset Microphone "$bundle_id" >/dev/null 2>&1 || true
fi

open "$dest/PersonalAgent.app"
cat <<EOF

Personal Agent is installed in $dest and running: look for ◉ in the menu bar.
  • The Settings window opens on first run: paste a free Gemini API key there.
  • Press Option+Space anywhere to talk. Allow microphone access when asked.
  • Optional: connect Gmail and Calendar from Settings (see README).
EOF
