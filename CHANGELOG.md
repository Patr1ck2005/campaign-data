# Changelog

## 0.2.0

- Added `read_table(path, max_rows=None) -> pandas.DataFrame`: one-call reader
  for TSV and COMSOL batch-export CSVs (handles the `%` metadata preamble and
  header prefix; numeric columns converted, complex/text columns kept as str).
  Requires the new `pandas` extra: `pip install campaign-data[pandas]`.
- Added `peek_table(path, max_values=8) -> str` (stdlib-only) and a matching
  `python -m campaign_data peek <file>` subcommand for one-file structure
  summaries, filling the gap between `head` and a full audit.
- Exported both from the package top level.

## 0.1.0

- Published recursive CSV/TXT indexing, audit, deduplication, merge, and split
  primitives.

Earlier internal development history is intentionally not part of the public
release branch.
