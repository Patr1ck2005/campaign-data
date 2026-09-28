# campaign-data

<p align="center"><img src="docs/assets/portfolio/scientific-tools-v1.png" width="760" alt="Four glass modules linked by light: the shared scientific-tooling collection" /></p>

*Concept illustration for the shared scientific-tooling collection; not a computed result. [Artwork provenance](docs/assets/portfolio/manifest.json).*

For tasks in the personal research system, read the [canonical MyPhysics entry](D:/Obsidian/MyPhysics/System/README.md) and follow its pointers to the owning scientific project. This does not change the domain-neutral package contract. [Local Agent rules](AGENTS.md); [shared-library ownership](D:/Dev/Projects/Work/research-agent-workbench/docs/shared-libraries/shared-library-catalog.md).

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
