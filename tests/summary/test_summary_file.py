from pathlib import Path

from sombra.summary.summary_file import (
    EpochSection,
    append_epoch,
    read_current_epoch,
    read_epochs,
    read_minutes_section,
    write_minutes_section,
)


def test_layout_epochs_then_minutes(tmp_path: Path) -> None:
    path = tmp_path / "summary.md"
    append_epoch(path, EpochSection(1, "14:25:00", "### Tópicos\n- roadmap"))
    append_epoch(path, EpochSection(2, "14:50:00", "### Tópicos\n- roadmap\n- checkout"))
    write_minutes_section(path, "### Resumo\n\nata")
    assert path.read_text(encoding="utf-8") == (
        "# Resumo da reunião\n"
        "\n"
        "## Época 1 · até 14:25:00\n"
        "### Tópicos\n- roadmap\n"
        "\n"
        "## Época 2 · até 14:50:00\n"
        "### Tópicos\n- roadmap\n- checkout\n"
        "\n"
        "## Ata\n"
        "\n"
        "### Resumo\n\nata\n"
    )


def test_read_current_epoch_is_the_last(tmp_path: Path) -> None:
    path = tmp_path / "summary.md"
    assert read_current_epoch(path) is None
    assert read_epochs(path) == []
    append_epoch(path, EpochSection(1, "14:25:00", "um"))
    append_epoch(path, EpochSection(2, "14:50:00", "dois\n\ncom parágrafos"))
    write_minutes_section(path, "ata")
    assert read_current_epoch(path) == EpochSection(2, "14:50:00", "dois\n\ncom parágrafos")
    assert [e.epoch for e in read_epochs(path)] == [1, 2]


def test_appending_epoch_never_changes_earlier_bytes(tmp_path: Path) -> None:
    path = tmp_path / "summary.md"
    append_epoch(path, EpochSection(1, "14:25:00", "um"))
    before = path.read_text(encoding="utf-8")
    append_epoch(path, EpochSection(2, "14:50:00", "dois"))
    assert path.read_text(encoding="utf-8").startswith(before)


def test_regenerating_minutes_replaces_only_the_ata(tmp_path: Path) -> None:
    path = tmp_path / "summary.md"
    append_epoch(path, EpochSection(1, "14:25:00", "um"))
    write_minutes_section(path, "primeira ata")
    write_minutes_section(path, "segunda ata")
    text = path.read_text(encoding="utf-8")
    assert text.count("## Ata") == 1
    assert "primeira" not in text
    assert read_minutes_section(path) == "segunda ata"
    assert read_current_epoch(path) == EpochSection(1, "14:25:00", "um")


def test_epoch_after_minutes_goes_before_ata(tmp_path: Path) -> None:
    path = tmp_path / "summary.md"
    write_minutes_section(path, "ata")
    append_epoch(path, EpochSection(1, "14:25:00", "um"))
    text = path.read_text(encoding="utf-8")
    assert text.index("## Época 1") < text.index("## Ata")
    assert text.rstrip().endswith("ata")


def test_model_headings_are_demoted(tmp_path: Path) -> None:
    path = tmp_path / "summary.md"
    append_epoch(path, EpochSection(1, "14:25:00", "## Ata\n# Título\n### ok"))
    epochs = read_epochs(path)
    assert epochs[0].text == "### Ata\n### Título\n### ok"
    assert read_minutes_section(path) is None


def test_read_minutes_missing_file(tmp_path: Path) -> None:
    assert read_minutes_section(tmp_path / "summary.md") is None
