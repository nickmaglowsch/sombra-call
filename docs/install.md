# Installing Sombra

One command installs Sombra on **macOS 14+ (Apple Silicon)** or **Ubuntu 22.04/24.04 (x86_64, aarch64)**:

```sh
curl -fsSL https://github.com/nickmaglowsch/sombra-call/releases/latest/download/install.sh | sh
```

Pass options after `sh -s --`, for example a pinned release without questions:

```sh
curl -fsSL https://github.com/nickmaglowsch/sombra-call/releases/latest/download/install.sh | sh -s -- --version v0.2.0 --yes
```

On Ubuntu, live capture (microphone, system audio, screen) is not built yet ([ADR 0016](adr/0016-linux-wayland-adapters.md)). Everything else works: `replay`, `ask`, `minutes`, `report`. `sombra doctor` says so as a warning.

## What the installer does

Every step is printed before it runs. Nothing is sent anywhere (no telemetry).

1. Checks the OS and CPU and stops with a clear message on anything unsupported.
2. On Ubuntu, checks for `curl` and `ca-certificates` and, only if one is missing, asks before running `sudo apt-get install`. Sombra's Python dependencies ship prebuilt wheels and need no other system packages.
3. Installs [uv](https://docs.astral.sh/uv/) into your user directory (`~/.local/bin`) with its official installer if it is missing. No sudo.
4. Installs Sombra with `uv tool install` on a uv-managed Python 3.12 (your system Python is never used):
   - from a GitHub release (`--version vX.Y.Z`, default the latest), after checking the wheel against the release's `SHA256SUMS`;
   - or from a local wheel (`--from-wheel PATH`).
5. Downloads the default whisper model and Silero VAD with `sombra models download` (about 580 MB, SHA-256 checked) into `~/.cache/sombra/models`.
6. If uv's tool directory (`~/.local/bin`) is not on your `PATH`, prints the exact line to add. With `--modify-path` it appends that line to your shell rc file instead (`~/.zshrc`, `~/.bashrc`, `~/.bash_profile` on macOS bash, or `~/.profile`).
7. Writes `~/.config/sombra/config.toml` with the defaults (`sombra config init`) if it does not exist. An existing file is never touched.
8. Runs `sombra doctor` and prints the next steps.

Re-running the installer upgrades Sombra in place; your config, models and meetings stay.

## Options

| Option | Effect |
| --- | --- |
| `--version vX.Y.Z` | install this release instead of the latest |
| `--from-wheel PATH` | install a local wheel (testing and the release smoke job) |
| `--yes` | non-interactive: answer yes to every question (sudo, deleting models on uninstall) |
| `--no-models` | skip the model download; run `sombra models download` later |
| `--modify-path` | add uv's tool bin directory to your shell rc file |
| `--uninstall` | remove Sombra; asks before deleting the models |

Without a terminal (for example under CI) and without `--yes`, every question is answered "no".

## Uninstall

```sh
curl -fsSL https://github.com/nickmaglowsch/sombra-call/releases/latest/download/install.sh | sh -s -- --uninstall
```

This runs `uv tool uninstall sombra` and, after you confirm, deletes the model files it downloaded from `~/.cache/sombra/models` (the folder too, unless you put other files in it). It **never** deletes your meetings (`~/Sombra/meetings`) or your config (`~/.config/sombra`), and leaves uv installed. Delete those yourself if you want them gone; API keys are removed with `sombra auth clear <provider>`.

## `sombra doctor`

`sombra doctor` checks the machine and prints one line per check, `ok`, `warn` or `FAIL`, with the fix for anything that is not ok. It exits 1 when any check fails. `sombra doctor --live` adds one test question through the configured agent. `sombra doctor --json` prints the same as JSON (`{"ok": bool, "checks": [{"section", "name", "status", "detail", "fix"}]}`).

| Section | Checks |
| --- | --- |
| Sombra | sombra and Python versions; each model present and matching its SHA-256; free disk space where meetings go (warn under 5 GiB, fail under 1 GiB); the OS keychain (macOS Keychain, Linux Secret Service) reachable |
| macOS | macOS 14+; Microphone, Screen Recording and Accessibility permissions; the default input device; the system-audio route (Core Audio process tap on 14.4+, or a loopback such as BlackHole) |
| Linux | Ubuntu 22.04/24.04; live capture "not supported yet (ADR 0016)" as a warning |
| Agent | the configured backend (`[brain]`) and summary backend (`[summary]`), each with what it needs: the `claude` / `codex` CLI on `PATH` at its minimum version and logged in (read from `claude auth status` / `codex login status`, whose output is never printed), or the `anthropic` / `openai` key in the keychain. A missing CLI, login or key is a FAIL; a CLI logged in with an API key when you chose a subscription is a warning. `sombra doctor --live` also asks the agent one test question ([providers.md](providers.md)) |

Permissions are read without triggering the macOS prompt. macOS grants them to **the app you run `sombra` from** (Terminal, iTerm, VS Code, ...), not to Sombra itself, so `doctor` names that app. To grant one: System Settings > Privacy & Security > Microphone (or Screen Recording, or Accessibility), enable that app, then restart it. The Screen Recording and Accessibility probes cannot tell "denied" from "not asked yet", so both show as a warning until granted. System Audio Recording (for the process tap) has no probe; macOS asks for it on the first meeting.

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

A release (built by the release workflow, #44) must publish `install.sh`, `SHA256SUMS` and exactly one `sombra-<version>-py3-none-any.whl` listed in `SHA256SUMS` (`sha256sum` format). The installer reads the wheel's name from `SHA256SUMS`, so the version does not need to be known in advance for `latest`.
