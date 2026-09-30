# Homebrew packaging

`brew install --cask nickmaglowsch/sombra/sombra` installs `Sombra.app` (issue #51). The app is self-signed and not notarized by Apple (#50), and the cask never removes the quarantine flag itself. Users clear it explicitly afterwards with `xattr -dr com.apple.quarantine /Applications/Sombra.app`, or with Open Anyway. The setup and release flow are in [docs/release.md](../../docs/release.md#homebrew-tap).

| File | What |
| --- | --- |
| `sombra.rb.tmpl` | the cask template (`@@VERSION@@`, `@@SHA256@@`, `@@URL@@`, `@@SHIM@@`) |
| `cask.py` | `render` fills in the template from a release's `SHA256SUMS`. `bump` writes it into a tap checkout and prints the diff; it never downgrades. Standard library only, tested in `tests/packaging/`. |
| `ci.sh` | `audit` runs `brew style` and `brew audit --cask --strict`. `smoke` runs a plain install and asserts that the app is quarantined. It then does the documented user step (`xattr -dr com.apple.quarantine`), runs `sombra --version` and `sombra doctor --json`, then `uninstall --zap`, and checks that `~/Sombra/meetings` survives. Both use a throwaway local tap and run on macos-14. |
| `tap/` | the bootstrap for the `nickmaglowsch/homebrew-sombra` repository: its README and the `Casks/` folder |

The cask depends on the release asset from #50: `Sombra-<version>-macos-arm64.zip` (a ditto zip of `Sombra.app`) listed in `SHA256SUMS`, with the CLI shim at `Sombra.app/Contents/Helpers/sombra` (ADR 0050). `SHIM` in `cask.py` must match `SHIM_RELPATH` in `packaging/macos/sombra_app.py`; a test checks it.
