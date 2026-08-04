"""Install a silent daily user-level synchronization task on each desktop OS."""

from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import AppPaths, default_paths

TASK_NAME = "arxiv-expert-sync"


@dataclass(frozen=True, slots=True)
class ScheduledFile:
    path: Path
    content: bytes


@dataclass(frozen=True, slots=True)
class SchedulerPlan:
    platform: str
    files: tuple[ScheduledFile, ...]
    install_commands: tuple[tuple[str, ...], ...]
    uninstall_commands: tuple[tuple[str, ...], ...]


def _module_command(python_executable: str | None = None) -> list[str]:
    executable = python_executable or sys.executable
    return [executable, "-m", "arxiv_expert", "sync", "--quiet"]


def build_scheduler_plan(
    platform: str | None = None,
    *,
    paths: AppPaths | None = None,
    python_executable: str | None = None,
    home: Path | None = None,
) -> SchedulerPlan:
    current = platform or sys.platform
    resolved_paths = paths or default_paths()
    user_home = home or Path.home()
    command = _module_command(python_executable)

    if current.startswith("linux"):
        unit_dir = user_home / ".config" / "systemd" / "user"
        service = unit_dir / f"{TASK_NAME}.service"
        timer = unit_dir / f"{TASK_NAME}.timer"
        exec_start = " ".join(shlex.quote(part) for part in command)
        service_content = (
            "[Unit]\nDescription=Ingest new papers for arXiv Expert\n\n"
            "[Service]\nType=oneshot\n"
            f"ExecStart={exec_start}\n"
            f"StandardOutput=append:{resolved_paths.sync_log}\n"
            f"StandardError=append:{resolved_paths.sync_log}\n"
        ).encode()
        timer_content = (
            b"[Unit]\nDescription=Daily arXiv Expert paper sync\n\n"
            b"[Timer]\nOnCalendar=daily\nPersistent=true\nRandomizedDelaySec=15m\n\n"
            b"[Install]\nWantedBy=timers.target\n"
        )
        return SchedulerPlan(
            "linux",
            (ScheduledFile(service, service_content), ScheduledFile(timer, timer_content)),
            (
                ("systemctl", "--user", "daemon-reload"),
                ("systemctl", "--user", "enable", "--now", f"{TASK_NAME}.timer"),
            ),
            (
                ("systemctl", "--user", "disable", "--now", f"{TASK_NAME}.timer"),
                ("systemctl", "--user", "daemon-reload"),
            ),
        )

    if current == "darwin":
        target = user_home / "Library" / "LaunchAgents" / "org.arxiv-expert.sync.plist"
        payload = {
            "Label": "org.arxiv-expert.sync",
            "ProgramArguments": command,
            "StartCalendarInterval": {"Hour": 3, "Minute": 0},
            "RunAtLoad": True,
            "StandardOutPath": str(resolved_paths.sync_log),
            "StandardErrorPath": str(resolved_paths.sync_log),
        }
        return SchedulerPlan(
            "macos",
            (ScheduledFile(target, plistlib.dumps(payload)),),
            (("launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)),),
            (("launchctl", "bootout", f"gui/{os.getuid()}", str(target)),),
        )

    if current.startswith("win"):
        executable = Path(command[0])
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.exists():
            command[0] = str(pythonw)
        task_command = subprocess.list2cmdline(command)
        return SchedulerPlan(
            "windows",
            (),
            (
                (
                    "schtasks.exe",
                    "/Create",
                    "/F",
                    "/SC",
                    "DAILY",
                    "/ST",
                    "03:00",
                    "/TN",
                    TASK_NAME,
                    "/TR",
                    task_command,
                ),
            ),
            (("schtasks.exe", "/Delete", "/F", "/TN", TASK_NAME),),
        )

    raise RuntimeError(f"Automatic scheduling is not supported on platform {current!r}.")


class SchedulerManager:
    def __init__(self, plan: SchedulerPlan | None = None):
        self.plan = plan or build_scheduler_plan()

    def install(self) -> None:
        for scheduled_file in self.plan.files:
            scheduled_file.path.parent.mkdir(parents=True, exist_ok=True)
            scheduled_file.path.write_bytes(scheduled_file.content)
        self._run(self.plan.install_commands)

    def uninstall(self) -> None:
        self._run(self.plan.uninstall_commands, tolerate_failure=True)
        for scheduled_file in self.plan.files:
            scheduled_file.path.unlink(missing_ok=True)

    @staticmethod
    def _run(commands: Sequence[Sequence[str]], tolerate_failure: bool = False) -> None:
        for command in commands:
            try:
                subprocess.run(command, check=True, capture_output=True, text=True)
            except (OSError, subprocess.CalledProcessError):
                if not tolerate_failure:
                    raise
