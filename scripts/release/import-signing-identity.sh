#!/bin/sh
# Import a code-signing .p12 into a new, temporary keychain so codesign can use it (ADR 0050).
# The release workflow runs it for the project's identity (the MACOS_SELFSIGN_* secrets)
# and, on every run, for a throwaway identity from make-signing-identity.sh, so both go
# through the same code:
#
#   sh scripts/release/import-signing-identity.sh P12 PASSWORD KEYCHAIN [--github-output FILE]
#
# Prints (and appends to FILE) keychain=, sha1= (what `codesign --sign` takes) and
# sha256= (what the release gate pins). macOS only. Delete the keychain afterwards:
# `security delete-keychain KEYCHAIN`.

set -eu

[ $# -ge 3 ] || {
    sed -n '2,11p' "$0" >&2
    exit 2
}
p12="$1"
password="$2"
keychain="$3"
out=""
if [ $# -ge 5 ] && [ "$4" = "--github-output" ]; then
    out="$5"
fi
[ -f "$p12" ] || {
    printf 'error: no such file: %s\n' "$p12" >&2
    exit 1
}

kc_pass="$(openssl rand -hex 24)"
security create-keychain -p "$kc_pass" "$keychain"
security set-keychain-settings -lut 3600 "$keychain"
security unlock-keychain -p "$kc_pass" "$keychain"
security import "$p12" -k "$keychain" -P "$password" -T /usr/bin/codesign
# Let codesign use the key without a UI prompt.
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$kc_pass" "$keychain" >/dev/null
# Add it to the search list, keeping the existing keychains.
# shellcheck disable=SC2046 # one word per existing keychain path
security list-keychains -d user -s "$keychain" $(security list-keychains -d user | tr -d '"')

cert="$(mktemp)"
trap 'rm -f "$cert"' EXIT
# The password reaches openssl through the environment, never argv (visible in `ps`).
# (`security import -P` has no such option; it runs on a throwaway CI runner.)
SOMBRA_P12_PASSWORD="$password"
export SOMBRA_P12_PASSWORD
openssl pkcs12 -in "$p12" -passin env:SOMBRA_P12_PASSWORD -nokeys -clcerts |
    openssl x509 -outform PEM >"$cert"
fingerprint() {
    openssl x509 -in "$cert" -noout -fingerprint "-$1" | sed 's/.*=//' | tr -d ':' | tr 'A-F' 'a-f'
}
sha1="$(fingerprint sha1)"
sha256="$(fingerprint sha256)"

# A self-signed identity is not trusted, which is fine: codesign signs with it by hash,
# and TCC checks the designated requirement (the certificate hash), not trust.
security find-identity -p codesigning "$keychain"

lines="keychain=$keychain
sha1=$sha1
sha256=$sha256"
printf '%s\n' "$lines"
if [ -n "$out" ]; then
    printf '%s\n' "$lines" >>"$out"
fi
