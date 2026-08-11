# campaign-data

`campaign-data` audits, merges, deduplicates, and splits large collections of
CSV and tab-separated TXT files. It does not assume a particular simulator,
project, or language for column names.

The library treats the first `header_len` columns as campaign/grid parameters.
The boundary can be supplied explicitly or detected from the data. Physical
column meaning, DatasetRegistry integration, and output ownership remain in
consumer adapters.

```python
from campaign_data import audit_directory, smart_merge

report = audit_directory("data", recursive=True)
smart_merge(["data"], out_path="merged.txt", recursive=True)
```

The CLI exposes the same audit-first workflow:

```powershell
campaign-data audit data --recursive
campaign-data merge data --recursive --out merged.txt
```

Raw input files are never modified. Merge and split operations write only to
the explicit output path supplied by the caller.

See [docs/public-api.md](docs/public-api.md) for the supported operations and
data-safety contract.
