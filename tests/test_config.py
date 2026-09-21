"""Unit tests for the stdlib .env parser (lib/config.py)."""

import config


def test_load_env_file_parses_pairs(tmp_path):
    f = tmp_path / ".env"
    f.write_text(
        "# a comment\n"
        "A=1\n"
        'B = "two"\n'
        "C='three'\n"
        "\n"
        "D=\n"
        "E=has=equals\n",
        encoding="utf-8",
    )
    d = config._load_env_file(f)
    assert d["A"] == "1"
    assert d["B"] == "two"          # quotes stripped
    assert d["C"] == "three"
    assert d["D"] == ""             # empty value kept
    assert d["E"] == "has=equals"   # only split on the first '='
    assert "comment" not in " ".join(d.keys())


def test_load_env_file_missing_returns_empty(tmp_path):
    assert config._load_env_file(tmp_path / "does-not-exist.env") == {}


def test_settings_has_expected_shape():
    # Sanity: the frozen settings object exposes the documented knobs.
    for attr in ("winappdriver_url", "unicorn_process", "column_dialog_title",
                 "import_access_scope", "exports_dir", "watch_dir",
                 "benchling_url", "benchling_api_key", "benchling_entity_schema"):
        assert hasattr(config.settings, attr)
