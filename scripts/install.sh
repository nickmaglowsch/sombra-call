#!/bin/sh
# Sombra installer for macOS (14+, Apple Silicon) and Ubuntu 22.04/24.04.
#
#   curl -fsSL https://github.com/nickmaglowsch/sombra-call/releases/latest/download/install.sh | sh
#   curl -fsSL .../install.sh | sh -s -- --version v0.2.0 --yes
#   sh scripts/install.sh --from-wheel dist/sombra-0.2.0-py3-none-any.whl --yes --no-models
#   sh scripts/install.sh --from-app-zip Sombra-0.2.0-macos-arm64.zip --yes --no-models
#   sh scripts/install.sh --uninstall
#
# On macOS it installs Sombra.app (into /Applications, or ~/Applications), signed with the
# project's own certificate, and links its CLI shim onto PATH, so macOS grants the
# permissions to Sombra (ADR 0050).
# Elsewhere, or with --no-app, it installs uv (user directory, no sudo) if missing, then
# Sombra as a uv tool on a uv-managed Python 3.12. Then it downloads the local models,
# writes the default config and runs `sombra doctor`. Re-running upgrades in place.
# No telemetry; every step is echoed. See docs/install.md.

set -eu

REPO="${SOMBRA_REPO:-nickmaglowsch/sombra-call}"
PYTHON_VERSION="3.12"
UV_INSTALLER_URL="https://astral.sh/uv/install.sh"
# Ubuntu packages the installer itself needs (curl for downloads, CA certificates for
# HTTPS). Sombra's Python dependencies ship manylinux wheels and need nothing else yet.
APT_PACKAGES="curl ca-certificates"
# Sombra.app (packaging/macos/sombra_app.py; a test checks these agree with it).
BUNDLE_ID="io.github.nickmaglowsch.Sombra"
SHIM_RELPATH="Contents/Helpers/sombra"

VERSION="latest"
FROM_WHEEL=""
FROM_APP_ZIP=""
NO_APP=0
ASSUME_YES=0
NO_MODELS=0
MODIFY_PATH=0
UNINSTALL=0

TMP_DIR=""
UV=""
SOMBRA=""
BIN_DIR=""
APP_PATH=""

usage() {
    cat <<'EOF'
Usage: install.sh [options]

  --version vX.Y.Z   install this release (default: latest)
  --from-wheel PATH  install a local wheel instead of a release (testing, CI)
  --from-app-zip PATH
                     macOS: install a local Sombra-<version>-macos-arm64.zip
  --no-app           macOS: install with uv (grants go to your terminal app)
                     instead of Sombra.app
  --yes              non-interactive: answer yes to every question
  --no-models        skip downloading the whisper and Silero VAD models
  --modify-path      add the directory sombra is linked into to your shell rc file
  --uninstall        remove Sombra (Sombra.app and/or the uv tool, and after
                     confirmation the models); never touches ~/Sombra/meetings
                     or ~/.config/sombra
  -h, --help         show this help
EOF
}

say() { printf '==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}
run() {
    printf '+ %s\n' "$*"
    "$@"
}

cleanup() {
    if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR"
    fi
}
trap cleanup EXIT
# Stop on Ctrl-C / kill instead of carrying on with the next step; EXIT then cleans up.
trap 'exit 130' INT
trap 'exit 143' TERM

# ask "question" -> 0 for yes. --yes answers yes; with no terminal the answer is no.
ask() {
    if [ "$ASSUME_YES" -eq 1 ]; then
        printf '%s [y/N] y (--yes)\n' "$1"
        return 0
    fi
    if ! { true </dev/tty; } 2>/dev/null; then
        printf '%s [y/N] n (no terminal; pass --yes to accept)\n' "$1"
        return 1
    fi
    printf '%s [y/N] ' "$1"
    read -r answer </dev/tty || answer=""
    case "$answer" in
        y | Y | yes | YES | Yes) return 0 ;;
        *) return 1 ;;
    esac
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --version)
                [ $# -ge 2 ] || die "--version needs a value, e.g. --version v0.2.0"
                VERSION="$2"
                shift 2
                ;;
            --version=*)
                VERSION="${1#--version=}"
                shift
                ;;
            --from-wheel)
                [ $# -ge 2 ] || die "--from-wheel needs a path to a .whl file"
                FROM_WHEEL="$2"
                shift 2
                ;;
            --from-wheel=*)
                FROM_WHEEL="${1#--from-wheel=}"
                shift
                ;;
            --from-app-zip)
                [ $# -ge 2 ] || die "--from-app-zip needs a path to a .zip file"
                FROM_APP_ZIP="$2"
                shift 2
                ;;
            --from-app-zip=*)
                FROM_APP_ZIP="${1#--from-app-zip=}"
                shift
                ;;
            --no-app)
                NO_APP=1
                shift
                ;;
            --yes | -y)
                ASSUME_YES=1
                shift
                ;;
            --no-models)
                NO_MODELS=1
                shift
                ;;
            --modify-path)
                MODIFY_PATH=1
                shift
                ;;
            --uninstall)
                UNINSTALL=1
                shift
                ;;
            -h | --help)
                usage
                exit 0
                ;;
            *)
                usage >&2
                die "unknown argument: $1"
                ;;
        esac
    done
    if [ "$VERSION" != "latest" ]; then
        case "$VERSION" in
            v[0-9]*.[0-9]*.[0-9]*) ;;
            *) die "--version must look like vX.Y.Z (got '$VERSION')" ;;
        esac
    fi
    if [ -n "$FROM_WHEEL" ]; then
        [ -f "$FROM_WHEEL" ] || die "--from-wheel: no such file: $FROM_WHEEL"
        case "$FROM_WHEEL" in
            *.whl) ;;
            *) die "--from-wheel: not a .whl file: $FROM_WHEEL" ;;
        esac
    fi
    if [ -n "$FROM_APP_ZIP" ]; then
        [ -f "$FROM_APP_ZIP" ] || die "--from-app-zip: no such file: $FROM_APP_ZIP"
        case "$FROM_APP_ZIP" in
            *.zip) ;;
            *) die "--from-app-zip: not a .zip file: $FROM_APP_ZIP" ;;
        esac
        if [ -n "$FROM_WHEEL" ] || [ "$NO_APP" -eq 1 ]; then
            die "--from-app-zip cannot be combined with --from-wheel or --no-app"
        fi
    fi
}

# --- platform ------------------------------------------------------------------------

OS=""
ARCH=""
detect_platform() {
    say "Checking the platform"
    OS="$(uname -s)"
    ARCH="$(uname -m)"
    case "$OS" in
        Darwin)
            mac_version="$(sw_vers -productVersion 2>/dev/null || echo 0)"
            mac_major="${mac_version%%.*}"
            case "$mac_major" in
                '' | *[!0-9]*) mac_major=0 ;;
            esac
            if [ "$mac_major" -lt 14 ]; then
                die "macOS $mac_version is not supported: Sombra needs macOS 14 (Sonoma) or newer."
            fi
            if [ "$ARCH" != "arm64" ]; then
                die "macOS on $ARCH is not supported: Sombra needs Apple Silicon (arm64)."
            fi
            note "macOS $mac_version ($ARCH)"
            ;;
        Linux)
            distro=""
            distro_version=""
            if [ -r /etc/os-release ]; then
                # shellcheck disable=SC1091  # present on every systemd distribution
                distro="$(. /etc/os-release && printf '%s' "${ID:-}")"
                # shellcheck disable=SC1091
                distro_version="$(. /etc/os-release && printf '%s' "${VERSION_ID:-}")"
            fi
            if [ "$distro" != "ubuntu" ]; then
                die "Linux distribution '${distro:-unknown}' is not supported: Sombra supports Ubuntu 22.04 and 24.04."
            fi
            case "$distro_version" in
                22.04 | 24.04) ;;
                *) die "Ubuntu $distro_version is not supported: Sombra supports Ubuntu 22.04 and 24.04." ;;
            esac
            case "$ARCH" in
                x86_64 | aarch64) ;;
                *) die "Ubuntu on $ARCH is not supported: Sombra supports x86_64 and aarch64." ;;
            esac
            note "Ubuntu $distro_version ($ARCH)"
            note "live capture on Linux is not built yet (ADR 0016); replay, ask, minutes and report work"
            ;;
        *)
            die "$OS is not supported: Sombra runs on macOS 14+ (Apple Silicon) and Ubuntu 22.04/24.04."
            ;;
    esac
}

# --- system packages (Ubuntu) ----------------------------------------------------------

apt_missing() {
    missing=""
    for pkg in $APT_PACKAGES; do
        if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed"; then
            missing="$missing $pkg"
        fi
    done
    printf '%s' "${missing# }"
}

install_system_packages() {
    [ "$OS" = "Linux" ] || return 0
    say "Checking system packages ($APT_PACKAGES)"
    missing="$(apt_missing)"
    if [ -z "$missing" ]; then
        note "all present"
        return 0
    fi
    if [ "$(id -u)" -eq 0 ]; then
        sudo=""
    elif command -v sudo >/dev/null 2>&1; then
        sudo="sudo"
    else
        die "missing packages: $missing. Install them as root: apt-get install -y $missing"
    fi
    if ! ask "Install $missing with ${sudo:+sudo }apt-get?"; then
        die "missing packages: $missing. Install them yourself: ${sudo:+sudo }apt-get install -y $missing"
    fi
    # shellcheck disable=SC2086  # $sudo and $missing are word lists on purpose
    run $sudo apt-get update
    # shellcheck disable=SC2086
    run $sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y $missing
}

# --- uv --------------------------------------------------------------------------------

find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi
    for candidate in "${XDG_BIN_HOME:-}/uv" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

ensure_uv() {
    say "Checking uv"
    if UV="$(find_uv)"; then
        note "found $UV ($("$UV" --version))"
        return 0
    fi
    command -v curl >/dev/null 2>&1 || die "curl is needed to install uv"
    note "uv not found; installing it into your user directory with the official installer"
    printf '+ curl -LsSf %s | env UV_NO_MODIFY_PATH=1 sh\n' "$UV_INSTALLER_URL"
    curl -LsSf "$UV_INSTALLER_URL" | env UV_NO_MODIFY_PATH=1 sh
    UV="$(find_uv)" || die "uv was installed but cannot be found; add ~/.local/bin to PATH and re-run"
    note "installed $UV ($("$UV" --version))"
}

# --- release download ------------------------------------------------------------------

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d ' ' -f 1
    else
        shasum -a 256 "$1" | cut -d ' ' -f 1
    fi
}

release_base_url() {
    if [ -n "${SOMBRA_RELEASE_BASE_URL:-}" ]; then
        printf '%s\n' "$SOMBRA_RELEASE_BASE_URL"
    elif [ "$VERSION" = "latest" ]; then
        printf 'https://github.com/%s/releases/latest/download\n' "$REPO"
    else
        printf 'https://github.com/%s/releases/download/%s\n' "$REPO" "$VERSION"
    fi
}

# Downloads the release's SHA256SUMS into $TMP_DIR (once).
fetch_sums() {
    [ -f "$TMP_DIR/SHA256SUMS" ] && return 0
    base="$(release_base_url)"
    say "Downloading Sombra ($VERSION) from $base" >&2
    printf '+ curl -fsSL -o SHA256SUMS %s/SHA256SUMS\n' "$base" >&2
    curl -fsSL -o "$TMP_DIR/SHA256SUMS" "$base/SHA256SUMS" ||
        die "could not download $base/SHA256SUMS (does release $VERSION exist?)"
}

# download_release_asset ERE: downloads the asset SHA256SUMS lists under a name matching
# ERE into $TMP_DIR, verifies it, prints its path. Exits 3 when no asset matches.
download_release_asset() {
    fetch_sums
    base="$(release_base_url)"
    line="$(grep -E "[[:space:]]\\*?$1\$" "$TMP_DIR/SHA256SUMS" | head -n 1 || true)"
    [ -n "$line" ] || exit 3
    expected="$(printf '%s' "$line" | awk '{print $1}')"
    asset="$(printf '%s' "$line" | awk '{print $2}')"
    asset="${asset#\*}"
    case "$asset" in
        */* | *..*) die "unexpected asset name in SHA256SUMS: $asset" ;;
    esac
    printf '+ curl -fsSL -o %s %s/%s\n' "$asset" "$base" "$asset" >&2
    curl -fsSL -o "$TMP_DIR/$asset" "$base/$asset" || die "could not download $base/$asset"
    actual="$(sha256_of "$TMP_DIR/$asset")"
    if [ "$actual" != "$expected" ]; then
        die "checksum mismatch for $asset: expected $expected, got $actual. Not installing."
    fi
    note "sha256 ok: $asset" >&2
    printf '%s\n' "$TMP_DIR/$asset"
}

# Downloads the release wheel into $TMP_DIR, verifies it against SHA256SUMS, prints its path.
download_release_wheel() {
    rc=0
    path="$(download_release_asset 'sombra-[^[:space:]/]*-py3-none-any\.whl')" || rc=$?
    [ "$rc" -ne 3 ] || die "SHA256SUMS lists no sombra wheel"
    [ "$rc" -eq 0 ] || exit "$rc"
    printf '%s\n' "$path"
}

# --- install -----------------------------------------------------------------------------

install_sombra() {
    if [ -n "$FROM_WHEEL" ]; then
        wheel="$FROM_WHEEL"
        say "Installing Sombra from $wheel"
    else
        wheel="$(download_release_wheel)"
        say "Installing Sombra from the release wheel"
    fi
    # uv-managed CPython only, so the system Python never matters. --force replaces an
    # existing install; --reinstall-package makes re-running the same version refresh it.
    run env UV_PYTHON_PREFERENCE=only-managed "$UV" tool install \
        --python "$PYTHON_VERSION" --force --reinstall-package sombra "$wheel"
    BIN_DIR="$("$UV" tool dir --bin)"
    SOMBRA="$BIN_DIR/sombra"
    [ -x "$SOMBRA" ] || die "sombra was not installed at $SOMBRA"
    run "$SOMBRA" --version
}

# --- Sombra.app (macOS) -------------------------------------------------------------------

bundle_id_of() {
    plutil -extract CFBundleIdentifier raw -o - "$1/Contents/Info.plist" 2>/dev/null
}

default_bin_dir() {
    printf '%s\n' "${XDG_BIN_HOME:-$HOME/.local/bin}"
}

# Where Sombra.app is installed (ours, by bundle id), or nothing. $SOMBRA_APP_DIR, when
# set, is the only place looked at: the tests set it, and must never find (and upgrade or
# uninstall) the real /Applications/Sombra.app.
installed_app() {
    if [ -n "${SOMBRA_APP_DIR:-}" ]; then
        set -- "$SOMBRA_APP_DIR"
    else
        set -- /Applications "$HOME/Applications"
    fi
    for dir in "$@"; do
        if [ -d "$dir/Sombra.app" ] && [ "$(bundle_id_of "$dir/Sombra.app" || true)" = "$BUNDLE_ID" ]; then
            printf '%s\n' "$dir/Sombra.app"
            return 0
        fi
    done
    return 1
}

# Where a new install goes: $SOMBRA_APP_DIR, /Applications if writable, or ~/Applications.
app_install_dir() {
    if [ -n "${SOMBRA_APP_DIR:-}" ]; then
        printf '%s\n' "$SOMBRA_APP_DIR"
    elif [ -w /Applications ]; then
        printf '%s\n' /Applications
    else
        printf '%s\n' "$HOME/Applications"
    fi
}

# Installs Sombra.app, links its CLI shim and sets APP_PATH. Leaves APP_PATH empty when
# the release has no app, so the caller falls back to the uv install.
install_app() {
    if [ -n "$FROM_APP_ZIP" ]; then
        zip="$FROM_APP_ZIP"
        say "Installing Sombra.app from $zip"
    else
        rc=0
        zip="$(download_release_asset 'Sombra-[^[:space:]/]*-macos-arm64\.zip')" || rc=$?
        if [ "$rc" -eq 3 ]; then
            note "release $VERSION has no Sombra.app; installing with uv instead"
            return 0
        fi
        [ "$rc" -eq 0 ] || exit "$rc"
        say "Installing Sombra.app from the release"
    fi
    unpack="$TMP_DIR/app"
    mkdir -p "$unpack"
    run ditto -x -k "$zip" "$unpack"
    [ -d "$unpack/Sombra.app" ] || die "$zip holds no Sombra.app"
    got="$(bundle_id_of "$unpack/Sombra.app" || true)"
    [ "$got" = "$BUNDLE_ID" ] || die "$zip holds an app with bundle id '$got', not $BUNDLE_ID"
    run codesign --verify --deep --strict "$unpack/Sombra.app" ||
        die "Sombra.app's code signature does not verify; not installing"
    if codesign -dv "$unpack/Sombra.app" 2>&1 | grep -q '^Signature=adhoc'; then
        warn "this Sombra.app is ad-hoc signed (a development build or a labelled pre-release): macOS will ask for the permissions again after every upgrade"
    fi
    if xattr -p com.apple.quarantine "$zip" >/dev/null 2>&1; then
        note "$zip is quarantined (downloaded by a browser); the quarantine is removed below"
    fi

    if dest="$(installed_app)"; then
        note "upgrading $dest (the permissions you granted stay)"
        run rm -rf "$dest"
    else
        dir="$(app_install_dir)"
        run mkdir -p "$dir"
        dest="$dir/Sombra.app"
        if [ -e "$dest" ]; then
            die "$dest exists and is not Sombra ($BUNDLE_ID); move it away and re-run"
        fi
    fi
    run ditto "$unpack/Sombra.app" "$dest"
    # Sombra.app is self-signed, not notarized (ADR 0050), so Gatekeeper blocks it when it
    # carries com.apple.quarantine. curl sets none, but a browser-downloaded zip does and
    # ditto copies it into the app: remove it.
    run xattr -dr com.apple.quarantine "$dest" 2>/dev/null || true
    APP_PATH="$dest"

    # The app replaces a uv-tool install: one sombra on PATH, and it is the app's.
    if UV="$(find_uv)" && "$UV" tool list 2>/dev/null | grep -q '^sombra '; then
        note "replacing the uv-tool install of sombra with Sombra.app"
        run "$UV" tool uninstall sombra
    fi
    BIN_DIR="$(default_bin_dir)"
    run mkdir -p "$BIN_DIR"
    run ln -sfn "$APP_PATH/$SHIM_RELPATH" "$BIN_DIR/sombra"
    SOMBRA="$BIN_DIR/sombra"
    [ -x "$SOMBRA" ] || die "the CLI shim is missing from $APP_PATH"
    run "$SOMBRA" --version
}

download_models() {
    if [ "$NO_MODELS" -eq 1 ]; then
        say "Skipping models (--no-models); run 'sombra models download' later"
        return 0
    fi
    say "Downloading the whisper and Silero VAD models (SHA-256 checked)"
    run "$SOMBRA" models download
}

rc_file() {
    case "${SHELL:-}" in
        */zsh) printf '%s\n' "${ZDOTDIR:-$HOME}/.zshrc" ;;
        */bash)
            if [ "$OS" = "Darwin" ]; then
                printf '%s\n' "$HOME/.bash_profile"
            else
                printf '%s\n' "$HOME/.bashrc"
            fi
            ;;
        *) printf '%s\n' "$HOME/.profile" ;;
    esac
}

check_path() {
    say "Checking PATH"
    case ":$PATH:" in
        *":$BIN_DIR:"*)
            note "$BIN_DIR is on PATH"
            return 0
            ;;
    esac
    line="export PATH=\"$BIN_DIR:\$PATH\""
    rc="$(rc_file)"
    if [ "$MODIFY_PATH" -eq 1 ]; then
        if [ -f "$rc" ] && grep -qF "$line" "$rc"; then
            note "$rc already adds $BIN_DIR; open a new shell"
        else
            printf '+ append to %s: %s\n' "$rc" "$line"
            printf '\n# Added by the Sombra installer\n%s\n' "$line" >>"$rc"
            note "open a new shell, or run: $line"
        fi
    else
        note "$BIN_DIR is not on PATH. Add this line to $rc (or re-run with --modify-path):"
        note ""
        note "  $line"
        note ""
    fi
}

write_config() {
    say "Writing the default config (kept if it exists)"
    run "$SOMBRA" config init
}

grant_hint() {
    if [ -n "$APP_PATH" ]; then
        printf '%s' "macOS asks you to grant Microphone, Screen Recording and Accessibility
       to Sombra (System Settings > Privacy & Security lists Sombra)."
    elif [ "$OS" = "Darwin" ]; then
        printf '%s' "On macOS, grant Microphone, Screen Recording and Accessibility to the terminal
       app you run sombra from (System Settings > Privacy & Security)."
    else
        printf '%s' "Linux needs no permission grants."
    fi
}

run_doctor() {
    say "Running sombra doctor"
    run "$SOMBRA" doctor || true
    cat <<EOF

==> Next steps
    1. Fix anything 'sombra doctor' marked FAIL or warn above, then run it again.
       $(grant_hint)
    2. Choose the agent provider: 'sombra setup' (Claude or Codex, on your
       subscription or an API key; see docs/providers.md).
    3. First meeting: sombra start "Daily" on macOS (see docs/usage.md).
EOF
}

# --- uninstall ---------------------------------------------------------------------------

uninstall() {
    say "Uninstalling Sombra"
    app=""
    if [ "$(uname -s)" = "Darwin" ]; then
        app="$(installed_app || true)"
    fi
    UV="$(find_uv || true)"
    if [ -z "$app" ] && [ -z "$UV" ]; then
        die "neither Sombra.app nor uv found; nothing to uninstall"
    fi
    models_dir="$HOME/.cache/sombra/models"
    if [ -n "$app" ]; then
        bin="$app/$SHIM_RELPATH"
    else
        bin="$("$UV" tool dir --bin)/sombra"
    fi
    if [ -x "$bin" ]; then
        models_dir="$("$bin" models path 2>/dev/null || printf '%s' "$models_dir")"
    fi
    if [ -n "$app" ]; then
        run rm -rf "$app"
        link="$(default_bin_dir)/sombra"
        if [ -L "$link" ]; then
            case "$(readlink "$link")" in
                */Sombra.app/*) run rm -f "$link" ;;
            esac
        fi
        note "macOS keeps the permissions you granted Sombra; to remove them too, run:"
        note "  tccutil reset All $BUNDLE_ID"
    fi
    if [ -n "$UV" ]; then
        if "$UV" tool list 2>/dev/null | grep -q '^sombra '; then
            run "$UV" tool uninstall sombra
        else
            note "sombra is not installed as a uv tool"
        fi
    fi
    if [ -d "$models_dir" ]; then
        case "$models_dir" in
            */models) ;;
            *) die "refusing to delete unexpected models path: $models_dir" ;;
        esac
        if ask "Delete the downloaded models in $models_dir?"; then
            # Only the files Sombra downloads (and their partial downloads); anything
            # else the user put there stays, and so does the folder then.
            for f in "$models_dir"/ggml-*.bin "$models_dir"/ggml-*.bin.part \
                "$models_dir"/silero_vad.onnx "$models_dir"/silero_vad.onnx.part; do
                if [ -f "$f" ]; then
                    run rm -f "$f"
                fi
            done
            if rmdir "$models_dir" 2>/dev/null; then
                note "removed $models_dir"
            else
                note "kept $models_dir: it holds files the installer did not download"
            fi
        else
            note "kept $models_dir"
        fi
    fi
    note "kept your meetings (~/Sombra/meetings) and config (~/.config/sombra)"
    if [ -n "$UV" ]; then
        note "uv itself was left installed"
    fi
}

main() {
    parse_args "$@"
    if [ "$UNINSTALL" -eq 1 ]; then
        uninstall
        return 0
    fi
    if [ -n "$FROM_APP_ZIP" ] && [ "$(uname -s)" != "Darwin" ]; then
        die "--from-app-zip is for macOS only"
    fi
    detect_platform
    TMP_DIR="$(mktemp -d)"
    install_system_packages
    if [ "$OS" = "Darwin" ] && [ "$NO_APP" -eq 0 ] && [ -z "$FROM_WHEEL" ]; then
        install_app
    fi
    if [ -z "$APP_PATH" ]; then
        ensure_uv
        install_sombra
    fi
    download_models
    check_path
    write_config
    run_doctor
    say "Sombra is installed: $SOMBRA"
}

main "$@"
