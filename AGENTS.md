# CampaignData Agent Instructions

## Mission

Provide reusable CSV/TXT campaign indexing, audit, deduplication, merge, and
split primitives without assuming a simulator, dataframe schema, registry, or
project output layout.

## Boundaries

- Inputs are explicit files/directories and column names or indices.
- Never modify source data.
- Cache files are disposable diagnostics and must be isolated from raw data
  ownership decisions made by consumers.
- Do not import consumer applications.
- Keep Chinese and English schemas in consumer adapters.
- Stable consumers install a wheel; source path injection is test-only.

## Workflow

1. Characterize behavior with a real or synthetic campaign case.
2. Change the shared implementation and tests here.
3. Update caller compatibility adapters.
4. Verify runtime provenance from installed wheels.
5. Commit this repository before consumer repositories.
