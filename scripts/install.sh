#!/bin/sh
# Sombra installer for macOS (14+, Apple Silicon) and Ubuntu 22.04/24.04.
#
#   curl -fsSL https://github.com/nickmaglowsch/sombra-call/releases/latest/download/install.sh | sh
#   curl -fsSL .../install.sh | sh -s -- --version v0.2.0 --yes
#   sh scripts/install.sh --from-wheel dist/sombra-0.2.0-py3-none-any.whl --yes --no-models
#   sh scripts/install.sh --uninstall
#
# Installs uv (user directory, no sudo) if missing, then Sombra as a uv tool on a
# uv-managed Python 3.12, downloads the local models, writes the default config and
# runs `sombra doctor`. Re-running upgrades in place. No telemetry; every step is echoed.
# See docs/install.md.

set -eu

REPO="${SOMBRA_REPO:-nickmaglowsch/sombra-call}"
PYTHON_VERSION="3.12"
UV_INSTALLER_URL="https://astral.sh/uv/install.sh"
# Ubuntu packages the installer itself needs (curl for downloads, CA certificates for
# HTTPS). Sombra's Python dependencies ship manylinux wheels and need nothing else yet.
APT_PACKAGES="curl ca-certificates"

VERSION="latest"
FROM_WHEEL=""
ASSUME_YES=0
NO_MODELS=0
MODIFY_PATH=0
UNINSTALL=0

TMP_DIR=""
UV=""
SOMBRA=""
BIN_DIR=""

usage() {
    cat <<'EOF'
Usage: install.sh [options]

  --version vX.Y.Z   install this release (default: latest)
  --from-wheel PATH  install a local wheel instead of a release (testing, CI)
  --yes              non-interactive: answer yes to every question
  --no-models        skip downloading the whisper and Silero VAD models
  --modify-path      add uv's tool bin directory to your shell rc file
  --uninstall        remove Sombra (and, after confirmation, the models);
                     never touches ~/Sombra/meetings or ~/.config/sombra
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

# Downloads the release wheel into $TMP_DIR, verifies it against SHA256SUMS, prints its path.
download_release_wheel() {
    base="$(release_base_url)"
    say "Downloading Sombra ($VERSION) from $base" >&2
    printf '+ curl -fsSL -o SHA256SUMS %s/SHA256SUMS\n' "$base" >&2
    curl -fsSL -o "$TMP_DIR/SHA256SUMS" "$base/SHA256SUMS" ||
        die "could not download $base/SHA256SUMS (does release $VERSION exist?)"
    line="$(grep -E '[[:space:]]\*?sombra-[^[:space:]]*-py3-none-any\.whl$' "$TMP_DIR/SHA256SUMS" | head -n 1 || true)"
    [ -n "$line" ] || die "SHA256SUMS lists no sombra wheel"
    expected="$(printf '%s' "$line" | awk '{print $1}')"
    wheel="$(printf '%s' "$line" | awk '{print $2}')"
    wheel="${wheel#\*}"
    case "$wheel" in
        */* | *..*) die "unexpected wheel name in SHA256SUMS: $wheel" ;;
    esac
    printf '+ curl -fsSL -o %s %s/%s\n' "$wheel" "$base" "$wheel" >&2
    curl -fsSL -o "$TMP_DIR/$wheel" "$base/$wheel" || die "could not download $base/$wheel"
    actual="$(sha256_of "$TMP_DIR/$wheel")"
    if [ "$actual" != "$expected" ]; then
        die "checksum mismatch for $wheel: expected $expected, got $actual. Not installing."
    fi
    note "sha256 ok: $wheel" >&2
    printf '%s\n' "$TMP_DIR/$wheel"
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

run_doctor() {
    say "Running sombra doctor"
    run "$SOMBRA" doctor || true
    cat <<EOF

==> Next steps
    1. Fix anything 'sombra doctor' marked FAIL or warn above, then run it again.
       On macOS, grant Microphone, Screen Recording and Accessibility to the terminal
       app you run sombra from (System Settings > Privacy & Security).
    2. Choose the agent provider: 'sombra setup' (coming with R4; Claude or Codex).
    3. Or store an API key in the OS keychain: 'sombra auth set anthropic'.
    4. Start your first meeting: sombra new "Daily" (see docs/install.md).
EOF
}

# --- uninstall ---------------------------------------------------------------------------

uninstall() {
    say "Uninstalling Sombra"
    if UV="$(find_uv)"; then
        :
    else
        die "uv not found; nothing to uninstall"
    fi
    models_dir="$HOME/.cache/sombra/models"
    bin="$("$UV" tool dir --bin)/sombra"
    if [ -x "$bin" ]; then
        models_dir="$("$bin" models path 2>/dev/null || printf '%s' "$models_dir")"
    fi
    if "$UV" tool list 2>/dev/null | grep -q '^sombra '; then
        run "$UV" tool uninstall sombra
    else
        note "sombra is not installed as a uv tool"
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
    note "uv itself was left installed"
}

main() {
    parse_args "$@"
    if [ "$UNINSTALL" -eq 1 ]; then
        uninstall
        return 0
    fi
    detect_platform
    TMP_DIR="$(mktemp -d)"
    install_system_packages
    ensure_uv
    install_sombra
    download_models
    check_path
    write_config
    run_doctor
    say "Sombra is installed: $SOMBRA"
}

main "$@"
