# Releases

Sombra ships as GitHub Releases, built by `.github/workflows/release.yml` when a tag `vX.Y.Z` is pushed. [ADR 0044](adr/0044-release-packaging.md) decides the wheel and `uv tool install`. [ADR 0050](adr/0050-macos-app-bundle.md) adds `Sombra.app` for macOS, signed with the project's own self-signed certificate (no paid Apple account, no notarization), so the permissions belong to Sombra rather than to the terminal.

## Versioning

- [SemVer](https://semver.org/). During the MVP the major version stays `0`: `0.MINOR.PATCH`, where a minor bump may break config or CLI flags, and a patch bump only fixes things.
- **The git tag is the version.** `pyproject.toml` has no version number. hatch-vcs reads it from the tag at build time, so cutting a release needs no commit on `main`, which only takes squash-merged PRs.
- Tag format: `vX.Y.Z` for a release, and `vX.Y.Z-rcN`, `vX.Y.Z-betaN` or `vX.Y.Z-alphaN` for a pre-release. Pre-releases are published as GitHub pre-releases. `releases/latest` (what `install.sh` installs by default) only ever points at the highest final version, so a patch to an older line such as `v0.2.1` after `v0.3.0` does not become "Latest". Python normalizes the version (PEP 440): `v0.2.0-rc1` builds `sombra 0.2.0rc1`.
- **Untagged builds get a dev version**, such as `0.2.1.dev4+g1a2b3c4`: the next patch, the number of commits since the last tag, and the commit. A dirty tree adds `.dYYYYMMDD`. `sombra --version` prints it. In a checkout, `uv sync` rebuilds the editable install when HEAD or the tags move, so the number stays current. A source tree with no git metadata at all builds as `0.0.0`.

## Cutting a release

1. Make sure `main` is green and holds everything the release needs.
2. Tag the merged commit on `main` and push the tag. Only maintainers push tags (enforcing that with a tag ruleset is #54).

   ```sh
   git fetch origin
   git tag -a v0.2.0 -m "Sombra v0.2.0" origin/main
   git push origin v0.2.0
   ```

3. Watch the **Release** workflow. It runs:
   - **build**: checks that the tagged commit is on `main` and builds the sdist and wheel once. It checks that their version is the tag's, adds `install.sh`, and writes and verifies `SHA256SUMS`.
   - **smoke**: on clean `macos-14`, `ubuntu-latest`, `ubuntu-22.04` and `ubuntu-24.04-arm` runners, it verifies `SHA256SUMS` with the stock tool. It then installs the wheel as users do, with `install.sh --from-wheel` (which runs `uv tool install` on a uv-managed CPython 3.12), and downloads whisper `tiny` with `sombra models download tiny`. Finally it runs `sombra --version` (which must print the tag's version), `sombra --help`, `sombra new`, and `sombra replay` on the synthetic PT-BR e2e fixture with the fake brain and whisper `tiny`, which must log exactly one trigger, suggestion and approval.
   - **app** (`macos-14`): builds `Sombra.app` from that wheel with PyInstaller (`packaging/macos/build.py`, the dependencies pinned by `uv.lock`). It signs every nested Mach-O inside-out with the hardened runtime (`packaging/macos/sign.py`), using the project's self-signed certificate (the `MACOS_SELFSIGN_*` secrets, checked against the pin in `packaging/macos/signing-cert.sha256`). Then it zips the app with `ditto`. Without the secrets the app is ad-hoc signed instead (see below). Every run also signs a twin with a throwaway certificate made in the job, to prove the self-signed path and that its designated requirement pins the certificate.
   - **app-smoke**: on a clean `macos-14`, for the release app and the throwaway-signed twin, it quarantines the zip as a browser would and installs it with `install.sh --from-app-zip`. It checks the quarantine is gone, runs `codesign --verify --deep --strict`, and records the signature, designated requirement and certificate SHA-256 for the gate. Then, through the CLI shim, it runs `sombra --version`, `sombra doctor --json` (the "permissions holder" must be Sombra) and the same `sombra replay` as **smoke**.
   - **publish**: only when every job above passes. It runs the **macOS release gate** (`scripts/release/app_gate.py decide`). A **final** tag (`vX.Y.Z`) is **refused**, and nothing is published, unless `Sombra.app` is signed by the pinned certificate, with the hardened runtime and a designated requirement that pins that certificate. A pre-release may publish an ad-hoc app; its release notes then say so. Then it creates the GitHub Release with the assets below and the release notes.
4. If a job fails, nothing is published. Fix it on `main` through a PR, then tag the next patch or `-rcN`. Never move a published tag.

PRs that touch the release files run the same jobs without **publish**. They never use the signing secrets, so their `Sombra.app` is ad-hoc signed and only uploaded as a workflow artifact (`macos-app`). **Actions → Release → Run workflow** runs the same jobs by hand, and signs with the project's certificate when the secrets exist: that is how to test the signing setup without a tag.

## What's in a release

| Asset | What |
| --- | --- |
| `sombra-X.Y.Z-py3-none-any.whl` | the app; the same wheel on every platform (native dependencies come from PyPI at install time) |
| `sombra-X.Y.Z.tar.gz` | the sdist |
| `install.sh` | the one-line installer (see `docs/install.md`) |
| `Sombra-X.Y.Z-macos-arm64.zip` | `Sombra.app` for macOS 14+ on Apple Silicon, signed with the project's self-signed certificate (not notarized) and zipped with `ditto`. The version is the PEP 440 one (`v0.2.0-rc1` → `Sombra-0.2.0rc1-macos-arm64.zip`), and it matches the app's `CFBundleShortVersionString`. A pre-release's app may be ad-hoc signed, which its notes then say. The Homebrew cask (#51) installs this file. |
| `SHA256SUMS` | SHA-256 of every other asset, in `sha256sum` format |

The release notes list the titles of the PRs merged since the previous release. A final release (`v0.3.0`) counts from the previous final release, and includes everything its release candidates had. A pre-release counts from the tag just before it. `scripts/release/notes.py` generates them from the first-parent history of `main`, where each squash-merge commit is one PR.

Supported platforms: macOS 14+ on Apple Silicon, and Ubuntu 22.04/24.04 on x86_64 and aarch64. Intel Macs are not supported ([ADR 0044](adr/0044-release-packaging.md)). Do not `pip install sombra` or `uv tool install sombra` from PyPI: that name belongs to an unrelated project.

## Verifying a download

Download `SHA256SUMS` into the same folder as the assets, then run:

```sh
sha256sum -c --ignore-missing SHA256SUMS        # Ubuntu
shasum -a 256 -c --ignore-missing SHA256SUMS    # macOS
```

Every file you downloaded must print `OK`. `--ignore-missing` skips the assets you didn't download. `install.sh` does the same check before it installs a release. `SHA256SUMS` proves the files match what the workflow built, not who built them. The wheel and sdist are not signed. `Sombra.app` is, with the project's own certificate. Check it with `codesign --verify --deep --strict --verbose=2 Sombra.app`, and compare its certificate with the pinned one:

```sh
codesign -d --extract-certificates=/tmp/sombra-cert Sombra.app
shasum -a 256 /tmp/sombra-cert0     # must equal packaging/macos/signing-cert.sha256
codesign -d -r- Sombra.app          # designated => identifier "io.github.nickmaglowsch.Sombra" and certificate root = H"..."
```

`spctl --assess` rejects it: it is not notarized ([ADR 0050](adr/0050-macos-app-bundle.md#gatekeeper-without-notarization)).

## Installing a release by hand

Without the installer, with [uv](https://docs.astral.sh/uv/) installed:

```sh
uv tool install --python 3.12 \
  https://github.com/nickmaglowsch/sombra-call/releases/download/v0.2.0/sombra-0.2.0-py3-none-any.whl
sombra --version
```

- **Upgrade:** run the same command with the new URL, adding `--force`.
- **Uninstall:** `uv tool uninstall sombra`. Your config (`~/.config/sombra`), models (`~/.cache/sombra/models`) and meetings (`~/Sombra/meetings`) stay.

On macOS this path grants Microphone, Screen Recording and Accessibility to the terminal app you run `sombra` from ([ADR 0044](adr/0044-release-packaging.md#evidence-macos-tcc)). For grants that belong to Sombra, install `Sombra.app` instead: `install.sh` does by default. By hand, unzip `Sombra-X.Y.Z-macos-arm64.zip` with `ditto -x -k`, move `Sombra.app` to `/Applications`, and run `xattr -dr com.apple.quarantine /Applications/Sombra.app` if you downloaded it with a browser. Then link `/Applications/Sombra.app/Contents/Helpers/sombra` onto your `PATH` (ADR 0050).

## Signing Sombra.app (the free path)

There is no paid Apple Developer account, so no Developer ID and no notarization ([ADR 0050](adr/0050-macos-app-bundle.md)). Every release is signed with **one self-signed certificate that never changes**. macOS keys the users' permission grants on it (the app's designated requirement pins it), so the grants survive upgrades. Two free repository secrets hold it:

| Secret | What |
| --- | --- |
| `MACOS_SELFSIGN_P12_BASE64` | the certificate **and its private key**, as a `.p12`, base64-encoded |
| `MACOS_SELFSIGN_P12_PASSWORD` | the `.p12`'s password |

Without them, every build is ad-hoc signed. It works, but macOS asks for the permissions again after every upgrade, and the release gate refuses every final tag.

One-time setup, step by step:

1. **Create the identity**, on a machine you trust (macOS or Linux, with `openssl`):

   ```sh
   sh scripts/release/make-signing-identity.sh --out ~/sombra-signing
   ```

   It creates an RSA-3072 code-signing certificate (`CN=Sombra Code Signing`, valid 20 years) and prints the two secret values and the certificate's SHA-256. It refuses to overwrite an existing identity.
2. **Add the two secrets:** Settings → Secrets and variables → Actions → New repository secret. Paste the printed `MACOS_SELFSIGN_P12_PASSWORD` and `MACOS_SELFSIGN_P12_BASE64` values.
3. **Pin the certificate:** put the printed SHA-256 (64 hex characters) on its own line in `packaging/macos/signing-cert.sha256` and merge it through a PR. The release gate refuses final releases signed by anything else, and the **app** job refuses to sign with a secret that doesn't match the pin.
4. **Back up** `~/sombra-signing/sombra-signing.p12` and its password offline (a password manager), then delete the folder. **Never generate a second identity:** a new certificate makes every user grant every permission again. If the key ever leaks, treat it like a leaked password: make a new identity, pin it, and say in the release notes that users must grant the permissions again.
5. **Test it without a release:** Actions → Release → Run workflow (branch `main`). The **app** job's summary must say "the project's self-signed certificate", and **app-smoke (release)** must print `release-ready: signed by the pinned certificate`.
6. **Then push an rc tag** (`v0.2.0-rc1`) and check that the pre-release lists `Sombra-0.2.0rc1-macos-arm64.zip` in `SHA256SUMS`, with no "ad-hoc" note in its release notes.

## Release helpers

`scripts/release/` holds small, standard-library-only scripts, tested in `tests/release/`:

- `tags.py check <tag> [--dist dist]`: validates the tag, checks the wheel and sdist versions, and prints `version=` / `prerelease=`.
- `checksums.py write|verify <dir>`: writes and verifies `SHA256SUMS`.
- `notes.py <tag> [--repo owner/name] [--output file]`: writes the release notes.
- `app_gate.py record|decide|ready`: the macOS release gate (ADR 0050). `record` inspects an installed `Sombra.app` on macOS. `decide` says whether a tag may publish it: it exits 1 for a final tag whose app isn't signed by the pinned certificate, and otherwise prints `attach=` and `app_signature=pinned|adhoc`.
- `make-signing-identity.sh`: creates the self-signed identity (once, by the owner; CI makes throwaway ones with it).
- `import-signing-identity.sh`: loads a `.p12` into a temporary keychain for `codesign` (macOS, CI).

`packaging/macos/` builds the app: `build.py` (PyInstaller, `sombra.spec`), `sign.py` (inside-out signing), `entitlements.plist`, `sombra_app.py` (bundle id, `Info.plist`, PT-BR/EN usage strings) and `shim/sombra.c` (the CLI shim). Tests are in `tests/release/`.
