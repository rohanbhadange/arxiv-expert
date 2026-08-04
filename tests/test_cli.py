from types import SimpleNamespace

from typer.testing import CliRunner

from arxiv_expert import cli
from arxiv_expert.categories import require_category
from arxiv_expert.config import load_settings
from arxiv_expert.database import Database


class FakeOpenAIService:
    deleted = []

    def __init__(self, settings):
        self.settings = settings

    def create_vector_store(self, name, category):
        return "vs_setup_flow"

    def delete_vector_store(self, vector_store_id):
        self.deleted.append(vector_store_id)


class FakeScheduler:
    installed = 0
    uninstalled = 0

    def install(self):
        self.__class__.installed += 1

    def uninstall(self):
        self.__class__.uninstalled += 1


def test_terminal_setup_creates_expert_config_and_scheduler(monkeypatch, paths):
    monkeypatch.setattr(cli, "default_paths", lambda: paths)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "sk-user-owned")
    monkeypatch.setattr(cli, "_prompt_category", lambda: require_category("cs.RO"))
    monkeypatch.setattr(cli, "OpenAIService", FakeOpenAIService)
    monkeypatch.setattr(cli, "SchedulerManager", FakeScheduler)
    FakeScheduler.installed = 0

    result = CliRunner().invoke(cli.app, ["setup"], input="My Robotics Expert\n")

    assert result.exit_code == 0, result.output
    assert "Setup complete" in result.output
    assert " chat`" in result.output
    assert FakeScheduler.installed == 1
    assert load_settings(paths).openai_api_key == "sk-user-owned"
    experts = Database(paths.database_file).list_experts()
    assert [(item.name, item.category) for item in experts] == [("My Robotics Expert", "cs.RO")]


def test_setup_rolls_back_remote_and_local_state_when_scheduler_fails(monkeypatch, paths):
    class FailingScheduler(FakeScheduler):
        def install(self):
            raise RuntimeError("scheduler unavailable")

    FakeOpenAIService.deleted = []
    monkeypatch.setattr(cli, "default_paths", lambda: paths)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "sk-user-owned")
    monkeypatch.setattr(cli, "_prompt_category", lambda: require_category("cs.RO"))
    monkeypatch.setattr(cli, "OpenAIService", FakeOpenAIService)
    monkeypatch.setattr(cli, "SchedulerManager", FailingScheduler)

    result = CliRunner().invoke(cli.app, ["setup"], input="Robotics\n")

    assert result.exit_code != 0
    assert not paths.config_file.exists()
    assert Database(paths.database_file).list_experts() == []
    assert FakeOpenAIService.deleted == ["vs_setup_flow"]


def test_chat_server_binds_only_to_loopback(monkeypatch):
    class FakeSync:
        def sync_all(self):
            return []

    class FakeArxiv:
        def close(self):
            pass

    captured = []
    monkeypatch.setattr(cli, "load_settings", lambda: None)
    monkeypatch.setattr(cli, "sync_service", lambda: (FakeSync(), FakeArxiv()))
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **kwargs: captured.append(command) or SimpleNamespace(returncode=0),
    )

    result = CliRunner().invoke(cli.app, ["chat"])

    assert result.exit_code == 0, result.output
    assert "--server.address=127.0.0.1" in captured[0]


def test_module_invocation_prints_a_reusable_command(monkeypatch):
    monkeypatch.setattr(cli.sys, "argv", ["/tmp/arxiv_expert/__main__.py", "setup"])
    monkeypatch.setattr(cli.sys, "executable", "/usr/bin/python3")

    assert cli._invoked_command() == "/usr/bin/python3 -m arxiv_expert"
