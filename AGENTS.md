# CampaignData Agent Instructions

## Mission

Provide reusable CSV/TXT campaign indexing, audit, deduplication, merge, and
split primitives without assuming a simulator, dataframe schema, registry, or
project output layout.

## Single-file inspection ladder

For "what's in this file?" questions prefer the cheap entry points over
hand-rolled parsing: `python -m campaign_data peek <file>` (stdlib summary)
or `campaign_data.read_table(path)` (pandas DataFrame, `pandas` extra).
Then `audit` for directory relationships, then `merge`/`split` for writes.

## Boundaries

- Inputs are explicit files/directories and column names or indices.
- Never modify source data.
- Cache files are disposable diagnostics and must be isolated from raw data
  ownership decisions made by consumers.
- Do not import consumer applications. pandas is an optional extra behind
  lazy import in `read_table`; the core package stays stdlib-only.
- Keep Chinese and English schemas in consumer adapters.
- Stable consumers install a wheel; source path injection is test-only.

## Workflow

1. Characterize behavior with a real or synthetic campaign case.
2. Change the shared implementation and tests here.
3. Update caller compatibility adapters.
4. Verify runtime provenance from installed wheels.
5. Commit this repository before consumer repositories.
