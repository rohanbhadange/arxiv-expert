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
- Persistent, named conversations with isolated chat histories.
- An optional focus of up to eight papers per conversation, with stable **F1–F8** citations.
- Explicit in-chat imports for any arXiv paper, regardless of the expert's tracked topic.
- Clickable **Library** and **Web** citations.

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

1. Start a named conversation or reopen an existing one from the sidebar.
2. Use **Analyze** to ask what papers found, compared, or concluded.
3. Use **Experiment Designer** to turn the library into hypotheses, baselines,
   ablations, measurements, and follow-up experiments.
4. Follow **Library** citations for ingested papers and **Web** citations for
   relevant work that is not in the saved corpus.
5. Open the **Library** tab to inspect paper status and whether it arrived from
   the tracked **Topic** or a **Manual** import.

Conversations, their focused papers, and the global paper library persist between
sessions. Deleting a conversation does not delete its papers. Stop the local
server with `Ctrl+C`; daily ingestion continues through the installed background
task.

### Focus a conversation on specific papers

Use **Focus in this chat** in the Library tab, or type one of these messages:

```text
Import arXiv:2401.12345
Bring in https://arxiv.org/abs/2401.12345
Add the paper "DreamerV3"
Focus on Learning to Move
Remove arXiv:2401.12345 from this chat
```

An exact arXiv ID or link imports immediately. A title search shows up to five
official arXiv matches and waits for confirmation. The paper is indexed once in
the expert's global library, then reused by any conversation without another
upload.

With focused papers, every answer receives their metadata and abstracts, and
full-text retrieval is restricted to only those papers. The assistant does not
silently broaden the evidence. For a single turn, say **search the full library**
or explicitly ask it to **search the web**. With no focused papers, the full saved
library remains the default.

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
- A manually requested paper may come from any arXiv category and is saved once
  in the same expert-wide library.
- Each conversation can focus on at most eight saved papers.

Existing installations migrate automatically on the next run. Existing papers,
citations, timestamps, and chat messages are preserved in a **Previous chat**
conversation.

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
