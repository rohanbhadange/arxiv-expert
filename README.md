# arXiv Expert

arXiv Expert turns one official arXiv category into a persistent research
assistant. It starts collecting papers when an expert is created, indexes every
new primary and cross-listed paper once, and keeps the library current through a
daily background task. Chat answers use the permanent library first and fall
back to cited web research when the requested paper is not present.

## What users get

- Terminal-only setup; no setup windows or browser popups.
- A local browser chat with **Analyze** and **Experiment Designer** modes.
- One isolated, full-text paper library per official arXiv topic.
- Daily unattended ingestion plus catch-up synchronization after downtime.
- Clickable **Library** and **Web** citations.
- Persistent paper status and chat history across restarts.

The application sends public arXiv PDFs and chat requests to OpenAI using the
user's own API key. It stores the key in a user-only local configuration file.

## Install

Python 3.10 or newer is required.

### Linux and macOS

```bash
git clone https://github.com/rohanbhadange/arxiv-expert.git
cd arxiv-expert
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/arxiv-expert setup
```

### Windows PowerShell

```powershell
git clone https://github.com/rohanbhadange/arxiv-expert.git
cd arxiv-expert
py -m venv .venv
.venv\Scripts\python.exe -m pip install .
.venv\Scripts\arxiv-expert.exe setup
```

Setup asks for the API key with masked terminal input, lets the user search the
bundled official arXiv taxonomy, creates the first expert, and installs the
platform's user-level daily task. It does not open another window.

## Use it

Start the expert from the repository directory. Use the executable inside the
virtual environment so activation is not required.

Linux and macOS:

```bash
.venv/bin/arxiv-expert chat
```

Windows PowerShell:

```powershell
.venv\Scripts\arxiv-expert.exe chat
```

The command performs a catch-up sync, starts the local server, and prints
`http://localhost:8501`. It deliberately does not open a browser automatically
and binds only to the local computer.

Open the printed address and:

1. Use **Analyze** to ask what papers found, compared, or concluded.
2. Use **Experiment Designer** to turn the library into hypotheses, baselines,
   ablations, measurements, and follow-up experiments.
3. Follow **Library** citations for ingested papers and **Web** citations for
   relevant work that is not in the saved corpus.
4. Open the **Library** tab to inspect paper titles and indexing status.

Chats and the paper library persist between sessions. Stop the local server
with `Ctrl+C`; daily ingestion continues through the installed background task.

## Operational commands

```bash
.venv/bin/arxiv-expert status
.venv/bin/arxiv-expert sync
.venv/bin/arxiv-expert logs
.venv/bin/arxiv-expert uninstall-scheduler
```

- Linux uses a persistent systemd user timer.
- macOS uses a per-user LaunchAgent.
- Windows uses a per-user hidden Task Scheduler job.

The computer must be powered on for work to run. The stored UTC cursor ensures
the next scheduled run or chat startup catches up every missed day.

## Exact paper policy

- An expert tracks exactly one official category.
- Tracking begins at the expert's UTC creation time; there is no historical seed.
- Primary-category papers and cross-listed papers are both included.
- The version-free arXiv ID is the permanent identity.
- Once a paper is indexed, later revisions are ignored.
- Failed downloads and indexing jobs retry during every sync.
- Web-discovered papers support the current answer but are never added silently.

## Development

```bash
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/coverage run -m pytest
.venv/bin/coverage report
```

The test suite uses local fakes for OpenAI and arXiv. A real API key is needed
only for the optional live smoke test:

```bash
.venv/bin/python scripts/live_smoke.py
```

The smoke test imports one recent robotics paper, performs full-text retrieval,
generates library-cited analysis and a web-cited experiment design, verifies chat
history, and deletes its temporary OpenAI file and vector store. It uses billable
API operations.

See [SECURITY.md](SECURITY.md) before changing credential storage, networking,
deletion, or background scheduling.
