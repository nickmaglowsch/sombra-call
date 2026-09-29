# Releases

Sombra ships as GitHub Releases, built by `.github/workflows/release.yml` when a tag `vX.Y.Z` is pushed. Why a wheel and `uv tool install`, rather than an app bundle or Homebrew, is decided in [ADR 0044](adr/0044-release-packaging.md).

## Versioning

- [SemVer](https://semver.org/). During the MVP the major version stays `0`: `0.MINOR.PATCH`, where a minor bump may break config or CLI flags, and a patch bump only fixes things.
- **The git tag is the version.** `pyproject.toml` has no version number. hatch-vcs reads it from the tag at build time, so cutting a release needs no commit on `main`, which only takes squash-merged PRs.
- Tag format: `vX.Y.Z` for a release, and `vX.Y.Z-rcN`, `vX.Y.Z-betaN` or `vX.Y.Z-alphaN` for a pre-release. Pre-releases are published as GitHub pre-releases, and `releases/latest` never points at them. Python normalizes the version (PEP 440): `v0.2.0-rc1` builds `sombra 0.2.0rc1`.
- **Untagged builds get a dev version**, such as `0.2.1.dev4+g1a2b3c4`: the next patch, the number of commits since the last tag, and the commit. A dirty tree adds `.dYYYYMMDD`. `sombra --version` prints it. In a checkout, `uv sync` rebuilds the editable install when HEAD or the tags move, so the number stays current. A source tree with no git metadata at all builds as `0.0.0`.

## Cutting a release

1. Make sure `main` is green and holds everything the release needs.
2. Tag the merged commit on `main` and push the tag. Only maintainers push tags.

   ```sh
   git fetch origin
   git tag -a v0.2.0 -m "Sombra v0.2.0" origin/main
   git push origin v0.2.0
   ```

3. Watch the **Release** workflow. It runs:
   - **build**: checks that the tagged commit is on `main` and builds the sdist and wheel once. It checks that their version is the tag's, adds `install.sh`, and writes and verifies `SHA256SUMS`.
   - **smoke**: on clean `macos-14`, `ubuntu-latest`, `ubuntu-22.04` and `ubuntu-24.04-arm` runners, it verifies `SHA256SUMS` with the stock tool. It then installs the wheel as users do: `install.sh --from-wheel` once #45 lands, `uv tool install` on a uv-managed CPython 3.12 until then. Finally it runs `sombra --version` (which must print the tag's version), `sombra --help` and `sombra new`.
   - **publish**: only when build and every smoke job pass. It creates the GitHub Release with the assets below and the release notes.
4. If a job fails, nothing is published. Fix it on `main` through a PR, then tag the next patch or `-rcN`. Never move a published tag.

PRs that touch the release files run the same **build** and **smoke** jobs, without **publish**.

## What's in a release

| Asset | What |
| --- | --- |
| `sombra-X.Y.Z-py3-none-any.whl` | the app; the same wheel on every platform (native dependencies come from PyPI at install time) |
| `sombra-X.Y.Z.tar.gz` | the sdist |
| `install.sh` | the one-line installer (#45), once it exists |
| `SHA256SUMS` | SHA-256 of every other asset, in `sha256sum` format |

The release notes list the titles of the PRs merged since the previous release. A final release (`v0.3.0`) counts from the previous final release, and includes everything its release candidates had. A pre-release counts from the tag just before it. `scripts/release/notes.py` generates them from the first-parent history of `main`, where each squash-merge commit is one PR.

Supported platforms: macOS 14+ on Apple Silicon, and Ubuntu 22.04/24.04 on x86_64 and aarch64. Intel Macs are not supported ([ADR 0044](adr/0044-release-packaging.md)). Do not `pip install sombra` or `uv tool install sombra` from PyPI: that name belongs to an unrelated project.

## Verifying a download

Download `SHA256SUMS` into the same folder as the assets, then run:

```sh
sha256sum -c --ignore-missing SHA256SUMS        # Ubuntu
shasum -a 256 -c --ignore-missing SHA256SUMS    # macOS
```

Every file you downloaded must print `OK`. `--ignore-missing` skips the assets you didn't download. `install.sh` does the same check before it installs a release. `SHA256SUMS` proves the files match what the workflow built, not who built them. Releases are not signed yet: signing is the `.app` follow-up in ADR 0044.

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

## Release helpers

`scripts/release/` holds small, standard-library-only scripts, tested in `tests/release/`:

- `tags.py check <tag> [--dist dist]`: validates the tag, checks the wheel and sdist versions, and prints `version=` / `prerelease=`.
- `checksums.py write|verify <dir>`: writes and verifies `SHA256SUMS`.
- `notes.py <tag> [--repo owner/name] [--output file]`: writes the release notes.
