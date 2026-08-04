from pathlib import Path

from streamlit.testing.v1 import AppTest

from arxiv_expert.config import Settings, save_settings
from arxiv_expert.database import Database


def test_configured_user_sees_expert_chat_and_library(monkeypatch, paths, created_at):
    monkeypatch.setenv("ARXIV_EXPERT_HOME", str(paths.config_dir.parent))
    save_settings(Settings.create("sk-test"), paths)
    db = Database(paths.database_file)
    db.initialize()
    db.create_expert("Robotics", "cs.RO", "vs_ui", created_at=created_at)
    script = Path(__file__).parents[1] / "src" / "arxiv_expert" / "web.py"

    app = AppTest.from_file(str(script)).run(timeout=15)

    assert not app.exception
    assert "Robotics" in [item.value for item in app.title]
    assert [tab.label for tab in app.tabs] == ["Chat", "Library"]
    assert any("no historical seed" in item.value for item in app.info)
