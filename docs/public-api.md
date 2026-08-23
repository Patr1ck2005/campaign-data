# Public API and data-safety boundary

The package provides directory indexing, audit reports, duplicate detection,
merge, and split helpers for CSV and tab-separated text campaigns. Inputs and
outputs are explicit paths or file-like data supplied by the caller.

## Single-file inspection

- `campaign_data.read_table(path, max_rows=None) -> pandas.DataFrame` —
  one-call reader for `.txt` TSVs and batch-export `.csv`s (skips the `%`
  metadata preamble, strips the header `%` prefix). Numeric columns are
  converted; complex/text cells stay str. Requires the optional extra:
  `pip install campaign-data[pandas]`.
- `campaign_data.peek_table(path, max_values=8) -> str` — stdlib-only
  structure summary (shape, per-column kind, distinct counts, sample
  values). Also exposed as `python -m campaign_data peek <file>`.
- Subcommand ladder: `peek` (one file) → `audit` (directory) → `merge/split`.

Source files are read-only from the library's perspective. Physical column
meaning, schema translation, registry integration, and output retention policy
belong to the caller. No simulator, project, or private dataset is referenced
by this package.
