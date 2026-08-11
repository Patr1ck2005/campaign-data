"""Smart splitter for merged parameter-scan data.

After merging many export files, the result can be impractically
large.  This module analyses the canonical grid and splits it along
user-chosen dimensions (typically those with smaller cardinality).

Design principle: analysis, planning, and execution are three separate
steps so the user can review the plan before any files are written.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from campaign_data.io_utils import fv, write_tsv
from campaign_data.grid_analysis import (
    get_canonical_varying_order, classify_dimensions,
)
from campaign_data.dedup import iter_apply_dedup, _build_projection, _project_row


@dataclass
class SplitOption:
    """A candidate dimension for splitting."""
    col_idx: int
    col_name: str
    cardinality: int
    values: list              # sorted unique values (already fv-rounded)
    est_rows_per_file: int
    is_per_file_constant: bool = False  # constant within each source file


@dataclass
class SplitPlan:
    """A concrete split plan for user approval."""
    split_by: list[tuple[int, str]]                # [(col_idx, col_name), ...]
    files: list[dict] = field(default_factory=list)  # [{"condition": {...}, "path": Path}, ...]
    total_files: int = 0
    total_estimated_rows: int = 0


@dataclass
class SplitResult:
    """Result of executing a split."""
    output_paths: list[Path] = field(default_factory=list)
    total_rows_written: int = 0
    rows_per_file: dict = field(default_factory=dict)
    skipped_by_dedup: int = 0


# ---------------------------------------------------------------------------
# Analysis — "what can we split on?"
# ---------------------------------------------------------------------------

def analyze_split_options(reports, varying_order=None):
    """Return viable split dimensions sorted by cardinality (smallest first).

    Pure analysis — no files written.  Call after dry_run to present options
    to the user.

    Each option is labelled as grid_axis (varies within files) or group_key
    (constant within most files, differs across files).
    """
    if varying_order is None:
        varying_order = get_canonical_varying_order(reports)

    grid_axes, group_keys = classify_dimensions(reports)
    group_key_set = set(group_keys)
    total_rows = sum(rep.data_rows for rep in reports)
    options = []

    for ci in varying_order:
        all_vals = set()
        for rep in reports:
            if ci in rep.varying:
                all_vals.update(rep.varying[ci])
            elif ci in rep.constant:
                all_vals.add(rep.constant[ci])

        cardinality = len(all_vals)
        if cardinality < 2:
            continue

        name = reports[0].header[ci]
        options.append(SplitOption(
            col_idx=ci,
            col_name=name,
            cardinality=cardinality,
            values=sorted(all_vals),
            est_rows_per_file=total_rows // cardinality,
            is_per_file_constant=(ci in group_key_set),
        ))

    options.sort(key=lambda o: o.cardinality)
    return options


# ---------------------------------------------------------------------------
# Planning — "what would the output files look like?"
# ---------------------------------------------------------------------------

def _resolve_split_by(reports, split_by):
    """Turn a list of column-indices-or-names into [(col_idx, col_name), ...]."""
    header = reports[0].header
    result = []
    for item in split_by:
        if isinstance(item, int):
            result.append((item, header[item]))
        else:
            # look up by name
            for ci, name in enumerate(header):
                if name == item or name.strip() == str(item).strip():
                    result.append((ci, name))
                    break
            else:
                raise KeyError(f"Column '{item}' not found in header")
    return result


def _format_value(v):
    """Format a float value cleanly for filenames, e.g. 0.0 or 344.0625."""
    if isinstance(v, float):
        if v == int(v):
            return str(int(v))
        return f"{v:g}"
    return str(v)


def plan_split(reports, split_by, varying_order=None, base_name="split"):
    """Generate a SplitPlan for user approval.

    Args:
        reports: list of GridReport from dry_run analysis
        split_by: list of column indices or column names to split on
        varying_order: optional pre-computed canonical varying order
        base_name: prefix for output filenames

    Returns:
        SplitPlan with the complete file list and estimated row counts.
    """
    if varying_order is None:
        varying_order = get_canonical_varying_order(reports)

    split_cols = _resolve_split_by(reports, split_by)

    # Collect unique values for each split column
    col_values = {}
    for ci, _ in split_cols:
        vals = set()
        for rep in reports:
            if ci in rep.varying:
                vals.update(rep.varying[ci])
            elif ci in rep.constant:
                vals.add(rep.constant[ci])
        col_values[ci] = sorted(vals)

    # Build Cartesian product of split values
    total_rows = sum(rep.data_rows for rep in reports)
    files = []
    total_estimated = 0

    def _gen_combos(col_idx_list):
        if not col_idx_list:
            yield {}
            return
        ci = col_idx_list[0]
        rest = col_idx_list[1:]
        for v in col_values[ci]:
            for combo in _gen_combos(rest):
                combo[ci] = v
                yield combo

    split_indices = [ci for ci, _ in split_cols]
    for condition in _gen_combos(split_indices):
        # Build filename suffix
        suffix_parts = []
        for ci, name in split_cols:
            v = condition[ci]
            suffix_parts.append(f"{name}={_format_value(v)}")
        suffix = "-[" + "]-[".join(suffix_parts) + "]"
        fname = f"{base_name}{suffix}.txt"

        est = total_rows // len(col_values[split_indices[0]])
        # refine estimate for multi-dim splits
        if len(split_indices) > 1:
            est = total_rows
            for ci in split_indices:
                est = est // len(col_values[ci])

        files.append({
            "condition": dict(condition),
            "path": Path(fname),
        })
        total_estimated += est

    return SplitPlan(
        split_by=split_cols,
        files=files,
        total_files=len(files),
        total_estimated_rows=total_estimated,
    )


# ---------------------------------------------------------------------------
# Execution — "write the split files"
# ---------------------------------------------------------------------------

def execute_split(reports, plan, out_dir, all_data=None, resolutions=None,
                  varying_order=None, clean_floats=True):
    """Execute a split plan, writing output files.

    Args:
        reports: list of GridReport
        plan: SplitPlan from plan_split()
        out_dir: directory to write split files to
        all_data: {file_index: [data_rows]} — if None, streams from report.path
        resolutions: dedup resolutions dict (optional)
        varying_order: canonical varying order (optional)
        clean_floats: whether to clean float noise in output

    When all_data is None, files are read and processed one at a time
    (streaming mode).  This avoids loading all rows into memory but
    requires that no cross-file dedup is needed (resolutions is empty).

    Returns:
        SplitResult with per-file row counts and totals.
    """
    # Build split-key → output writer mapping
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    split_indices = [ci for ci, _ in plan.split_by]
    header = reports[0].header

    import csv
    # key → (writer, out_path_string)
    key_to_writer = {}
    opened_files = []
    key_to_path = {}
    for finfo in plan.files:
        cond = finfo["condition"]
        key = tuple(cond.get(ci) for ci in split_indices)
        out_path = out_dir / finfo["path"]
        f = open(out_path, 'w', newline='', encoding='utf-8-sig')
        opened_files.append(f)
        w = csv.writer(f, delimiter='\t')
        w.writerow(header)
        key_to_writer[key] = w
        key_to_path[key] = str(out_path)

    rows_per_file = {str(out_dir / f["path"]): 0 for f in plan.files}
    skipped_by_dedup = 0
    total_written = 0

    from campaign_data.io_utils import clean_cell, fv, OUT_FLOAT_RND
    if all_data is not None:
        data_source = _iter_from_dict(all_data)
    else:
        data_source = _iter_from_files(reports)

    # Normalize parameter columns to fv() precision,
    # output columns via clean_cell (OUT_FLOAT_RND)
    header_len = len(header)
    def _write_row(w, row):
        cells = []
        for ci, c in enumerate(row):
            if ci < header_len:
                cells.append(str(fv(c)))
            elif clean_floats:
                cells.append(clean_cell(c, OUT_FLOAT_RND))
            else:
                cells.append(c)
        w.writerow(cells)

    projections = {}
    for fi, r in data_source:
        if resolutions:
            if fi not in projections:
                projections[fi] = _build_projection(reports[fi], varying_order)
            gk = _project_row(r, projections[fi])
            if gk in resolutions and resolutions[gk] != fi:
                skipped_by_dedup += 1
                continue

        split_vals = tuple(fv(r[ci]) for ci in split_indices)
        w = key_to_writer.get(split_vals)
        if w is not None:
            _write_row(w, r)
            rows_per_file[key_to_path[split_vals]] += 1
            total_written += 1

    for f in opened_files:
        f.close()

    return SplitResult(
        output_paths=[out_dir / f["path"] for f in plan.files],
        total_rows_written=total_written,
        rows_per_file=rows_per_file,
        skipped_by_dedup=skipped_by_dedup,
    )


def _iter_from_dict(all_data):
    """Yield (file_index, row) from an in-memory all_data dict."""
    for fi in sorted(all_data):
        for r in all_data[fi]:
            yield fi, r


def _iter_from_files(reports):
    """Yield (file_index, row) by streaming from report.path."""
    from campaign_data.io_utils import read_csv, read_tsv
    from campaign_data.grid_analysis import remove_intra_point_duplicates
    for fi, rep in enumerate(reports):
        if rep.path.suffix.lower() == '.csv':
            rows = read_csv(rep.path)
        else:
            rows = read_tsv(rep.path)
        data = rows[1:]  # skip header
        if rep.duplicate_rows:
            data = remove_intra_point_duplicates(data, rep.varying, rep.header)
        for r in data:
            yield fi, r


# ---------------------------------------------------------------------------
# Convenience — full pipeline in one call
# ---------------------------------------------------------------------------

def smart_merge_and_split(sources, split_by, out_dir, clean_floats=True,
                          header_len=None, use_cache=True, parallel=False,
                          float_rnd=None, recursive=False,
                          path_dims=None, dup_freq_tolerance=None):
    """Deprecated: use smart_merge(split_by=..., split_out_dir=...) instead.

    smart_merge_and_split predates smart_merge's split_by parameter and lacks
    path_dims support. It now delegates to smart_merge with equivalent args.
    """
    import warnings
    warnings.warn(
        "smart_merge_and_split is deprecated; use "
        "smart_merge(sources, split_by=..., split_out_dir=..., path_dims=...)",
        DeprecationWarning,
        stacklevel=2,
    )
    from campaign_data.merge import smart_merge
    result = smart_merge(
        sources, split_by=split_by, split_out_dir=out_dir,
        clean_floats=clean_floats, header_len=header_len,
        use_cache=use_cache, parallel=parallel, float_rnd=float_rnd,
        recursive=recursive, path_dims=path_dims,
        dup_freq_tolerance=dup_freq_tolerance,
    )
    # smart_merge returns SplitResult in split mode, Path in merge mode, dict in dry_run
    if isinstance(result, SplitResult):
        return result
    return SplitResult()
