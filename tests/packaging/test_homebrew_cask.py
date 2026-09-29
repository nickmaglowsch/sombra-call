"""The Homebrew cask renderer and tap bump (issue #51).

``brew style`` / ``brew audit --cask --strict`` and the install/uninstall smoke test run
on macos-14 in ``.github/workflows/homebrew.yml``; these tests pin the logic that feeds them.
"""

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from cask import (
    CASK_PATH,
    DEFAULT_URL,
    SHIM,
    TEMPLATE,
    CaskError,
    asset_name,
    bump,
    final_version,
    main,
    parse_sums,
    render,
    sha256_for,
    tap_version,
)

ZIP_SHA = hashlib.sha256(b"app zip").hexdigest()
WHEEL_SHA = hashlib.sha256(b"wheel").hexdigest()
SUMS = (
    f"{ZIP_SHA}  Sombra-0.3.0-macos-arm64.zip\n"
    f"{WHEEL_SHA}  sombra-0.3.0-py3-none-any.whl\n"
    f"{hashlib.sha256(b'sh').hexdigest()}  install.sh\n"
)
REPO_ROOT = Path(__file__).resolve().parents[2]


def _stanza(text: str, name: str) -> str:
    """The body of a ``name ... [ ... ]`` array stanza, e.g. ``zap trash: [ ... ]``."""
    m = re.search(rf"^\s*{name}\b[^\[]*\[(?P<body>.*?)\]", text, re.MULTILINE | re.DOTALL)
    assert m is not None, f"no {name} stanza"
    return m["body"]


# --- versions and SHA256SUMS -------------------------------------------------------------


def test_asset_name_is_the_one_issue_50_publishes() -> None:
    assert asset_name("0.3.0") == "Sombra-0.3.0-macos-arm64.zip"


@pytest.mark.parametrize(("given", "expected"), [("v0.3.0", "0.3.0"), ("1.20.3", "1.20.3")])
def test_final_version_accepts_tags_and_bare_versions(given: str, expected: str) -> None:
    assert final_version(given) == expected


@pytest.mark.parametrize("bad", ["v0.3.0-rc1", "v0.3.0-beta2", "v0.3", "v01.2.3", "latest", ""])
def test_final_version_rejects_pre_releases_and_junk(bad: str) -> None:
    with pytest.raises(CaskError):
        final_version(bad)


def test_sha256_is_the_zips_line_of_sha256sums() -> None:
    assert sha256_for(SUMS, "0.3.0") == ZIP_SHA


def test_sums_in_binary_mode_and_upper_case_are_read() -> None:
    assert sha256_for(f"{ZIP_SHA.upper()} *Sombra-0.3.0-macos-arm64.zip\n", "0.3.0") == ZIP_SHA


def test_missing_zip_names_what_the_sums_file_has() -> None:
    with pytest.raises(CaskError, match=r"no Sombra-0\.4\.0-macos-arm64\.zip.*install\.sh"):
        sha256_for(SUMS, "0.4.0")


def test_a_wheel_only_release_has_no_cask() -> None:
    # A release built before #50 has no app zip: the cask must not fall back to the wheel.
    with pytest.raises(CaskError, match=r"no Sombra-0\.3\.0"):
        sha256_for(f"{WHEEL_SHA}  sombra-0.3.0-py3-none-any.whl\n", "0.3.0")


@pytest.mark.parametrize(
    "bad",
    [
        "nothex  Sombra-0.3.0-macos-arm64.zip",
        f"{ZIP_SHA[:-1]}  Sombra-0.3.0-macos-arm64.zip",
        ZIP_SHA,
        f"{ZIP_SHA}  Sombra-0.3.0-macos-arm64.zip\n{WHEEL_SHA}  Sombra-0.3.0-macos-arm64.zip",
    ],
)
def test_malformed_or_contradictory_sums_are_rejected(bad: str) -> None:
    with pytest.raises(CaskError):
        parse_sums(bad)


def test_blank_lines_and_repeated_identical_lines_are_fine() -> None:
    line = f"{ZIP_SHA}  Sombra-0.3.0-macos-arm64.zip"
    assert parse_sums(f"\n{line}\n\n{line}\n") == {"Sombra-0.3.0-macos-arm64.zip": ZIP_SHA}


# --- the rendered cask -------------------------------------------------------------------


def test_render_fills_version_sha_and_the_release_url() -> None:
    text = render("0.3.0", ZIP_SHA)
    assert 'version "0.3.0"' in text
    assert f'sha256 "{ZIP_SHA}"' in text
    assert (
        'url "https://github.com/nickmaglowsch/sombra-call/releases/download/'
        'v#{version}/Sombra-#{version}-macos-arm64.zip"'
    ) in text
    assert "@@" not in text


def test_default_url_expands_to_the_release_asset() -> None:
    # What Homebrew's #{version} interpolation turns the URL into.
    url = DEFAULT_URL.replace("#{version}", "0.3.0")
    assert url.endswith("/releases/download/v0.3.0/" + asset_name("0.3.0"))


def test_cask_installs_the_app_and_links_the_bundles_cli_shim() -> None:
    text = render("0.3.0", ZIP_SHA)
    assert 'cask "sombra" do' in text
    assert 'app "Sombra.app"' in text
    assert f'binary "#{{appdir}}/{SHIM}"' in text
    assert SHIM.startswith("Sombra.app/Contents/")
    assert text.count("depends_on arch: :arm64") == 1
    assert 'depends_on macos: ">= :sonoma"' in text


def test_livecheck_follows_github_releases() -> None:
    text = render("0.3.0", ZIP_SHA)
    assert re.search(r"livecheck do\s+url :url\s+strategy :github_latest\s+end", text)


def test_zap_removes_only_config_and_cache() -> None:
    text = render("0.3.0", ZIP_SHA)
    entries = re.findall(r'"([^"]+)"', _stanza(text, "zap"))
    assert entries == ["~/.cache/sombra", "~/.config/sombra"]


def test_nothing_in_the_cask_can_delete_meetings() -> None:
    # ~/Sombra/meetings is user data: no zap, uninstall or trash stanza may name ~/Sombra.
    text = render("0.3.0", ZIP_SHA)
    caveats = re.search(r"caveats <<~EOS\n(?P<body>.*?)\n\s*EOS", text, re.DOTALL)
    assert caveats is not None
    code = text.replace(caveats.group(0), "")
    code = "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("#"))
    assert "~/Sombra" not in code
    assert "Sombra/meetings" not in code
    assert "uninstall " not in code  # no uninstall stanza that could delete or trash paths
    assert "rmdir" not in code


def test_caveats_give_the_next_steps() -> None:
    text = render("0.3.0", ZIP_SHA)
    for step in ("sombra doctor", "sombra setup", "sombra models download", "to Sombra"):
        assert step in text
    assert "~/Sombra/meetings are never removed" in text


def test_render_takes_a_file_url_for_a_local_zip() -> None:
    text = render("0.3.0-rc1", ZIP_SHA, url="file:///tmp/Sombra.zip")
    assert 'url "file:///tmp/Sombra.zip"' in text
    assert 'version "0.3.0-rc1"' in text


@pytest.mark.parametrize(
    ("version", "sha", "url"),
    [
        ("0.3.0", "abc", DEFAULT_URL),
        ("0.3.0", ZIP_SHA.upper(), DEFAULT_URL),
        ('0.3.0" do evil', ZIP_SHA, DEFAULT_URL),
        ("0.3.0", ZIP_SHA, 'file:///x"; system "rm'),
        ("0.3.0", ZIP_SHA, "file:///x\\"),
    ],
)
def test_render_refuses_values_that_break_the_ruby(version: str, sha: str, url: str) -> None:
    with pytest.raises(CaskError):
        render(version, sha, url=url)


def test_render_refuses_a_template_with_unknown_placeholders(tmp_path: Path) -> None:
    tmpl = tmp_path / "t.rb.tmpl"
    tmpl.write_text(TEMPLATE.read_text() + "# @@NEW_FIELD@@\n")
    with pytest.raises(CaskError, match="NEW_FIELD"):
        render("0.3.0", ZIP_SHA, template=tmpl)


# --- bumping the tap ---------------------------------------------------------------------


def _tap(tmp_path: Path, version: str | None) -> Path:
    tap = tmp_path / "tap"
    tap.mkdir()
    if version is not None:
        (tap / "Casks").mkdir()
        (tap / CASK_PATH).write_text(render(version, WHEEL_SHA))
    return tap


def test_bump_into_an_empty_tap_writes_the_cask_and_shows_the_whole_diff(tmp_path: Path) -> None:
    tap = _tap(tmp_path, None)
    changed, diff = bump(tap, render("0.3.0", ZIP_SHA), "0.3.0", dry_run=False)
    assert changed
    assert diff.startswith("--- a/Casks/sombra.rb\n+++ b/Casks/sombra.rb\n")
    assert '+  version "0.3.0"' in diff
    assert (tap / CASK_PATH).read_text() == render("0.3.0", ZIP_SHA)


def test_bump_upgrades_and_the_diff_shows_version_and_sha(tmp_path: Path) -> None:
    tap = _tap(tmp_path, "0.2.9")
    changed, diff = bump(tap, render("0.3.0", ZIP_SHA), "0.3.0", dry_run=False)
    assert changed
    assert '-  version "0.2.9"' in diff
    assert '+  version "0.3.0"' in diff
    assert f'+  sha256 "{ZIP_SHA}"' in diff
    assert tap_version((tap / CASK_PATH).read_text()) == "0.3.0"


def test_dry_run_prints_the_diff_and_writes_nothing(tmp_path: Path) -> None:
    tap = _tap(tmp_path, "0.2.9")
    before = (tap / CASK_PATH).read_text()
    changed, diff = bump(tap, render("0.3.0", ZIP_SHA), "0.3.0", dry_run=True)
    assert changed
    assert '+  version "0.3.0"' in diff
    assert (tap / CASK_PATH).read_text() == before


def test_same_cask_is_a_no_op(tmp_path: Path) -> None:
    tap = _tap(tmp_path, None)
    text = render("0.3.0", ZIP_SHA)
    bump(tap, text, "0.3.0", dry_run=False)
    changed, message = bump(tap, text, "0.3.0", dry_run=False)
    assert not changed
    assert "nothing to push" in message


def test_a_patch_to_an_older_line_never_downgrades_the_tap(tmp_path: Path) -> None:
    tap = _tap(tmp_path, "0.10.0")
    before = (tap / CASK_PATH).read_text()
    changed, message = bump(tap, render("0.9.1", ZIP_SHA), "0.9.1", dry_run=False)
    assert not changed
    assert "0.10.0, newer than 0.9.1" in message
    assert (tap / CASK_PATH).read_text() == before


def test_a_tap_cask_with_a_non_release_version_is_replaced(tmp_path: Path) -> None:
    tap = _tap(tmp_path, None)
    (tap / "Casks").mkdir()
    (tap / CASK_PATH).write_text('cask "sombra" do\n  version "0.3.0-rc1"\nend\n')
    changed, _ = bump(tap, render("0.3.0", ZIP_SHA), "0.3.0", dry_run=False)
    assert changed


def test_tap_version_reads_the_version_stanza() -> None:
    assert tap_version(render("1.2.3", ZIP_SHA)) == "1.2.3"
    assert tap_version("") is None


# --- the command line --------------------------------------------------------------------


@pytest.fixture
def sums(tmp_path: Path) -> Path:
    path = tmp_path / "SHA256SUMS"
    path.write_text(SUMS)
    return path


def test_cli_render_prints_the_cask(sums: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["render", "--version", "v0.3.0", "--sums", str(sums)]) == 0
    assert capsys.readouterr().out == render("0.3.0", ZIP_SHA)


def test_cli_render_writes_the_output_file(sums: Path, tmp_path: Path) -> None:
    out = tmp_path / "rendered" / "sombra.rb"
    assert main(["render", "--version", "0.3.0", "--sums", str(sums), "--output", str(out)]) == 0
    assert out.read_text() == render("0.3.0", ZIP_SHA)


def test_cli_render_hashes_a_local_zip_for_the_smoke_test(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    z = tmp_path / "Sombra.zip"
    z.write_bytes(b"app zip")
    url = f"file://{z}"
    argv = ["render", "--version", "v0.3.0-rc1", "--zip", str(z), "--url", url]
    assert main(argv) == 0
    assert capsys.readouterr().out == render("0.3.0-rc1", ZIP_SHA, url=url)


def test_cli_render_refuses_a_pre_release_for_the_release_url(
    sums: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["render", "--version", "v0.3.0-rc1", "--sums", str(sums)]) == 1
    assert "not a final release" in capsys.readouterr().err


def test_cli_render_fails_without_the_zip_in_sums(
    sums: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["render", "--version", "v0.4.0", "--sums", str(sums)]) == 1
    assert "no Sombra-0.4.0-macos-arm64.zip" in capsys.readouterr().err


def test_cli_render_fails_on_a_missing_sums_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["render", "--version", "v0.3.0", "--sums", str(tmp_path / "nope")]) == 1
    assert capsys.readouterr().err.startswith("cask: ")


def test_cli_bump_dry_run_prints_the_diff_and_the_output_line(
    sums: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tap = _tap(tmp_path, "0.2.9")
    gh_out = tmp_path / "gh_output"
    argv = ["bump", "--version", "v0.3.0", "--sums", str(sums), "--tap-dir", str(tap)]
    assert main([*argv, "--dry-run", "--github-output", str(gh_out)]) == 0
    out = capsys.readouterr().out
    assert '+  version "0.3.0"' in out
    assert out.endswith("changed=true\n")
    assert gh_out.read_text() == "changed=true\n"
    assert tap_version((tap / CASK_PATH).read_text()) == "0.2.9"


def test_cli_bump_writes_then_reports_unchanged(
    sums: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tap = _tap(tmp_path, None)
    argv = ["bump", "--version", "v0.3.0", "--sums", str(sums), "--tap-dir", str(tap)]
    assert main(argv) == 0
    assert (tap / CASK_PATH).read_text() == render("0.3.0", ZIP_SHA)
    capsys.readouterr()
    assert main(argv) == 0
    assert capsys.readouterr().out.endswith("nothing to push\nchanged=false\n")


def test_cli_bump_refuses_a_pre_release(
    sums: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tap = _tap(tmp_path, None)
    argv = ["bump", "--version", "v0.3.0-rc1", "--sums", str(sums), "--tap-dir", str(tap)]
    assert main(argv) == 1
    assert not (tap / CASK_PATH).exists()


def test_script_runs_standalone(sums: Path) -> None:
    # The workflow runs it as a plain script with no project installed.
    result = subprocess.run(  # noqa: S603  # fixed argv, no shell
        [
            shutil.which("python3") or "python3",
            str(REPO_ROOT / "packaging" / "homebrew" / "cask.py"),
            "render",
            "--version",
            "v0.3.0",
            "--sums",
            str(sums),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == render("0.3.0", ZIP_SHA)


# --- the CI helper and the tap bootstrap -------------------------------------------------


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_ci_sh_is_shellcheck_clean() -> None:
    result = subprocess.run(  # noqa: S603  # fixed argv, no shell
        ["shellcheck", "--shell=sh", str(REPO_ROOT / "packaging" / "homebrew" / "ci.sh")],  # noqa: S607  # found on PATH above
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("args", [[], ["audit"], ["smoke", "x.rb"], ["nope", "x.rb"]])
def test_ci_sh_usage_errors(args: list[str]) -> None:
    result = subprocess.run(  # noqa: S603  # fixed argv, no shell
        ["sh", str(REPO_ROOT / "packaging" / "homebrew" / "ci.sh"), *args],  # noqa: S607  # POSIX sh
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_tap_bootstrap_has_the_layout_homebrew_expects() -> None:
    tap = REPO_ROOT / "packaging" / "homebrew" / "tap"
    assert (tap / "README.md").is_file()
    assert (tap / "Casks").is_dir()
    readme = (tap / "README.md").read_text()
    assert "brew install --cask nickmaglowsch/sombra/sombra" in readme
