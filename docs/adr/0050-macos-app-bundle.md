# ADR 0050: A self-signed `Sombra.app`, built with PyInstaller, owns the macOS permissions

- Status: **provisional**. CI on `macos-14` builds, signs and exercises the bundle, the CLI shim, the ad-hoc and self-signed signatures, the designated requirement and the release gate (see [Evidence](#evidence-from-ci)). What needs the owner's certificate or a person at a real Mac (the System Settings entries, prompts, grants surviving an upgrade, a live meeting) waits for a human run. See [Left for a human](#left-for-a-human).
- Date: 2026-09-30
- Issue: #50 (epic #43; follow-up from ADR 0044). The Homebrew cask (#51) consumes the release asset defined here.
- Numbering: this ADR uses the issue number, as ADRs 0009, 0018, 0044 and 0046 do.
- Owner decision (2026-09-29): **no paid Apple Developer Program**, so no Developer ID and no notarization. This ADR is the free path.

## Context

Releases ship a wheel installed with `uv tool install` (ADR 0044). macOS then attributes Microphone, Screen Recording and Accessibility to the **responsible process**. For a command-line tool that is the terminal app it runs in, so the grant covers every program run from that terminal. A `.app` with a stable bundle id and a **stable code-signing identity** gets grants that belong to Sombra alone and survive upgrades.

Four things have to hold together:

1. a bundler that packs Sombra's native dependencies into a `.app`: onnxruntime, pywhispercpp (whisper.cpp), PortAudio through sounddevice, and the pyobjc frameworks (Cocoa, CoreAudio, Quartz, ScreenCaptureKit);
2. a way for `sombra` typed in a terminal to run **as Sombra** in TCC's eyes, not as the terminal;
3. a signature that stays the same from release to release, **without** a paid Developer ID;
4. a way past Gatekeeper for an app that is not notarized, and a gate that stops a final release from shipping with the wrong signature.

## How TCC decides who asks, and what it remembers

`tccd` does not look at the process that calls `AVCaptureDevice` or `CGPreflightScreenCaptureAccess`. It looks at that process's *responsible process*, which is inherited across `fork`/`exec`. A GUI app started by LaunchServices is responsible for itself. A shell's children, and everything they exec, stay the responsibility of the terminal app. So the uv install's grants go to Terminal, and running `Sombra.app/Contents/MacOS/Sombra` straight from a shell would change nothing. CI shows exactly that ([Evidence](#evidence-from-ci)).

The spawn attribute `responsibility_spawnattrs_setdisclaim(attr, 1)` makes a `posix_spawn`ed child responsible for itself. It is private SPI in libSystem. LLDB uses it to launch debuggees, Chromium uses it for its helpers, and Qt Creator uses it for the programs it runs, all for this exact TCC reason. The matching query, `responsibility_get_pid_responsible_for_pid`, is what `sombra doctor` calls to report who holds the grants.

When the user grants a permission, TCC stores the client's bundle id **and its designated requirement** (DR), the code requirement `codesign -d -r-` prints. Later requests must satisfy that stored requirement. The DR is what decides whether a grant survives an upgrade:

| Signed with | Designated requirement (`codesign -d -r-`) | After an upgrade |
| --- | --- | --- |
| Developer ID (paid; not used) | `anchor apple generic and identifier "…" and certificate leaf[subject.OU] = TEAM` | kept |
| **Our self-signed certificate** | `identifier "io.github.nickmaglowsch.Sombra" and certificate root = H"<SHA-1 of our certificate>"` (self-signed: the leaf is the root) | **kept**: every release is signed by the same certificate, so the new app satisfies the stored requirement |
| Ad-hoc (`codesign -s -`) | `cdhash H"<hash of this exact build>"` | **reset**: the new build has another cdhash, so macOS asks again |

A non-Apple certificate gets a `certificate … = H"…"` clause. Checking it is a hash comparison, not a trust evaluation, so the certificate doesn't need to chain to Apple or be trusted on the user's Mac. CI signs a twin of the app with a throwaway certificate and checks both halves of this table: the DR pins that certificate, and a rebuilt app still satisfies the old requirement; an ad-hoc rebuild does not ([Evidence](#evidence-from-ci)).

Two limits apply however the app is signed:

- **ScreenCaptureKit re-consent.** macOS 15 (Sequoia) periodically asks the user to confirm that an app may keep recording the screen. Sombra's grant is scoped to Sombra, but the user will still see that reminder now and then.
- **Resetting grants.** `tccutil reset All io.github.nickmaglowsch.Sombra` removes Sombra's grants (the uninstaller prints the command).

How to see TCC's view on a Mac, for the human check below:

```sh
# Who does TCC attribute a request to? Watch while running `sombra doctor` or a meeting:
log stream --debug --predicate 'subsystem == "com.apple.TCC"' | grep -i -E 'AttributionChain|sombra'
#   ... responsible={TCCDProcess: identifier=io.github.nickmaglowsch.Sombra, ... binary_path=/Applications/Sombra.app/Contents/MacOS/Sombra}
codesign -d -r- /Applications/Sombra.app    # the requirement TCC stores
```

## Options: the bundler

| | PyInstaller 6.22 (+ hooks-contrib 2026.7) | Briefcase | py2app |
| --- | --- | --- | --- |
| Input | the same locked wheels `uv.lock` pins, installed into a clean venv on a uv-managed CPython 3.12 | its own app template and "support package" Python, deps installed by pip from `pyproject` | a setuptools build of the source tree |
| onnxruntime | hook in hooks-contrib (collects its dylibs) | pip wheel copied as is | no hook; manual `includes`/`frameworks` |
| sounddevice / PortAudio | hook in hooks-contrib (collects `_sounddevice_data/portaudio-binaries/libportaudio.dylib`) | wheel copied as is | manual |
| pyobjc frameworks | hooks for `objc`, `Foundation`, `AppKit`, `Quartz`, `CoreAudio`, `ScreenCaptureKit`, … | wheel copied as is | first-class (same authors as pyobjc) |
| pywhispercpp | no hook needed: binary analysis follows the extension's `@rpath` links (`libwhisper`, `libggml*`); the spec also collects its libs and data | wheel copied as is | manual |
| Signing | we sign ourselves, inside-out (below) | `briefcase package` signs its own way, built around Developer ID | we sign ourselves |
| Fits our CLI | `console=True` onedir `.app`, no Dock icon | built around a GUI app; a CLI needs workarounds | GUI-first |
| License | GPL-2.0 with the bootloader exception: bundling proprietary apps is allowed | BSD-3 | MIT |

**Chosen: PyInstaller, onedir, wrapped in a `.app` by its `BUNDLE` step.** It is the only one we built. The decision rests on that build plus the table:

- It works with every native dependency: CI runs `sombra replay` from the bundle. That run uses onnxruntime (Silero VAD), whisper.cpp with Metal and numpy on the e2e fixture, and `doctor` loads pyobjc, CoreAudio, Quartz, AVFoundation and PortAudio (`sounddevice.query_devices`).
- It starts from the wheels the lock pins, so the app and the wheel run the same code and dependency versions.
- It lets us control signing, which the release gate depends on.

Briefcase would probably work too (it copies wheels as is), but it brings its own Python build and template, and its signing pipeline assumes a Developer ID. py2app knows pyobjc best but needs manual work for onnxruntime and PortAudio. Neither was built, so neither is "proven not to work"; they lost on fit.

`onefile` is out: it unpacks to a temporary folder on every start, which breaks the code signature and the TCC identity.

## Decision

### The bundle

- **Bundle id `io.github.nickmaglowsch.Sombra`, forever.** Reverse DNS of a domain the project controls (its GitHub Pages namespace). Changing it later would make every user grant again, so it lives in one place (`packaging/macos/sombra_app.py`), and a test checks that `install.sh` and `sombra doctor` agree with it.
- **Layout.** `Contents/MacOS/Sombra` is PyInstaller's bootloader. It runs Python in-process, so its pid is the one TCC sees. Python, extension modules and dylibs sit in `Contents/Frameworks`, data (prompts, UI files, `sombra`'s dist-info for `--version`) in `Contents/Resources`, and the CLI shim in `Contents/Helpers/sombra` (Apple's place for helper tools; `Contents/MacOS/sombra` would clash with `Sombra` on a case-insensitive disk).
- **`Info.plist`.** `CFBundleShortVersionString` = `CFBundleVersion` = the release version exactly as `sombra --version` prints it (PEP 440: `0.2.0`, `0.2.0rc1`); #51 relies on it. `LSMinimumSystemVersion` 14.0, `LSArchitecturePriority` arm64, and `LSUIElement` true (a command-line app gets no Dock icon).
- **Usage strings, EN and PT-BR.** `NSMicrophoneUsageDescription` (microphone), `NSAudioCaptureUsageDescription` (the Core Audio process tap, "System Audio Recording", macOS 14.4+) and `NSScreenCaptureUsageDescription`. English is in `Info.plist`, and each language has a `<lang>.lproj/InfoPlist.strings` (`en`, `pt-BR`). Apple documents no usage key for Screen Recording on macOS 14; its prompt text is fixed. We ship the key anyway because macOS ignores keys it doesn't read. Accessibility has no usage key.
- **Excluded:** the optional `pywebview` window (the `window` extra) and tkinter.

### The CLI shim

`Contents/Helpers/sombra` is a small C program (`packaging/macos/shim/sombra.c`), built with `clang -arch arm64 -mmacosx-version-min=14.0`. `install.sh` links it onto `PATH` as `~/.local/bin/sombra`, and the Homebrew cask's `binary` stanza (#51) points at the same path. When run, the shim:

1. resolves its real path through the symlink (`_NSGetExecutablePath` + `realpath`) and finds `../MacOS/Sombra`;
2. `posix_spawn`s it with `responsibility_spawnattrs_setdisclaim(attr, 1)`, looked up with `dlsym`, so a macOS that drops the SPI still runs Sombra, just with the terminal's grants, which `doctor` reports;
3. passes argv, the environment and stdin/stdout/stderr through unchanged, resets signal handlers and the mask for the child, forwards `SIGTERM`/`SIGHUP`/`SIGUSR1`/`SIGUSR2` (Ctrl-C already reaches the child through the process group), and exits with the child's status, re-raising the signal that killed it.

A shell script can't set spawn attributes, and `open -a Sombra` (LaunchServices) would make Sombra responsible but loses the terminal's stdio, the exit status and the environment. A tiny native launcher is the standard answer. Models and config stay where the uv install keeps them (`~/.cache/sombra/models`, `~/.config/sombra`), because the app runs the same code with the same `HOME`.

### Signing: one self-signed certificate, forever

- **The identity.** `scripts/release/make-signing-identity.sh` creates it once with `openssl`: an RSA-3072 leaf certificate, `CN=Sombra Code Signing`, `extendedKeyUsage=codeSigning`, `CA:false`, valid 20 years. It writes a password-protected `.p12` (SHA1-3DES, which `security import` accepts) and prints two free repository secrets, `MACOS_SELFSIGN_P12_BASE64` and `MACOS_SELFSIGN_P12_PASSWORD`, plus the certificate's SHA-256.
- **The pin.** That SHA-256 is committed in `packaging/macos/signing-cert.sha256`. The release gate compares the signing certificate against it, and the **app** job refuses to sign with a secret that isn't the pinned certificate.
- **Why 20 years.** A new certificate means a new DR, which means every user grants every permission again. So the certificate must never need replacing, and a 20-year validity means expiry never forces a rotation. The `.p12` must be backed up offline; losing it has the same cost as rotating it.
- **Importing.** CI imports the `.p12` into a temporary keychain with `scripts/release/import-signing-identity.sh` and signs by the certificate's SHA-1. The certificate is not trusted on the runner, and doesn't need to be.
- **Signing order.** `packaging/macos/sign.py` signs inside-out:
  1. every Mach-O in the bundle, deepest first, batched per `codesign` call. The main executable and the shim are skipped here, and symlinks are skipped because their targets are signed where they live;
  2. any nested `.framework`/`.bundle`;
  3. the shim, with its own identifier `io.github.nickmaglowsch.Sombra.cli`;
  4. the bundle, with `entitlements.plist`. This signs the main executable and seals the resources.

  It uses `--timestamp=none` (Apple's timestamp service is for Developer ID code) and ends with `codesign --verify --deep --strict`. We don't use `codesign --deep` to sign, because Apple discourages it: it applies one set of options to everything.
- **Without secrets** (PRs, forks): the same script with `--identity -` (ad-hoc). The app works, but its DR is its cdhash, so TCC grants reset on every upgrade. `sombra doctor` says so ("app signature"), and so do `install.sh` and the release notes of a pre-release that ships one.

### Hardened runtime and entitlements

Notarization would require the hardened runtime; we don't notarize, so it is a choice. We keep it, because Sombra holds Microphone and Screen Recording grants. Without the hardened runtime, any local process could inject a dylib into Sombra with `DYLD_INSERT_LIBRARIES`, or attach a debugger, and use those grants: a known class of TCC bypass. CI shows it costs nothing. The ad-hoc and the self-signed builds, both with the hardened runtime, run `replay` (onnxruntime, whisper.cpp with Metal) and `doctor`.

A self-signed certificate has no Team ID, and neither does an ad-hoc signature. Library validation then accepts our own dylibs, which carry the same (empty) team; CI confirms the bundle loads.

The app carries **one** entitlement:

| Entitlement | Why |
| --- | --- |
| `com.apple.security.device.audio-input` | Microphone (EU track). Under the hardened runtime, audio input is refused without it, even when the user granted Microphone. The Core Audio process tap goes through the same audio stack. |

Considered and left out, since nothing we run needs them. The rule: add one only with evidence that the app fails without it.

- `com.apple.security.cs.disable-library-validation`: every dylib and extension module in the bundle is signed by us, as above.
- `com.apple.security.cs.allow-unsigned-executable-memory` / `allow-jit`: onnxruntime, whisper.cpp (Metal shaders compile in a system service, not in our process) and numpy generate no code. The libffi closures used by ctypes, cffi (sounddevice's stream callback) and pyobjc use trampoline tables on arm64 macOS, not writable+executable pages.
- `com.apple.security.cs.allow-dyld-environment-variables`: the bootloader finds its libraries with `@rpath`/`@loader_path`, not `DYLD_*`.
- `com.apple.security.automation.apple-events`: notifications go through `osascript display notification`, a separate process, and we send no Apple Events.
- Screen Recording and Accessibility have no entitlement; they are TCC grants only.

The live-capture callbacks (PortAudio through cffi, the process tap, ScreenCaptureKit) need permissions a CI runner doesn't have. That is why a live meeting on a real Mac is in [Left for a human](#left-for-a-human) before the first final release.

### Gatekeeper without notarization

Gatekeeper only assesses an app that carries the `com.apple.quarantine` extended attribute. A self-signed, unnotarized app with that attribute is blocked. Without it, it runs.

- `install.sh` downloads with `curl`, which sets no quarantine. After copying the app, it also runs `xattr -dr com.apple.quarantine` on it, because a zip the user downloaded with a browser *is* quarantined, and `ditto` carries the attribute into the app. The app-smoke job quarantines the zip on purpose, installs it, and checks that no file in the installed app keeps the attribute.
- The Homebrew cask (#51) strips quarantine in `postflight`.
- By hand, after downloading the zip in a browser and moving `Sombra.app` to Applications:
  - either run `xattr -dr com.apple.quarantine /Applications/Sombra.app`;
  - or on macOS 14, right-click the app → **Open** → **Open**. On macOS 15+ that shortcut is gone: open it once, then use System Settings → Privacy & Security → **Open Anyway**.
- The trade-off: users trust the download because of `SHA256SUMS` over HTTPS from the GitHub release, not because Apple vouched for it. We can't do better than that without a Developer ID.

### Release pipeline (`.github/workflows/release.yml`)

- **app** (`macos-14`):
  - builds `Sombra.app` from the wheel the `build` job made (`packaging/macos/build.py`: `uv export --frozen --group app` → clean venv → PyInstaller → localisations → shim) and checks the `Info.plist` version;
  - on tags and manual runs with the secrets: imports the project's identity, checks it against the pin, and signs;
  - otherwise signs ad-hoc;
  - zips with `ditto -c -k --sequesterRsrc --keepParent`, reports sizes in the job summary and uploads `macos-app`.
  - On **every** run it also:
    - makes a throwaway identity with `make-signing-identity.sh` and signs an unsigned twin of the same build with it;
    - checks that `codesign -d -r-` pins that certificate (identifier and `certificate root = H"<sha1>"`, no cdhash);
    - shows that a rebuilt twin re-signed with the same certificate satisfies the old requirement (`codesign --verify -R`), while an ad-hoc rebuild fails its old one;
    - uploads the twin as `macos-app-selfsigned-test`.
- **app-smoke** runs on a clean `macos-14`, for both the `release` and the `selfsigned-test` app:
  - quarantines the zip, installs it with `install.sh --from-app-zip`, and checks the quarantine is gone;
  - runs `codesign --verify --deep --strict` and records the gate evidence (`app_gate.py record`: `codesign -dv`, `-d -r-`, the leaf certificate's SHA-256, the strict verify, the zip's SHA-256, the bundle id and version);
  - through the shim, runs `sombra --version` (must print the tag's version), `sombra doctor --json` (the "permissions holder" must be Sombra; for the self-signed twin, the "app signature" must be its certificate) and `sombra replay` on the e2e fixture with the fake brain and whisper tiny (exactly one trigger, suggestion and action);
  - as a control, runs the bootloader directly, without the shim, which must **not** report Sombra as the holder;
  - for the twin, runs the gate pinned to its throwaway certificate (must pass) and pinned to another one (must fail).
- **publish** needs both, then runs `app_gate.py decide`:
  - A **final tag is refused**, and nothing is published, unless the app is signed by the pinned certificate, has a DR pinning it, the hardened runtime and a clean strict verify.
  - A pre-release may publish an ad-hoc app: the notes say it is ad-hoc and that grants reset on upgrades.
  - A pre-release signed by any other certificate is refused, since that means the secrets are wrong.
  - Any tag is refused when the name, SHA-256, version or bundle id of the zip being published doesn't match the one that was verified.
  - When the app is attached, the zip is added to the assets and `SHA256SUMS` is rewritten.

### Release asset interface (for #51)

- `Sombra-<version>-macos-arm64.zip`: a `ditto` zip of `Sombra.app`, listed in `SHA256SUMS`. `<version>` is the PEP 440 version of the tag (`v0.2.0` → `0.2.0`, `v0.2.0-rc1` → `0.2.0rc1`), the same as the wheel's.
- Inside, `Sombra.app/Contents/Info.plist` has `CFBundleShortVersionString` = `<version>`, and the CLI shim is `Sombra.app/Contents/Helpers/sombra`.
- A final release always has it, signed by the pinned certificate. A pre-release has it too, possibly ad-hoc (labelled in the notes).

### Installer and `doctor`

- **`install.sh`** on macOS:
  - downloads the zip listed in `SHA256SUMS` (checksum verified) and unpacks it with `ditto`;
  - checks the bundle id and `codesign --verify --deep --strict`, refusing on failure, and warns when the app is ad-hoc signed;
  - installs into `/Applications`, or `~/Applications` when `/Applications` isn't writable, upgrading an existing Sombra in place and never replacing another app;
  - strips quarantine, links the shim to `~/.local/bin/sombra`, and removes an older uv-tool install.

  A release with no app falls back to the uv install, and `--no-app` forces it. `--uninstall` removes the app and the link and prints `tccutil reset All io.github.nickmaglowsch.Sombra` for the grants (macOS keeps them).
- **`sombra doctor`**:
  - **permissions holder** asks libSystem for the responsible process (`responsibility_get_pid_responsible_for_pid` + `proc_pidpath`) and names its app (outermost `.app`, with `CFBundleName` and bundle id), for example `Sombra (io.github.nickmaglowsch.Sombra) at /Applications/Sombra.app`. It is ok for Sombra, and ok with a note suggesting Sombra.app for a terminal. It warns when sombra runs from Sombra.app but something else is responsible (the shim was bypassed), and when the process can't be read.
  - **app signature** (only when running from Sombra.app) is ok for a certificate ("the grants survive upgrades"). It warns for ad-hoc ("macOS forgets the permission grants on every upgrade"), or when the signature can't be read.
  - The permission checks name the holder app in their fixes.

## Evidence from CI

All numbers come from PR #62's Release runs on `macos-14` (macOS 14.8.9, arm64): [36648302872](https://github.com/nickmaglowsch/sombra-call/actions/runs/36648302872) (first ad-hoc build) and [36650828166](https://github.com/nickmaglowsch/sombra-call/actions/runs/36650828166) (self-signed free path).

**Size and build.** `Sombra.app` is 137 MB on disk and holds 205 files, 84 of them Mach-O that `sign.py` signs. The zip (`ditto`) is 50 MB. Building takes about 20 s (PyInstaller) and the whole **app** job about 1 minute.

**Native dependencies under the hardened runtime.** The ad-hoc app, installed through `install.sh` and run through the shim, passes:

- `sombra --version`;
- `sombra doctor --json`: pyobjc, AVFoundation, Quartz, ApplicationServices and PortAudio are all probed, and it reports "default input: Apple Virtual Sound Device" and "Core Audio process tap";
- `sombra replay` on the e2e fixture: onnxruntime (Silero VAD) and whisper.cpp tiny with Metal (`ggml_metal_free: deallocating`) produce `['trigger', 'suggestion', 'action']`.

**The shim makes Sombra the responsible process.** Through `~/.local/bin/sombra`:

```
permissions holder: ok  Sombra (io.github.nickmaglowsch.Sombra) at /Applications/Sombra.app: the grants belong to Sombra alone
microphone: warn        not asked yet; macOS prompts Sombra the first time; ...
```

The control, `Sombra.app/Contents/MacOS/Sombra doctor` run directly, reports the runner's own process:

```
permissions holder: warn  hosted-compute-agent at /opt/hca/hosted-compute-agent, although sombra runs from /Applications/Sombra.app
```

**Designated requirements** (`codesign -d -r-`):

```
# ad-hoc (PR build, no secrets)
# designated => cdhash H"6a73f53d4bfb859eef386e7555c5e22be21492df"

# the same build signed with the job's throwaway self-signed certificate
Authority=Sombra CI Throwaway
TeamIdentifier=not set
CodeDirectory v=20500 size=94687 flags=0x10000(runtime) hashes=2948+7 location=embedded
designated => identifier "io.github.nickmaglowsch.Sombra" and certificate root = H"bcf07bb57ec61e37ce1719504e4e03783c02c9dd"
```

`security find-identity` listed that certificate as `CSSMERR_TP_NOT_TRUSTED` ("0 valid identities found"). `codesign` signed with it by hash anyway, and `codesign --verify --deep --strict` passed: trust is not needed.

**Grants across an upgrade** (TCC re-checks the stored requirement against the new app). CI changed `CFBundleVersion` in a copy and re-signed it:

```
codesign --verify -R='identifier "io.github.nickmaglowsch.Sombra" and certificate root = H"bcf0…"' next/self/Sombra.app
  -> satisfies it                                   self-signed: grants persist
codesign --verify -R='cdhash H"6a73…"' next/adhoc/Sombra.app
  -> test-requirement: code failed to satisfy specified code requirement(s)   ad-hoc: grants reset
```

**Gatekeeper.** app-smoke writes `com.apple.quarantine` on the zip (as Safari would) before `install.sh`. Afterwards no file in `/Applications/Sombra.app` carries it.

**Release gate.** The first run's ad-hoc app was recorded and rejected for a final release, although under the Developer ID rules this ADR replaced. Its self-signed-era equivalent is `not release-ready: signature is adhoc, …`. In run 36650828166, app-smoke (selfsigned-test) passed both of its gate checks: `app_gate.py ready` pinned to the twin's own certificate passed, and pinned to another certificate it failed. The same job also passed `doctor`'s "app signature" check (`signed with the certificate 'Sombra CI Throwaway': the grants survive upgrades`) and `replay`. The unit tests in `tests/release/test_app_gate.py` cover the decision itself (ad-hoc or wrong certificate on a final tag → refused).

## Consequences

- macOS users get two install paths: the app (default) and the uv tool (`--no-app`, and the fallback). Both run the same code from the same wheel. Only the app keeps grants scoped to Sombra.
- No money is spent, and Apple doesn't vouch for the app. Installing depends on the quarantine being absent (curl, `install.sh`, the cask) or removed by the user. Users who download the zip in a browser need one extra step, which the docs spell out.
- The signing certificate is now a long-lived secret with a real cost if lost or leaked. If it is lost, users re-grant. If it leaks, someone could sign code that satisfies Sombra's requirement and inherit its grants on machines where it is installed. It is kept only in the two repo secrets and an offline backup. Rotating it, if ever needed, is a new pin plus a release note that tells users to grant again.
- The disclaim SPI is private. If Apple removes it, the shim still runs Sombra, the grants fall back to the terminal, and `doctor` says so. It would not silently break.
- The app is large: a full CPython, numpy, onnxruntime, whisper.cpp and pyobjc. That is the price of self-contained. Models are not in the bundle; they download to `~/.cache/sombra/models` as before.
- PyInstaller joins the lock as a non-default dependency group (`app`); `uv sync` and `make check` don't install it.
- A Developer ID can be added later without changing the bundle id. Users would re-grant once, because the requirement would change from our certificate to Apple's anchor.

## Left for a human

1. Run `sh scripts/release/make-signing-identity.sh` once on a trusted machine. Add the two secrets, and put the printed SHA-256 in `packaging/macos/signing-cert.sha256` through a PR. Back up the `.p12` and its password offline. Steps: [docs/release.md](../release.md#signing-sombraapp-the-free-path).
2. Run **Actions → Release → Run workflow** on `main`. The **app** job must sign with the project's certificate (summary: "the project's self-signed certificate"), and **app-smoke (release)** must pass `app_gate.py ready`. Then push an rc tag (`v0.2.0-rc1`) and check that the pre-release lists `Sombra-0.2.0rc1-macos-arm64.zip` in `SHA256SUMS`, without an ad-hoc label.
3. On a real Apple Silicon Mac with macOS 14+:
   - install with `curl -fsSL …/install.sh | sh -s -- --version v0.2.0-rc1`, and run `sombra doctor` (permissions holder: Sombra; app signature: the certificate);
   - run `sombra start` for a short test meeting with a second device on the call, and accept the Microphone, System Audio Recording, Screen Recording and Accessibility prompts. **They must name Sombra**, and System Settings > Privacy & Security must list **Sombra**, not Terminal;
   - keep the `log stream` from [above](#how-tcc-decides-who-asks-and-what-it-remembers) running and save the `AttributionChain` lines;
   - confirm the transcript has both EU and OUTROS and that screenshots were taken. This is the hardened-runtime check for the live-capture callbacks.
4. Install the next rc over it and check that no prompt reappears (except the ScreenCaptureKit periodic reminder), and that System Settings still lists Sombra as allowed.
5. Also try the browser path once: download the zip in Safari, unzip it, move it to Applications, and check that the documented `xattr` or Open Anyway step works.
6. Report the results on #50 and move this ADR to **accepted**.
