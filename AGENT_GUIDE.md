# GTD Engine agent guide

Use the installed `gtd` command. Do not edit the SQLite database, `history.jsonl`, or generated files directly. Run commands from any directory after the human has installed the package and chosen its data location.

## State and boundaries

`GTD_HOME` defaults to `~/.local/share/gtd-engine`; it contains `gtd.db` and `history.jsonl`. `GTD_DB` can select one database file. Pass `--json` before a command when you need structured output. Exit `0` means success and exit `2` means invalid input, missing records, or an invalid transition. A successful mutation with a history warning is already saved in SQLite; report the warning and do not repeat the mutation.

Read state before proposing a mutation:

```sh
gtd --json inbox
gtd --json next
gtd --json blocked
gtd --json show action ID
gtd --json project list
gtd --json project actions ID
```

Use `capture TEXT [--provenance TEXT]` for unprocessed input. Do not turn a capture into an action or project unless the human asks. `clarify ID --as action --text TEXT [--project ID] [--reason TEXT]` and `clarify ID --as project --text TEXT [--reason TEXT]` make that decision. `clarify ID --as nothing --reason TEXT` records a decision that no commitment is needed.

## Supported mutations

Create work only at the human’s request:

```sh
gtd action add TEXT [--project ID] [--after ACTION_ID] [--type obligation|intention]
gtd project add OUTCOME [--done-test TEXT]
```

`--after` makes the new action wait for another action. Inspect `blocked` before claiming it is next.

For an explicit human decision, use these commands and state the result back:

```sh
gtd done ACTION_ID
gtd action drop ACTION_ID --reason TEXT
gtd action reopen ACTION_ID
gtd action someday ACTION_ID
gtd action reactivate ACTION_ID
gtd action defer ACTION_ID --until YYYY-MM-DD
gtd action undelay ACTION_ID
gtd action edit ACTION_ID --text TEXT --reason TEXT [--force]
gtd project close PROJECT_ID --state done|dropped
```

`drop`, `edit`, and `project close` are material state changes. Never choose them yourself. `--force` is only for a stated human reason when a recent edit changes the leading verb. `project close` refuses while it has open or someday child actions; list them with `project actions ID`, then ask the human whether to complete, drop, or reassign each one.

`defer` adds a date block. `undelay` removes that action’s date blocks. Both are in history. `park` is an alias for `someday`.

## Useful prompts for the human

- “Read my GTD Engine inbox and next actions. Do not make changes. Group captures that need a decision and explain blocked actions.”
- “Capture these notes with provenance `meeting`: … Do not clarify them.”
- “I decide that capture 12 is an action, ‘Send the revised proposal’, under project 3. Clarify it and show the resulting next or blocked state.”
- “Mark action 9 done. Tell me which dependent actions became available.”
- “I no longer want action 15. Drop it with this reason: ‘The event was cancelled.’ Then show its project actions.”
- “Show project 4’s unfinished actions. Do not close the project until I decide how to handle each one.”

## Human approval boundary

An assistant may read state, explain it, and capture text the human explicitly provides. An explicit request to clarify, add, complete, edit, defer, or close a named record authorizes that bounded command; do not ask again for each command in that request. Ask when the request leaves a new commitment, a reason, a project assignment, or a project child disposition unclear. Do not make destructive bulk changes, delete database files, bypass command validation, or invent reasons or approvals.
