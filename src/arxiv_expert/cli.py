"""Terminal-only setup and operational commands."""

from __future__ import annotations

import getpass
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

import typer

from .categories import Category, search_categories
from .config import Settings, default_paths, load_settings, save_settings
from .database import Database
from .openai_service import OpenAIService
from .runtime import database, sync_service
from .scheduler import SchedulerManager

app = typer.Typer(
    name="arxiv-expert",
    help="Build a persistent research expert from a self-updating arXiv topic.",
    no_args_is_help=True,
)


def _invoked_command() -> str:
    """Return the executable spelling that works in the caller's current shell."""
    invoked = sys.argv[0]
    if not invoked or invoked == "-m" or Path(invoked).name == "__main__.py":
        return f"{sys.executable} -m arxiv_expert"
    return invoked


def _prompt_category() -> Category:
    while True:
        query = typer.prompt("Search official arXiv topics", default="robotics")
        matches = search_categories(query)
        if not matches:
            typer.echo(
                "No official topics matched. Try a code such as cs.RO or a name such as robotics."
            )
            continue
        shown = matches[:20]
        for index, category in enumerate(shown, start=1):
            typer.echo(f"  {index:>2}. {category.label}")
        selection = typer.prompt("Select topic number", type=int)
        if 1 <= selection <= len(shown):
            return shown[selection - 1]
        typer.echo(f"Enter a number from 1 to {len(shown)}.")


@app.command()
def setup() -> None:
    """Configure the first expert and install daily background synchronization."""
    paths = default_paths()
    if paths.config_file.exists():
        raise typer.BadParameter(
            "arXiv Expert is already configured. Use the app to create another expert."
        )
    typer.echo("arXiv Expert setup (terminal only)\n")
    key = getpass.getpass("OpenAI API key: ").strip()
    settings = Settings.create(key)
    category = _prompt_category()
    expert_name = typer.prompt("Expert name", default=category.name).strip()
    if not expert_name:
        raise typer.BadParameter("Expert name cannot be empty.")

    backend = OpenAIService(settings)
    typer.echo("Creating the persistent paper library...")
    vector_store_id = backend.create_vector_store(expert_name, category.code)
    db = Database(paths.database_file)
    scheduler = SchedulerManager()
    expert = None
    try:
        db.initialize()
        expert = db.create_expert(expert_name, category.code, vector_store_id)
        save_settings(settings, paths)
        scheduler.install()
    except Exception:
        with suppress(Exception):
            scheduler.uninstall()
        if expert is not None:
            db.delete_expert(expert.id)
        paths.config_file.unlink(missing_ok=True)
        with suppress(Exception):
            backend.delete_vector_store(vector_store_id)
        raise
    typer.echo("\nSetup complete.")
    typer.echo(f"Run `{_invoked_command()} chat`, then open the printed local address.")
    typer.echo("New papers will synchronize daily while this computer is available.")


@app.command()
def sync(quiet: bool = typer.Option(False, "--quiet", help="Only write errors to stderr.")) -> None:
    """Fetch and index all papers published since each expert's last sync."""
    paths = default_paths()
    paths.ensure()
    service, arxiv = sync_service(paths)
    try:
        results = service.sync_all()
    except Exception as exc:
        with paths.sync_log.open("a", encoding="utf-8") as log:
            log.write(f"sync failed: {exc}\n")
        typer.echo(f"Sync failed: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        arxiv.close()
    lines = [
        (
            f"{item.expert_id}: skipped (sync already running)"
            if item.skipped
            else f"{item.expert_id}: discovered={item.discovered} ingested={item.ingested} failed={item.failed} retried={item.retried}"
        )
        for item in results
    ]
    with paths.sync_log.open("a", encoding="utf-8") as log:
        for line in lines:
            log.write(line + "\n")
    if not quiet:
        typer.echo("\n".join(lines) if lines else "No experts configured.")


@app.command()
def status() -> None:
    """Show each expert's topic, paper counts, and synchronization cursor."""
    db = database()
    experts = db.list_experts()
    if not experts:
        typer.echo("No experts configured.")
        return
    for expert in experts:
        counts = db.paper_counts(expert.id)
        synced = expert.last_sync_at.isoformat() if expert.last_sync_at else "not yet"
        typer.echo(
            f"{expert.name} [{expert.category}] — ready {counts['ready']}, "
            f"indexing {counts['indexing'] + counts['pending']}, failed {counts['failed']}, last sync {synced}"
        )


@app.command("logs")
def show_logs(lines: int = typer.Option(50, min=1, max=1000)) -> None:
    """Print the most recent background synchronization log lines."""
    target = default_paths().sync_log
    if not target.exists():
        typer.echo("No synchronization log exists yet.")
        return
    typer.echo("\n".join(target.read_text(encoding="utf-8").splitlines()[-lines:]))


@app.command("uninstall-scheduler")
def uninstall_scheduler() -> None:
    """Remove the daily background task without deleting experts or papers."""
    SchedulerManager().uninstall()
    typer.echo("Daily synchronization task removed.")


@app.command()
def chat() -> None:
    """Start the local chat server without automatically opening a browser."""
    load_settings()
    typer.echo("Checking for papers published since the last sync...")
    service, arxiv = sync_service()
    try:
        service.sync_all()
    except Exception as exc:
        typer.echo(f"Startup sync could not complete: {exc}", err=True)
        typer.echo("The saved library is still available.", err=True)
    finally:
        arxiv.close()
    script = Path(__file__).with_name("web.py")
    typer.echo("Starting arXiv Expert at http://localhost:8501")
    typer.echo("Open that address in your browser. Press Ctrl+C here to stop.\n")
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(script),
        "--server.headless=true",
        "--server.address=127.0.0.1",
        "--browser.gatherUsageStats=false",
    ]
    raise typer.Exit(subprocess.run(command, check=False).returncode)
