# GTD Engine

GTD Engine is a local task record for a person working with an AI assistant. It keeps captures, actions, projects, dependencies, and decisions in SQLite so the next conversation can use the same current state.

A capture is something that needs thought. Clarifying it decides whether it becomes an action or project. `next` only shows open actions whose dependencies are satisfied. The app makes no network calls and never publishes anything.

## Set up once

You need Python 3.12 or later. Install the app and initialize a local data directory.

```sh
git clone https://github.com/kn0wsnothing/gtd-engine-showcase.git
cd gtd-engine-showcase
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/gtd init
```

The default data directory is `~/.local/share/gtd-engine/`. It holds `gtd.db` and `history.jsonl`. Set `GTD_HOME` for another data directory or `GTD_DB` for a specific database file. `init` creates or migrates the database; it does not erase it.

## Work with an assistant

Use a coding assistant with local terminal and filesystem access, such as Codex or Claude Code. Give it this repository and [AGENT_GUIDE.md](AGENT_GUIDE.md). A chat only assistant cannot run the local command or inspect the local database.

Paste this prompt to set it up:

> Clone https://github.com/kn0wsnothing/gtd-engine-showcase.git, install it in an isolated Python environment, initialize the local GTD Engine, and read `AGENT_GUIDE.md`. Do not create tasks yet. Tell me the data location and ask what I want to capture.

Your task text and local state may be sent to whichever AI provider you choose if your assistant reads them. Review that provider’s data policy before granting file access. GTD Engine itself has no AI integration, account, or plugin.

After setup, ask: “Read the next actions in my local GTD Engine. Do not change anything. Explain which are blocked and why.” An explicit request to capture, complete, edit, or defer a named item authorizes that bounded command. The guide names the cases where the assistant should ask because the requested decision is unclear.

## What this app covers

It supports local capture, clarification, actions, projects, dependencies, completion, corrections, and history. SQLite is authoritative. If the separate history append fails after a database mutation, the command succeeds and prints a warning: the change is saved, so do not repeat it.

The public app does not include accounts, calendar input, note projection, automation queues, backups, or external integrations.

## License

MIT Copyright 2026 John. See [LICENSE](LICENSE) and [CONTRIBUTING.md](CONTRIBUTING.md).
