#!/bin/sh
# Create Sombra's self-signed code-signing identity (ADR 0050). The owner runs it ONCE:
#
#   sh scripts/release/make-signing-identity.sh [--out DIR] [--name NAME] [--days N]
#
# It writes, into DIR (default: a new private temp folder):
#   sombra-signing.p12   certificate + private key, password-protected (-> the secrets)
#   sombra-signing.cer   the certificate alone, DER
# and prints the two repository secrets (MACOS_SELFSIGN_P12_BASE64, MACOS_SELFSIGN_P12_PASSWORD)
# and the certificate's SHA-256, which goes in packaging/macos/signing-cert.sha256.
#
# The certificate must never change: macOS keys the users' permission grants on it (the
# app's designated requirement pins it). So it is valid for 20 years by default, and
# losing the .p12 means every user grants the permissions again. Keep a backup offline.
#
# --github-output FILE (CI, throwaway identities) appends p12=, cer=, password=, sha256=
# and sha1= lines to FILE instead of printing the secrets. Needs openssl (LibreSSL on
# macOS works).

set -eu

OUT=""
NAME="Sombra Code Signing"
DAYS=7300
GITHUB_OUTPUT_FILE=""

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --out)
            [ $# -ge 2 ] || die "--out needs a directory"
            OUT="$2"
            shift 2
            ;;
        --name)
            [ $# -ge 2 ] || die "--name needs a value"
            NAME="$2"
            shift 2
            ;;
        --days)
            [ $# -ge 2 ] || die "--days needs a number"
            DAYS="$2"
            shift 2
            ;;
        --github-output)
            [ $# -ge 2 ] || die "--github-output needs a file"
            GITHUB_OUTPUT_FILE="$2"
            shift 2
            ;;
        -h | --help)
            sed -n '2,20p' "$0"
            exit 0
            ;;
        *) die "unknown argument: $1" ;;
    esac
done
case "$DAYS" in
    '' | *[!0-9]*) die "--days must be a number of days" ;;
esac
command -v openssl >/dev/null 2>&1 || die "openssl is needed"

umask 077
if [ -z "$OUT" ]; then
    OUT="$(mktemp -d)"
fi
mkdir -p "$OUT"
p12="$OUT/sombra-signing.p12"
cer="$OUT/sombra-signing.cer"
[ ! -e "$p12" ] || die "$p12 exists; refusing to overwrite a signing identity"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# A leaf certificate for code signing only: the codeSigning extended key usage is what
# codesign and the designated requirement look for.
cat >"$work/openssl.cnf" <<EOF
[req]
distinguished_name = dn
prompt = no
x509_extensions = ext
[dn]
CN = $NAME
O = Sombra
[ext]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
subjectKeyIdentifier = hash
EOF

openssl req -x509 -newkey rsa:3072 -sha256 -days "$DAYS" -nodes \
    -keyout "$work/key.pem" -out "$work/cert.pem" -config "$work/openssl.cnf" 2>/dev/null
openssl x509 -in "$work/cert.pem" -outform DER -out "$cer"

password="$(openssl rand -hex 24)"
# The password reaches openssl through the environment, never argv (visible in `ps`).
SOMBRA_P12_PASSWORD="$password"
export SOMBRA_P12_PASSWORD
# SHA1-3DES keeps the .p12 importable by macOS `security import` (it rejects the newer
# PBES2 defaults of OpenSSL 3 on some releases).
openssl pkcs12 -export -inkey "$work/key.pem" -in "$work/cert.pem" -name "$NAME" \
    -out "$p12" -passout env:SOMBRA_P12_PASSWORD \
    -keypbe PBE-SHA1-3DES -certpbe PBE-SHA1-3DES -macalg sha1

fingerprint() {
    openssl x509 -in "$work/cert.pem" -noout -fingerprint "-$1" |
        sed 's/.*=//' | tr -d ':' | tr 'A-F' 'a-f'
}
sha256="$(fingerprint sha256)"
sha1="$(fingerprint sha1)"

if [ -n "$GITHUB_OUTPUT_FILE" ]; then
    printf '::add-mask::%s\n' "$password"
    {
        printf 'p12=%s\n' "$p12"
        printf 'cer=%s\n' "$cer"
        printf 'password=%s\n' "$password"
        printf 'sha256=%s\n' "$sha256"
        printf 'sha1=%s\n' "$sha1"
    } >>"$GITHUB_OUTPUT_FILE"
    printf 'throwaway identity "%s": sha256 %s\n' "$NAME" "$sha256"
    exit 0
fi

cat <<EOF
Created the Sombra code-signing identity "$NAME" (valid $DAYS days):

  $p12
  $cer

1. Add two repository secrets (Settings > Secrets and variables > Actions):

   MACOS_SELFSIGN_P12_PASSWORD
$password

   MACOS_SELFSIGN_P12_BASE64
$(openssl base64 -A -in "$p12")

2. Pin the certificate: put this line in packaging/macos/signing-cert.sha256 and merge it
   through a PR (the release gate refuses final releases signed by anything else):

$sha256

3. Back up $p12 and its password offline (a password manager), then delete $OUT.
   If they are lost, a new identity makes every user grant the permissions again.
EOF
