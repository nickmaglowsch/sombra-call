#!/bin/sh
# Homebrew checks for a rendered cask, run by .github/workflows/homebrew.yml on macos-14.
#
#   ci.sh audit <cask.rb>                   brew style + brew audit --cask --strict
#   ci.sh smoke <cask.rb> <expected-version> install, sombra --version, uninstall --zap
#
# Recent Homebrew only audits and installs casks that live in a tap, so both put the
# rendered file in a throwaway local tap (sombra-ci/local) and remove it afterwards.
# smoke also proves that `brew uninstall --zap` leaves ~/Sombra/meetings alone.
# Set SOMBRA_SMOKE_ADHOC=1 for an ad-hoc signed app (a PR build): the quarantine flag is
# then cleared, since Gatekeeper only accepts a Developer ID signed, notarized app.
set -eu

TAP=sombra-ci/local
CASK="$TAP/sombra"

usage() {
  echo "usage: $0 audit <cask.rb> | smoke <cask.rb> <expected-version>" >&2
  exit 2
}

local_tap() {
  brew untap "$TAP" >/dev/null 2>&1 || true
  brew tap-new --no-git "$TAP" >/dev/null
  dir="$(brew --repository "$TAP")/Casks"
  mkdir -p "$dir"
  cp "$1" "$dir/sombra.rb"
  trap 'brew untap "$TAP" >/dev/null 2>&1 || true' EXIT
}

audit() {
  local_tap "$1"
  brew style --cask "$CASK"
  brew audit --cask --strict "$CASK"
}

smoke() {
  expected="$2"
  meetings="$HOME/Sombra/meetings"
  sentinel="$meetings/brew-smoke/transcript.md"
  mkdir -p "$(dirname "$sentinel")"
  echo "user data: must survive brew uninstall --zap" >"$sentinel"
  local_tap "$1"

  brew install --cask "$CASK"
  app="$(brew --prefix)/Caskroom/sombra"
  test -d "$app" || { echo "::error::$app missing after install" >&2; exit 1; }
  appdir=/Applications/Sombra.app
  test -d "$appdir" || { echo "::error::$appdir missing after install" >&2; exit 1; }
  if [ "${SOMBRA_SMOKE_ADHOC:-0}" = 1 ]; then
    xattr -dr com.apple.quarantine "$appdir" 2>/dev/null || true
  fi

  got="$(sombra --version)"
  echo "$got"
  case "$got" in
    *"$expected"*) ;;
    *) echo "::error::sombra --version printed '$got', expected $expected" >&2; exit 1 ;;
  esac

  brew uninstall --cask --zap "$CASK"
  if [ -e "$appdir" ] || command -v sombra >/dev/null 2>&1; then
    echo "::error::Sombra.app or the sombra shim is still there after uninstall" >&2
    exit 1
  fi
  for gone in "$HOME/.cache/sombra" "$HOME/.config/sombra"; do
    if [ -e "$gone" ]; then echo "::error::zap left $gone" >&2; exit 1; fi
  done
  if [ ! -f "$sentinel" ]; then
    echo "::error::brew uninstall --zap removed user data in $meetings" >&2
    exit 1
  fi
  echo "uninstall --zap removed the app, shim, config and cache; $meetings is intact"
}

[ $# -ge 2 ] || usage
case "$1" in
  audit) [ $# -eq 2 ] || usage; audit "$2" ;;
  smoke) [ $# -eq 3 ] || usage; smoke "$2" "$3" ;;
  *) usage ;;
esac
