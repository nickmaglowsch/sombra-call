# Releases

Sombra ships as GitHub Releases, built by `.github/workflows/release.yml` when a tag `vX.Y.Z` is pushed. Why a wheel and `uv tool install`, rather than an app bundle or Homebrew, is decided in [ADR 0044](adr/0044-release-packaging.md).

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
   - **publish**: only when build and every smoke job pass. It creates the GitHub Release with the assets below and the release notes.
4. If a job fails, nothing is published. Fix it on `main` through a PR, then tag the next patch or `-rcN`. Never move a published tag.

PRs that touch the release files run the same **build** and **smoke** jobs, without **publish**.

## What's in a release

| Asset | What |
| --- | --- |
| `sombra-X.Y.Z-py3-none-any.whl` | the app; the same wheel on every platform (native dependencies come from PyPI at install time) |
| `sombra-X.Y.Z.tar.gz` | the sdist |
| `install.sh` | the one-line installer (see `docs/install.md`) |
| `SHA256SUMS` | SHA-256 of every other asset, in `sha256sum` format |

The release notes list the titles of the PRs merged since the previous release. A final release (`v0.3.0`) counts from the previous final release, and includes everything its release candidates had. A pre-release counts from the tag just before it. `scripts/release/notes.py` generates them from the first-parent history of `main`, where each squash-merge commit is one PR.

Supported platforms: macOS 14+ on Apple Silicon, and Ubuntu 22.04/24.04 on x86_64 and aarch64. Intel Macs are not supported ([ADR 0044](adr/0044-release-packaging.md)). Do not `pip install sombra` or `uv tool install sombra` from PyPI: that name belongs to an unrelated project.

## Verifying a download

Download `SHA256SUMS` into the same folder as the assets, then run:

```sh
sha256sum -c --ignore-missing SHA256SUMS        # Ubuntu
shasum -a 256 -c --ignore-missing SHA256SUMS    # macOS
```

Every file you downloaded must print `OK`. `--ignore-missing` skips the assets you didn't download. `install.sh` does the same check before it installs a release. `SHA256SUMS` proves the files match what the workflow built, not who built them. Releases are not signed yet: signing is the `.app` follow-up (#50) in ADR 0044.

## Installing a release by hand

Without the installer, with [uv](https://docs.astral.sh/uv/) installed:

```sh
uv tool install --python 3.12 \
  https://github.com/nickmaglowsch/sombra-call/releases/download/v0.2.0/sombra-0.2.0-py3-none-any.whl
sombra --version
```

- **Upgrade:** run the same command with the new URL, adding `--force`.
- **Uninstall:** `uv tool uninstall sombra`. Your config (`~/.config/sombra`), models (`~/.cache/sombra/models`) and meetings (`~/Sombra/meetings`) stay.

On macOS, grant Microphone, Screen Recording and Accessibility to the terminal app you run `sombra` from ([ADR 0044](adr/0044-release-packaging.md#evidence-macos-tcc)).

## Homebrew tap

`brew install --cask nickmaglowsch/sombra/sombra` installs `Sombra.app` (#51), so the macOS permissions belong to Sombra. The app is self-signed and not notarized by Apple: the owner decided against a paid Apple Developer account (#50). The cask's caveats and the tap README say so. The cask lives in the tap repository `nickmaglowsch/homebrew-sombra`. It is rendered from `packaging/homebrew/sombra.rb.tmpl` and needs the release asset `Sombra-<version>-macos-arm64.zip` from #50, listed in `SHA256SUMS`. A release without that zip gets no cask: the Homebrew workflow fails instead of falling back to the wheel. homebrew-core is out of reach, because its policy builds from source and onnxruntime publishes no sdist ([ADR 0044](adr/0044-release-packaging.md)).

### One-time setup (owner)

1. Create the public repository `nickmaglowsch/homebrew-sombra` from the bootstrap in `packaging/homebrew/tap/` (the README and an empty `Casks/`):

   ```sh
   gh repo create nickmaglowsch/homebrew-sombra --public --clone \
     --description "Homebrew tap for Sombra"
   cp -R sombra-call/packaging/homebrew/tap/. homebrew-sombra/   # from the folder holding both
   cd homebrew-sombra && git add -A && git commit -m "Bootstrap the tap" && git push -u origin HEAD
   ```

   The name must start with `homebrew-` so that `brew tap nickmaglowsch/sombra` finds it.
2. Create a **fine-grained personal access token** limited to that one repository, with **Contents: Read and write** and nothing else. Give it an expiry you will remember to renew.
3. Add it to `nickmaglowsch/sombra-call` as the repository secret `HOMEBREW_TAP_TOKEN` (Settings > Secrets and variables > Actions).
4. Check it: run **Homebrew** from the Actions tab with `dry_run` on. The `bump tap (dry run)` job prints the diff it would push.

Without `HOMEBREW_TAP_TOKEN`, the workflow still renders and audits the cask, but the push step is a no-op with a notice.

### What happens on a release

The **Homebrew** workflow (`.github/workflows/homebrew.yml`) runs when the Release workflow finishes on a tag. `release.yml` publishes with `GITHUB_TOKEN`, and that starts no `release` event, so this is a `workflow_run` trigger. The workflow also runs on `release: published`, for a release published by hand.

1. **resolve**: skips pre-releases. The tap only gets final versions.
2. **validate** (macos-14): downloads the release's `SHA256SUMS` and renders the cask with `packaging/homebrew/cask.py render`. It then runs `brew style` and `brew audit --cask --strict` on it, from a throwaway local tap.
3. **smoke** (macos-14): `brew install --cask` from the real release URL (the cask exactly as it will go to the tap), then `sombra --version`, which must print the version. Then `brew uninstall --cask --zap`, which must leave `~/Sombra/meetings` intact.
4. **bump**: clones the tap with the token and runs `cask.py bump`. It commits `Casks/sombra.rb` as `sombra X.Y.Z` and pushes. An unchanged cask pushes nothing. A patch to an older line (`v0.2.1` after `v0.3.0`) never downgrades the tap.

PRs that touch `packaging/homebrew/` run **validate** against a fake `SHA256SUMS`, plus the bump as a dry run that prints its diff. By hand (Actions > Homebrew > Run workflow):

- `tag`: the release to render (empty means the latest).
- `dry_run`: on by default; it prints the diff instead of pushing.
- `app_zip_url`: smoke-installs any `Sombra-<version>-macos-arm64.zip`, such as an rc asset, through a `file://` cask.

To re-publish a cask after fixing the template, run it by hand with `dry_run` off. The cask never removes `~/Sombra/meetings`: `zap` deletes only `~/.cache/sombra` and `~/.config/sombra`, and a unit test holds that.

## Release helpers

`scripts/release/` holds small, standard-library-only scripts, tested in `tests/release/`:

- `tags.py check <tag> [--dist dist]`: validates the tag, checks the wheel and sdist versions, and prints `version=` / `prerelease=`.
- `checksums.py write|verify <dir>`: writes and verifies `SHA256SUMS`.
- `notes.py <tag> [--repo owner/name] [--output file]`: writes the release notes.

The Homebrew cask renderer is `packaging/homebrew/cask.py`, tested in `tests/packaging/` (see [Homebrew tap](#homebrew-tap)).
