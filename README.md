# GTD Engine showcase

This is a small, runnable engineering sample from the private GTD Engine. It demonstrates the
real SQLite schema, transaction helper, capture-to-clarify transition, typed `done` dependency,
completion propagation, and query-defined next actions.

```sh
python gtd_showcase.py run
python gtd_showcase.py invalid-state
python tests/test_demo.py
```

`run` creates a temporary SQLite database, captures an item, clarifies it into an action, adds a
second action blocked on the first, then completes the first action. The JSON result proves that
the dependent action was hidden before completion and became next afterward. `invalid-state`
proves that the engine rejects a value outside its closed commitment vocabulary.

The exported `gtd/` core files start as committed private source blobs. `SOURCE_PROVENANCE.json`
records their input hash, output hash, and declared transformations. The export
generifies named operational terms in comments and messages; it does not change database or state
transition logic. `gtd/config.py`, `gtd/journal.py`, and `gtd/morning.py` are deliberate synthetic replacements:
they remove paths, integrations, and persistent journal writes. The journal adapter records
calls in process memory and has a test-only failure switch, so journal errors still surface
after the database transition. `gtd_showcase.py` is a thin adapter that supplies fixed
synthetic inputs. These replacements are listed in
`showcase/export-manifest.json` in the private source.

The wrapper is distinct from the full private CLI. The showcase does not include its private command surface, projection, calendar sync,
automation queue, backups, operating configuration, or user data. It does not make network calls
and it deletes its temporary database before exit.

## License and maintenance

MIT Copyright 2026 John. This repository is maintained by John and does not accept external
contributions. Forks and reuse are welcome under the license.
