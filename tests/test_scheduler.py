from pathlib import Path

from arxiv_expert.scheduler import build_scheduler_plan


def test_linux_plan_is_daily_persistent_and_uses_exact_python(paths, tmp_path):
    plan = build_scheduler_plan(
        "linux", paths=paths, python_executable="/opt/app/bin/python", home=tmp_path
    )
    rendered = b"\n".join(item.content for item in plan.files).decode()
    assert "OnCalendar=daily" in rendered
    assert "Persistent=true" in rendered
    assert "ExecStart=/opt/app/bin/python -m arxiv_expert sync --quiet" in rendered
    assert plan.install_commands[-1][-1] == "arxiv-expert-sync.timer"


def test_macos_plan_uses_launch_agent(paths, tmp_path):
    plan = build_scheduler_plan(
        "darwin", paths=paths, python_executable="/opt/app/python", home=tmp_path
    )
    assert plan.platform == "macos"
    assert "Library/LaunchAgents" in str(plan.files[0].path)
    assert b"StartCalendarInterval" in plan.files[0].content


def test_windows_plan_uses_hidden_python_when_available(monkeypatch, paths, tmp_path):
    monkeypatch.setattr(Path, "exists", lambda self: self.name == "pythonw.exe")
    plan = build_scheduler_plan(
        "win32", paths=paths, python_executable=r"C:\App\python.exe", home=tmp_path
    )
    command = plan.install_commands[0]
    assert command[0] == "schtasks.exe"
    assert "/SC" in command and "DAILY" in command
    assert "pythonw.exe" in command[-1]
