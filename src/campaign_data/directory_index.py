"""Directory-level CSV index for parameter-scan datasets.

This module turns a folder full of CSV/TSV files into a compact, serializable
summary that can be consumed by a web backend, notebook, or CLI preview.
It reuses the existing grid-analysis layer instead of re-implementing any CSV
parsing or grid detection logic.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from campaign_data.grid_analysis import (
    analyse_file, find_overlap_region,
    grid_report_to_dict, grid_report_from_dict,
)
from campaign_data.io_utils import HEADER_LEN, collect_paths, detect_header_len

# In-memory cache.  Key: (resolved_source, header_len).  Max 16 entries.
_INDEX_CACHE: dict[tuple, tuple[float, DirectoryIndex]] = {}
_CACHE_MAX = 16


def _sortable(value: Any):
    """Return a stable sort key for mixed values."""
    return (isinstance(value, str), value)


def _jsonable(value: Any):
    """Convert common Python / NumPy-like values into JSON-friendly objects."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    try:
        import numpy as np

        if isinstance(value, np.generic):
            return value.item()
    except Exception:
        pass
    return value


@dataclass(frozen=True)
class DirectoryIndex:
    """Serializable summary of one CSV folder."""

    root: Path
    header_len: int
    file_count: int
    total_rows: int
    axis_catalog: list[dict[str, Any]]
    file_summaries: list[dict[str, Any]]
    groups: list[dict[str, Any]]
    overlap_count: int
    overlap_samples: list[dict[str, Any]]
    issues: list[str]
    cache_hits: int = 0
    cache_misses: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "header_len": self.header_len,
            "file_count": self.file_count,
            "total_rows": self.total_rows,
            "axis_catalog": _jsonable(self.axis_catalog),
            "file_summaries": _jsonable(self.file_summaries),
            "groups": _jsonable(self.groups),
            "overlap_count": self.overlap_count,
            "overlap_samples": _jsonable(self.overlap_samples),
            "issues": list(self.issues),
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
        }


def _axis_summary(reports, axis_idx, header, *, axis_type="varying"):
    values = set()
    varying_files: list[str] = []
    constant_files: list[str] = []

    for rep in reports:
        rel_name = rep.path.name
        if axis_idx in rep.varying:
            values.update(rep.varying[axis_idx])
            varying_files.append(rel_name)
        elif axis_idx in rep.constant:
            values.add(rep.constant[axis_idx])
            constant_files.append(rel_name)

    sorted_values = sorted(values, key=_sortable)
    return {
        "index": axis_idx,
        "name": header[axis_idx],
        "type": axis_type,
        "unique_count": len(sorted_values),
        "min": sorted_values[0] if sorted_values else None,
        "max": sorted_values[-1] if sorted_values else None,
        "values": sorted_values,
        "varying_files": varying_files,
        "constant_files": constant_files,
    }


# ------------------------------------------------------------------
# Per-file cache helpers
# ------------------------------------------------------------------

def _cache_root(root: Path) -> Path:
    return root / "__campaign_data_cache__"


def _report_cache_dir(root: Path) -> Path:
    return _cache_root(root) / "reports"


def _report_cache_path(root: Path, file_path: Path) -> Path:
    safe = Path(file_path).name
    return _report_cache_dir(root) / f"{safe}.json"


def _load_report_cache(root, file_path, header_len, float_rnd=None,
                       float_rnd_mode=None, dup_freq_tolerance=None):
    """Load a single GridReport from its per-file cache, if still valid.

    Valid when: the source file's current mtime matches the cached mtime,
    the cached header_len matches, AND the cached float_rnd/float_rnd_mode/
    dup_freq_tolerance match.
    """
    cp = _report_cache_path(root, file_path)
    if not cp.exists():
        return None
    try:
        with cp.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    if data.get("header_len") != header_len:
        return None
    if float_rnd is not None and data.get("float_rnd") != float_rnd:
        return None
    if float_rnd_mode is not None and data.get("float_rnd_mode") != float_rnd_mode:
        return None
    if dup_freq_tolerance is not None and data.get("dup_freq_tolerance") != dup_freq_tolerance:
        return None

    try:
        current_mtime = Path(file_path).stat().st_mtime
    except Exception:
        return None
    if data.get("source_mtime") != current_mtime:
        return None

    try:
        return grid_report_from_dict(data)
    except Exception:
        return None


def _save_report_cache(root, report, header_len, float_rnd=None,
                       float_rnd_mode=None, dup_freq_tolerance=None):
    """Save a single GridReport to its per-file cache."""
    cp = _report_cache_path(root, report.path)
    cp.parent.mkdir(parents=True, exist_ok=True)
    data = grid_report_to_dict(report, header_len)
    data["float_rnd"] = float_rnd
    data["float_rnd_mode"] = float_rnd_mode
    data["dup_freq_tolerance"] = dup_freq_tolerance
    try:
        data["source_mtime"] = report.path.stat().st_mtime
    except Exception:
        data["source_mtime"] = 0
    try:
        with cp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def _cleanup_orphan_caches(root, current_paths):
    """Remove per-file cache entries for files that no longer exist."""
    current_names = {Path(p).name for p in current_paths}
    reports_dir = _report_cache_dir(root)
    if not reports_dir.exists():
        return
    for cache_file in list(reports_dir.glob("*.json")):
        # cache file name is "<csv_filename>.json"
        csv_name = cache_file.name[:-5]  # strip ".json"
        if csv_name not in current_names:
            try:
                cache_file.unlink()
            except Exception:
                pass


def build_directory_index(source, *, header_len=None, overlap_sample_limit=20,
                          float_rnd=None, float_rnd_mode=None,
                          dup_freq_tolerance=None):
    """Scan a CSV folder and return a serializable index object.

    Results are cached in memory so repeated loads of the same source
    (with the same header_len, float_rnd, float_rnd_mode, dup_freq_tolerance)
    return instantly.

    Args:
        source: directory or file path.
        header_len: number of leading columns treated as scan parameters.
            If None, auto-detect from the first valid file.
        overlap_sample_limit: maximum number of overlapping grid points to keep.
        float_rnd: FLOAT_RND override (None = use current module default).
        float_rnd_mode: ROUND_MODE override (None = use current module default).
        dup_freq_tolerance: intra-point block-duplicate tolerance override
            (None = use current module default).
    """
    if float_rnd is None:
        from campaign_data.io_utils import FLOAT_RND
        float_rnd = FLOAT_RND
    if float_rnd_mode is None:
        from campaign_data.io_utils import ROUND_MODE
        float_rnd_mode = ROUND_MODE
    source_path = Path(source)
    if source_path.is_file():
        root = source_path.parent
        paths = [source_path]
    else:
        root = source_path
        paths = collect_paths([source_path])

    if not paths:
        return DirectoryIndex(
            root=root,
            header_len=header_len,
            file_count=0,
            total_rows=0,
            axis_catalog=[],
            file_summaries=[],
            groups=[],
            overlap_count=0,
            overlap_samples=[],
            issues=[f"No CSV/TSV files found under {root}"],
        )

    # Auto-detect header_len
    if header_len is None:
        try:
            header_len = detect_header_len(paths[0])
        except Exception:
            header_len = HEADER_LEN

    # 1) Memory cache (fast intra-process reload — no file I/O)
    cache_key = (str(root.resolve()), header_len or 0, float_rnd, dup_freq_tolerance)
    if cache_key in _INDEX_CACHE:
        _ts, cached = _INDEX_CACHE[cache_key]
        if cached.file_count == len(paths):
            return cached

    # 2) Per-file cache: load cached GridReports, parse only what changed
    reports = []
    issues = []
    cache_hits = 0
    cache_misses = 0

    for path in paths:
        try:
            if Path(path).stat().st_size == 0:
                issues.append(f"Skipped empty file: {Path(path).name}")
                continue
        except Exception as exc:
            issues.append(f"Skipped unreadable file {path}: {exc}")
            continue

        cached = _load_report_cache(root, path, header_len, float_rnd,
                                     float_rnd_mode, dup_freq_tolerance)
        if cached is not None:
            reports.append(cached)
            cache_hits += 1
            continue

        try:
            rep = analyse_file(path, header_len=header_len,
                               dup_freq_tolerance=dup_freq_tolerance)
            reports.append(rep)
            _save_report_cache(root, rep, header_len, float_rnd,
                                float_rnd_mode, dup_freq_tolerance)
            cache_misses += 1
        except Exception as exc:
            issues.append(f"Skipped malformed file {Path(path).name}: {exc}")

    # Clean up orphan cache files
    _cleanup_orphan_caches(root, paths)

    if not reports:
        return DirectoryIndex(
            root=root,
            header_len=header_len,
            file_count=0,
            total_rows=0,
            axis_catalog=[],
            file_summaries=[],
            groups=[],
            overlap_count=0,
            overlap_samples=[],
            issues=issues or [f"No parseable CSV/TSV files found under {root}"],
        )

    ref_header = reports[0].header
    for rep in reports[1:]:
        if rep.header[:header_len] != ref_header[:header_len]:
            issues.append(
                f"Header mismatch: {rep.path.name} does not match {reports[0].path.name}"
            )

    all_varying = sorted(set().union(*(rep.varying.keys() for rep in reports)))
    axis_catalog = [
        _axis_summary(reports, axis_idx, ref_header, axis_type="varying")
        for axis_idx in all_varying
    ]

    # Meta axes: columns constant in every file, but different values across files
    for col_idx in range(header_len):
        if col_idx in all_varying:
            continue
        cross_values = set()
        for rep in reports:
            if col_idx in rep.constant:
                cross_values.add(rep.constant[col_idx])
        if len(cross_values) >= 2:
            axis_catalog.append(
                _axis_summary(reports, col_idx, ref_header, axis_type="meta")
            )

    file_summaries = []
    total_rows = 0
    for rep in reports:
        total_rows += rep.data_rows
        varying_idx = sorted(rep.varying.keys())
        varying_names = [rep.header[i] for i in varying_idx]
        constant_names = [rep.header[i] for i in sorted(rep.constant.keys())]
        relative_path = rep.path.name
        try:
            relative_path = str(rep.path.relative_to(root))
        except Exception:
            pass
        try:
            file_mtime = rep.path.stat().st_mtime
        except Exception:
            file_mtime = 0
        file_summaries.append({
            "name": rep.path.name,
            "path": str(rep.path),
            "relative_path": relative_path,
            "mtime": file_mtime,
            "data_rows": rep.data_rows,
            "varying": [
                {
                    "index": i,
                    "name": rep.header[i],
                    "unique_count": len(rep.varying[i]),
                    "min": rep.varying[i][0] if rep.varying[i] else None,
                    "max": rep.varying[i][-1] if rep.varying[i] else None,
                    "values": rep.varying[i],
                }
                for i in varying_idx
            ],
            "constant": [
                {
                    "index": i,
                    "name": rep.header[i],
                    "value": rep.constant[i],
                }
                for i in sorted(rep.constant.keys())
            ],
            "varying_names": varying_names,
            "constant_names": constant_names,
            "expected_points": rep.expected_points,
            "actual_points": rep.actual_points,
            "completeness": rep.completeness,
            "eigen_counts": rep.eigen_counts,
            "grid_shape": [len(rep.varying[i]) for i in varying_idx],
        })

    group_map = defaultdict(list)
    for summary in file_summaries:
        signature = tuple(summary["varying_names"])
        group_map[signature].append(summary["name"])

    groups = [
        {
            "signature": list(signature),
            "file_count": len(files),
            "files": files,
        }
        for signature, files in sorted(group_map.items(), key=lambda item: (len(item[0]), item[0]))
    ]

    overlap = find_overlap_region(reports)
    overlap_samples = []
    for grid_point, owners in list(overlap.items())[:overlap_sample_limit]:
        overlap_samples.append({
            "grid_point": list(grid_point),
            "files": [reports[idx].path.name for idx in owners],
        })

    result = DirectoryIndex(
        root=root,
        header_len=header_len,
        file_count=len(reports),
        total_rows=total_rows,
        axis_catalog=axis_catalog,
        file_summaries=file_summaries,
        groups=groups,
        overlap_count=len(overlap),
        overlap_samples=overlap_samples,
        issues=issues,
        cache_hits=cache_hits,
        cache_misses=cache_misses,
    )

    # Store in memory cache
    if len(_INDEX_CACHE) >= _CACHE_MAX:
        oldest_key = min(_INDEX_CACHE, key=lambda k: _INDEX_CACHE[k][0])
        del _INDEX_CACHE[oldest_key]
    _INDEX_CACHE[cache_key] = (time.time(), result)

    return result


def dump_directory_index(source, out_path, *, header_len=HEADER_LEN, overlap_sample_limit=20):
    """Build and write a directory index as JSON."""
    index = build_directory_index(
        source,
        header_len=header_len,
        overlap_sample_limit=overlap_sample_limit,
    )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(index.to_dict(), f, ensure_ascii=False, indent=2)
    return out_path
