# Installing Sombra

One command installs Sombra on **macOS 14+ (Apple Silicon)** or **Ubuntu 22.04/24.04 (x86_64, aarch64)**:

```sh
curl -fsSL https://github.com/nickmaglowsch/sombra-call/releases/latest/download/install.sh | sh
```

Pass options after `sh -s --`, for example a pinned release without questions:

```sh
curl -fsSL https://github.com/nickmaglowsch/sombra-call/releases/latest/download/install.sh | sh -s -- --version v0.2.0 --yes
```

On macOS it installs **`Sombra.app`**, signed with the project's own certificate, so macOS asks for the Microphone, Screen Recording and Accessibility permissions **for Sombra** and lists Sombra in System Settings, not your terminal. The grants stay across upgrades ([ADR 0050](adr/0050-macos-app-bundle.md)). `--no-app` installs the command-line tool with uv instead, as on Ubuntu.

On Ubuntu, live capture (microphone, system audio, screen) is not built yet ([ADR 0016](adr/0016-linux-wayland-adapters.md)). Everything else works: `replay`, `ask`, `minutes`, `report`. `sombra doctor` says so as a warning.

## Homebrew (macOS)

On macOS you can install `Sombra.app` with Homebrew instead, in two steps:

```sh
brew install --cask nickmaglowsch/sombra/sombra
xattr -dr com.apple.quarantine /Applications/Sombra.app
```

This installs `Sombra.app` into `/Applications` and links the `sombra` command, which runs through the app. Microphone, Screen Recording and Accessibility are then granted to **Sombra**, not to your terminal.

The second step is needed because `Sombra.app` is signed with the project's own self-signed certificate and is **not notarized by Apple**: Sombra has no paid Apple Developer account (#50). Homebrew quarantines everything it downloads, and Gatekeeper blocks the first launch of an unnotarized app while it is quarantined. The cask never removes the flag itself. You clear it explicitly, for this app only. Instead of `xattr`, you can open Sombra once and then click System Settings → Privacy & Security → **Open Anyway**. The zip is checked against the release's `SHA256SUMS` either way.

Homebrew prints the next steps: `sombra doctor`, `sombra models download`, grant the permissions to Sombra, then `sombra setup`. It needs macOS 14+ on Apple Silicon, like the installer.

- **Upgrade:** `brew upgrade --cask sombra`.
- **Uninstall:** `brew uninstall --cask sombra` keeps your config and models. `brew uninstall --cask --zap sombra` also deletes `~/.config/sombra` and `~/.cache/sombra`. Neither ever deletes your meetings in `~/Sombra/meetings`.

The cask is available from the first release that ships `Sombra.app` (#50). How the tap is kept up to date is in [release.md](release.md#homebrew-tap).

## What the installer does

Every step is printed before it runs. Nothing is sent anywhere (no telemetry).

1. Checks the OS and CPU and stops with a clear message on anything unsupported.
2. On Ubuntu, checks for `curl` and `ca-certificates` and, only if one is missing, asks before running `sudo apt-get install`. Sombra's Python dependencies ship prebuilt wheels and need no other system packages.
3. **On macOS**, installs `Sombra.app` (unless `--no-app`):
   - downloads `Sombra-<version>-macos-arm64.zip` from the release and checks it against `SHA256SUMS` (or uses `--from-app-zip PATH`);
   - unpacks it with `ditto`, checks it is Sombra (bundle id `io.github.nickmaglowsch.Sombra`) and that its code signature verifies (`codesign --verify --deep --strict`), and refuses to install otherwise. It warns if the app is ad-hoc signed (a development build or a labelled pre-release), because macOS then asks for the permissions again after every upgrade;
   - copies it to `/Applications/Sombra.app`, or `~/Applications/Sombra.app` when `/Applications` isn't writable. An existing Sombra.app is upgraded in place; any other app with that name is never touched;
   - removes the `com.apple.quarantine` attribute from the app (see [Gatekeeper](#gatekeeper-sombraapp-is-not-notarized));
   - links the app's command-line shim, `Sombra.app/Contents/Helpers/sombra`, to `~/.local/bin/sombra`, and removes an older uv-tool install of sombra so only one is on `PATH`. No uv or Python is needed: the app carries its own.

   A release without `Sombra.app` (older releases) falls back to the uv install below, and says so.
4. **Otherwise** (Ubuntu, `--no-app`, `--from-wheel`, or no app in the release), installs [uv](https://docs.astral.sh/uv/) into your user directory (`~/.local/bin`) with its official installer if it is missing (no sudo), then Sombra with `uv tool install` on a uv-managed Python 3.12 (your system Python is never used):
   - from a GitHub release (`--version vX.Y.Z`, default the latest), after checking the wheel against the release's `SHA256SUMS`;
   - or from a local wheel (`--from-wheel PATH`).
5. Downloads the default whisper model and Silero VAD with `sombra models download` (about 580 MB, SHA-256 checked) into `~/.cache/sombra/models`.
6. If the directory `sombra` is linked into (`~/.local/bin`) is not on your `PATH`, prints the exact line to add. With `--modify-path` it appends that line to your shell rc file instead (`~/.zshrc`, `~/.bashrc`, `~/.bash_profile` on macOS bash, or `~/.profile`).
7. Writes `~/.config/sombra/config.toml` with the defaults (`sombra config init`) if it does not exist. An existing file is never touched.
8. Runs `sombra doctor` and prints the next steps.

Re-running the installer upgrades Sombra in place; your config, models and meetings stay.

## Gatekeeper: Sombra.app is not notarized

Sombra.app is signed with the project's own certificate, not an Apple Developer ID, so Apple has not notarized it ([ADR 0050](adr/0050-macos-app-bundle.md#gatekeeper-without-notarization)). macOS blocks such an app only when it carries the quarantine flag that browsers add to downloads:

- **`install.sh`:** nothing to do. `curl` sets no quarantine, and the installer removes it anyway.
- **Homebrew:** run the second step of the [Homebrew install](#homebrew-macos), or follow the steps below as for a browser download. The cask never removes the flag itself. `install.sh` does strip the flag, but only on the app it installed, because you ran that script explicitly.
- **You downloaded the zip in a browser:** after moving `Sombra.app` to Applications, either run

  ```sh
  xattr -dr com.apple.quarantine /Applications/Sombra.app
  ```

  or, on macOS 14, right-click Sombra.app → **Open** → **Open**. On macOS 15 and later, open it once, then go to System Settings → Privacy & Security and click **Open Anyway**.

  Sombra.app has no window: after **Open Anyway**, nothing appears on screen, and that is expected. The app is now allowed; run `sombra` in a terminal (checked on macOS 27: the quarantine flag stays but is marked as approved, and Gatekeeper no longer blocks it).

What you are trusting is the release on GitHub: `install.sh` checks the download against the release's `SHA256SUMS` over HTTPS. To check the app's certificate yourself, see [docs/release.md](release.md#verifying-a-download).

## Options

| Option | Effect |
| --- | --- |
| `--version vX.Y.Z` | install this release instead of the latest |
| `--from-wheel PATH` | install a local wheel (testing and the release smoke job); implies `--no-app` |
| `--from-app-zip PATH` | macOS: install a local `Sombra-<version>-macos-arm64.zip` (testing and the release app-smoke job) |
| `--no-app` | macOS: install the command-line tool with uv instead of `Sombra.app`; permissions then go to your terminal app |
| `--yes` | non-interactive: answer yes to every question (sudo, deleting models on uninstall) |
| `--no-models` | skip the model download; run `sombra models download` later |
| `--modify-path` | add `~/.local/bin` (where `sombra` is linked or installed) to your shell rc file |
| `--uninstall` | remove Sombra; asks before deleting the models |

Without a terminal (for example under CI) and without `--yes`, every question is answered "no".

## Uninstall

```sh
curl -fsSL https://github.com/nickmaglowsch/sombra-call/releases/latest/download/install.sh | sh -s -- --uninstall
```

This removes `Sombra.app` and its `~/.local/bin/sombra` link on macOS, runs `uv tool uninstall sombra` if Sombra was installed with uv, and, after you confirm, deletes the model files it downloaded from `~/.cache/sombra/models` (the folder too, unless you put other files in it). It **never** deletes your meetings (`~/Sombra/meetings`) or your config (`~/.config/sombra`), and leaves uv installed. Delete those yourself if you want them gone; API keys are removed with `sombra auth clear <provider>`. macOS keeps the permissions you granted Sombra after the app is gone; `tccutil reset All io.github.nickmaglowsch.Sombra` removes them (the uninstaller prints this command).

## `sombra doctor`

`sombra doctor` checks the machine and prints one line per check, `ok`, `warn` or `FAIL`, with the fix for anything that is not ok. It exits 1 when any check fails. `sombra doctor --live` adds one test question through the configured agent. `sombra doctor --json` prints the same as JSON (`{"ok": bool, "checks": [{"section", "name", "status", "detail", "fix"}]}`).

| Section | Checks |
| --- | --- |
| Sombra | sombra and Python versions; each model present and matching its SHA-256; free disk space where meetings go (warn under 5 GiB, fail under 1 GiB); the OS keychain (macOS Keychain, Linux Secret Service) reachable |
| macOS | macOS 14+; which app holds the permissions, and (Sombra.app) whether its signature keeps them across upgrades; Microphone, Screen Recording and Accessibility permissions; the default input device; the system-audio route (Core Audio process tap on 14.4+, or a loopback such as BlackHole) |
| Linux | Ubuntu 22.04/24.04; live capture "not supported yet (ADR 0016)" as a warning |
| Agent | the configured backend (`[brain]`) and summary backend (`[summary]`), each with what it needs: the `claude` / `codex` CLI on `PATH` at its minimum version and logged in (read from `claude auth status` / `codex login status`, whose output is never printed), or the `anthropic` / `openai` key in the keychain. A missing CLI, login or key is a FAIL; a CLI logged in with an API key when you chose a subscription is a warning. `sombra doctor --live` also asks the agent one test question ([providers.md](providers.md)) |

Permissions are read without triggering the macOS prompt. macOS grants them to the **responsible app** of the running process, and the **permissions holder** check names it:

- `Sombra (io.github.nickmaglowsch.Sombra) at /Applications/Sombra.app`: the default install. The grants belong to Sombra alone and survive upgrades.
- Your terminal (Terminal, iTerm, VS Code, ...): the uv install (`--no-app`). The grants cover every program run from that terminal; `doctor` suggests installing Sombra.app.
- A warning when `sombra` runs from Sombra.app but the terminal is still responsible: something started `Sombra.app/Contents/MacOS/Sombra` directly instead of through the `sombra` shim.

Running from Sombra.app, the **app signature** check says whether macOS will keep those grants when you upgrade. It does for a release signed with Sombra's certificate. For an **ad-hoc** signed build (a development build, or a pre-release labelled ad-hoc) macOS asks again after every upgrade.

To grant one: System Settings > Privacy & Security > Microphone (or Screen Recording, or Accessibility), enable the app `doctor` named, then restart `sombra`. macOS 15 and later also ask you now and then to confirm that Sombra may keep recording the screen; that applies to every app. The Screen Recording and Accessibility probes cannot tell "denied" from "not asked yet", so both show as a warning until granted. System Audio Recording (for the process tap) has no probe; macOS asks for it on the first meeting.

## `sombra models`

```sh
sombra models download                 # default whisper model + Silero VAD
sombra models download tiny small-q5_1 # specific whisper models (+ Silero VAD)
sombra models list                     # known models, size, present/missing
sombra models path                     # the models folder
sombra models path tiny                # one model's file
```

All take `--dir PATH` to use another folder. Files are downloaded to `<name>.part` and moved into place only when their SHA-256 matches the pinned value, so a file in the folder is always a verified one. Re-running skips files that are already there.

## Next steps after installing

1. Fix what `sombra doctor` reports and run it again until it is all `ok`.
2. Choose the agent provider with `sombra setup`: Claude or Codex, on your subscription or an API key ([providers.md](providers.md)).
3. Create your first meeting: `sombra new "Daily"` ([config.md](config.md)).

## Release assets the installer expects

A release (built by the release workflow, #44) must publish `install.sh`, `SHA256SUMS` and exactly one `sombra-<version>-py3-none-any.whl` listed in `SHA256SUMS` (`sha256sum` format). On macOS the installer also looks for `Sombra-<version>-macos-arm64.zip` in `SHA256SUMS` (#50), and falls back to the wheel when there is none. The installer reads asset names from `SHA256SUMS`, so the version does not need to be known in advance for `latest`.
