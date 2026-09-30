# Homebrew tap for Sombra

This is the Homebrew tap for [Sombra](https://github.com/nickmaglowsch/sombra-call), a local meeting agent. It holds a single cask that installs `Sombra.app` from the project's GitHub Releases. The cask needs macOS 14 (Sonoma) or later on Apple Silicon.

```sh
brew install --cask nickmaglowsch/sombra/sombra
```

**Not notarized.** Sombra has no paid Apple Developer account, so `Sombra.app` is signed with the project's own self-signed certificate and is not notarized by Apple. The certificate is the same on every release, which is why the permissions you grant persist across upgrades. The download is checked against the release's `SHA256SUMS`. If macOS says it cannot check the app, open System Settings > Privacy & Security and click **Open Anyway** for Sombra.

Then follow the caveats Homebrew prints:

1. `sombra doctor` checks the models, permissions and agent CLIs.
2. `sombra models download` fetches the whisper and VAD models (about 580 MB).
3. Grant Microphone, Screen Recording and Accessibility to **Sombra** in System Settings > Privacy & Security. The grant goes to Sombra, not to your terminal.
4. `sombra setup` chooses the agent provider.

| Task | Command |
| --- | --- |
| Upgrade | `brew upgrade --cask sombra` |
| Uninstall (keeps config and models) | `brew uninstall --cask sombra` |
| Uninstall and delete config and models | `brew uninstall --cask --zap sombra` |

`--zap` deletes `~/.config/sombra` and `~/.cache/sombra`. It **never** deletes your meetings in `~/Sombra/meetings`: remove that folder yourself if you want it gone.

## How this tap is maintained

Do not edit `Casks/sombra.rb` by hand. The **Homebrew** workflow in `nickmaglowsch/sombra-call` (`.github/workflows/homebrew.yml`) renders it from `packaging/homebrew/sombra.rb.tmpl` and the release's `SHA256SUMS`. It installs and uninstalls the cask on macOS, then pushes it here for every final release. Pre-releases are never published here. Report problems in [sombra-call's issues](https://github.com/nickmaglowsch/sombra-call/issues).
