import os
import stat

from arxiv_expert.config import Settings, default_paths, load_settings, save_settings


def test_settings_round_trip_and_stable_safety_identifier(paths):
    settings = Settings.create("sk-test-value")
    target = save_settings(settings, paths)
    loaded = load_settings(paths)
    assert loaded == settings
    assert len(loaded.safety_identifier) == 64
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert stat.S_IMODE(paths.config_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(paths.data_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(paths.log_dir.stat().st_mode) == 0o700


def test_home_override(monkeypatch, tmp_path):
    monkeypatch.setenv("ARXIV_EXPERT_HOME", str(tmp_path))
    assert default_paths().database_file == tmp_path / "data" / "arxiv-expert.sqlite3"
