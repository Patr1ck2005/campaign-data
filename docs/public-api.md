# Public API and data-safety boundary

The package provides directory indexing, audit reports, duplicate detection,
merge, and split helpers for CSV and tab-separated text campaigns. Inputs and
outputs are explicit paths or file-like data supplied by the caller.

Source files are read-only from the library's perspective. Physical column
meaning, schema translation, registry integration, and output retention policy
belong to the caller. No simulator, project, or private dataset is referenced
by this package.
