"""Grid structure analysis for tab-separated parameter-scan data.

Detects varying vs. constant columns, builds grid-point sets, computes
completeness, and identifies overlapping regions between files.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from campaign_data.io_utils import fv, read_and_parse, detect_header_len, HEADER_LEN


@dataclass
class GridReport:
    """Result of analysing one file's grid structure."""
    path: Path
    header: list[str]
    data_rows: int
    varying: dict[int, list]          # {col_idx: sorted_unique_values}
    constant: dict[int, str]          # {col_idx: constant_value}
    expected_points: int              # product of varying cardinalities
    actual_points: int                # count of unique grid-point tuples
    completeness: float               # actual / expected
    eigen_counts: list[int]           # distinct eigenmode multiplicities found
    grid_set: set[tuple] = field(repr=False)             # unique grid-point tuples
    duplicate_rows: int = 0           # intra-point duplicate eigenmode count


@dataclass
class FileGroupMember:
    """Per-file details within a structure group."""
    path: Path
    constant_values: dict[int, str]   # distinguishing params
    data_rows: int
    actual_points: int
    completeness: float
    tag: str                          # "OK" or "XX%"


@dataclass
class FileGroup:
    """A group of files sharing the same varying-column structure."""
    signature: tuple[str, ...]        # sorted varying column names
    varying: dict[int, list]          # shared across all members
    expected_points: int              # shared across all members
    members: list[FileGroupMember]    # per-file differences


def build_file_groups(reports, header):
    """Cluster reports by varying-column signature into FileGroups."""
    from collections import defaultdict
    groups = defaultdict(list)
    for rep in reports:
        sig = tuple(sorted(header[ci] for ci in rep.varying))
        groups[sig].append(rep)

    result = []
    for sig in sorted(groups, key=lambda s: -len(groups[s])):
        reps = groups[sig]
        first = reps[0]
        members = []
        for rep in reps:
            tag = "OK" if rep.completeness >= 0.999 else f"{rep.completeness*100:.0f}%"
            members.append(FileGroupMember(
                path=rep.path,
                constant_values=rep.constant,
                data_rows=rep.data_rows,
                actual_points=rep.actual_points,
                completeness=rep.completeness,
                tag=tag,
            ))
        result.append(FileGroup(
            signature=sig,
            varying=first.varying,
            expected_points=first.expected_points,
            members=members,
        ))
    return result


def grid_report_to_dict(report: GridReport, header_len: int) -> dict:
    """Serialize a GridReport to a JSON-compatible dict for caching."""
    return {
        "header_len": header_len,
        "source_path": str(report.path),
        "header": report.header,
        "data_rows": report.data_rows,
        "varying": {str(k): v for k, v in report.varying.items()},
        "constant": {str(k): v for k, v in report.constant.items()},
        "expected_points": report.expected_points,
        "actual_points": report.actual_points,
        "completeness": report.completeness,
        "eigen_counts": report.eigen_counts,
        "grid_set": [list(pt) for pt in report.grid_set],
        "duplicate_rows": report.duplicate_rows,
    }


def grid_report_from_dict(data: dict) -> GridReport:
    """Deserialize a GridReport from a cache dict."""
    return GridReport(
        path=Path(data["source_path"]),
        header=data["header"],
        data_rows=data["data_rows"],
        varying={int(k): v for k, v in data["varying"].items()},
        constant={int(k): v for k, v in data["constant"].items()},
        expected_points=data["expected_points"],
        actual_points=data["actual_points"],
        completeness=data["completeness"],
        eigen_counts=data["eigen_counts"],
        grid_set={tuple(pt) for pt in data["grid_set"]},
        duplicate_rows=data.get("duplicate_rows", 0),
    )


def _analyse_rows(rows, header_len, path_groups=None):
    """Two-pass engine: classify then build grid_set with path tuples.

    Pass 1 — classify columns as varying vs constant (requires full scan).
    Pass 2 — build grid_set, eigen counts, and path-dimension tuple sets
    in a single combined scan.  This merges the old three-pass approach
    (_classify_cols + _build_grid_set_and_counts + path-dim re-scan) into
    two passes, eliminating one full data iteration.

    Returns (varying, constant, grid_set, eigen_counts, path_sets).
    *path_sets* is a dict {group_index_tuple: set_of_unique_tuples}, or
    None when *path_groups* is empty.
    """
    # ---- Pass 1: classify --------------------------------------------------
    by_col = defaultdict(set)
    for r in rows:
        for i in range(header_len):
            by_col[i].add(fv(r[i]))

    varying = {}
    constant = {}
    for i, vals in by_col.items():
        if len(vals) > 1:
            varying[i] = sorted(vals)
        else:
            constant[i] = vals.pop()

    # ---- Pass 2: grid set + eigen + path tuples ----------------------------
    grid_pts = set()
    eigen = defaultdict(int)
    path_sets = None
    if path_groups:
        path_sets = {}
        for g in path_groups:
            g_in_v = tuple(ci for ci in g if ci in varying)
            if len(g_in_v) >= 2:
                path_sets[g_in_v] = set()

    for r in rows:
        tup = tuple(fv(r[i]) for i in varying)
        grid_pts.add(tup)
        eigen[tup] += 1
        if path_sets:
            for g_in_v, pset in path_sets.items():
                pset.add(tuple(fv(r[ci]) for ci in g_in_v))

    return varying, constant, grid_pts, sorted(set(eigen.values())), path_sets


DUP_FREQ_TOLERANCE = 1e-6  # max relative freq diff between block-duplicate pairs


def _find_freq_column(header):
    """Return the column index of the complex eigenfrequency, or None."""
    for i, h in enumerate(header):
        if 'THz' in h:
            return i
    for i, h in enumerate(header):
        if 'z' in h.lower() and i > 0:
            if 'Hz' in h or 'hz' in h.lower():
                return i
    return None


def _parse_freq_real(raw):
    """Extract the real part of a complex frequency string like '189.9+56.7i'."""
    raw = raw.strip().replace('i', 'j')
    try:
        return complex(raw).real
    except ValueError:
        return float(raw)


def detect_anomalous_points(data, varying, header, float_rnd=None):
    """Detect grid points with more rows than the standard eigenmode count.

    Pure count-based detection - no frequency comparison. Returns:
      - point_indices: {grid_point_tuple: [row_indices]} for ALL points
      - standard: modal row count (typical eigenmode count per grid point)
      - anomalous: {grid_point_tuple: [row_indices]} for points where
        actual > standard (candidates for block-duplicate removal)

    The caller is responsible for verifying that anomalous points are true
    block duplicates (e.g. via verify_block_duplicates) before removing rows.
    Returns (point_indices, standard, {}) if no varying columns or no data.
    """
    from collections import Counter
    from campaign_data.io_utils import fv, FLOAT_RND as _FR

    if float_rnd is None:
        float_rnd = _FR
    if not varying:
        return {}, 0, {}

    point_indices = defaultdict(list)
    for ri, r in enumerate(data):
        tup = tuple(fv(r[i], float_rnd) for i in varying)
        point_indices[tup].append(ri)

    if not point_indices:
        return {}, 0, {}

    counts = Counter(len(v) for v in point_indices.values())
    standard = counts.most_common(1)[0][0]

    anomalous = {tup: idxs for tup, idxs in point_indices.items()
                 if len(idxs) > standard}
    return point_indices, standard, anomalous


def verify_block_duplicates(data, indices, standard, header,
                            dup_freq_tolerance=None):
    """Verify that rows at a single grid point follow block-duplicate structure.

    Rows are expected to be ordered as [block_0, block_1, ...] where each
    block has `standard` rows.  For each offset i in [0, standard), compares
    the eigenfrequency of row[i] with row[i + standard], row[i + 2*standard],
    etc.  If all relative differences are within *dup_freq_tolerance*, the
    block structure is confirmed.

    Raises ValueError if:
      - len(indices) is not a multiple of standard
      - any frequency pair exceeds the tolerance (distinct modes that
        coincidentally share a grid point, not true duplicates)

    Returns None on success.  Callers should keep the first `standard` rows
    and discard the rest only after this function returns without error.
    """
    if dup_freq_tolerance is None:
        dup_freq_tolerance = DUP_FREQ_TOLERANCE

    actual = len(indices)
    n_blocks = actual // standard
    if actual % standard != 0:
        raise ValueError(
            f"Intra-point row count {actual} is not a multiple of the "
            f"standard eigenmode count {standard} at grid point with "
            f"{actual} rows in {getattr(data, 'source', 'data')}.  "
            f"Expected block-structured duplicates."
        )

    freq_col = _find_freq_column(header)
    if freq_col is None:
        return  # no frequency column to verify against; trust the structure

    for offset in range(standard):
        freqs = []
        for b in range(n_blocks):
            ri = indices[offset + b * standard]
            freqs.append(_parse_freq_real(data[ri][freq_col]))

        ref = freqs[0]
        for b in range(1, n_blocks):
            other = freqs[b]
            if ref == 0 and other == 0:
                rel_diff = 0.0
            elif ref == 0 or other == 0:
                rel_diff = float('inf')
            else:
                rel_diff = abs(ref - other) / max(abs(ref), abs(other))
            if rel_diff > dup_freq_tolerance:
                raise ValueError(
                    f"Block-duplicate verification failed: "
                    f"row[{offset}] (freq={ref:.12g}) vs "
                    f"row[{offset + b * standard}] (freq={other:.12g}), "
                    f"relative difference {rel_diff:.2e} exceeds tolerance "
                    f"{dup_freq_tolerance:.1e}.  "
                    f"Data does not follow the expected block-duplicate format."
                )


def detect_intra_point_duplicates(data, varying, header, float_rnd=None,
                                  dup_freq_tolerance=None):
    """Detect and verify block-duplicate eigenmode rows within grid points.

    Composition of detect_anomalous_points (count-based detection) and
    verify_block_duplicates (frequency verification).  The first block at
    each anomalous point is kept; subsequent blocks are marked for removal.

    Returns (duplicate_indices, summary) where:
      - duplicate_indices: set of data row indices to remove
      - summary: {grid_point_tuple: {standard, actual, removed}}
    """
    from campaign_data.io_utils import FLOAT_RND as _FR

    if float_rnd is None:
        float_rnd = _FR
    if dup_freq_tolerance is None:
        dup_freq_tolerance = DUP_FREQ_TOLERANCE
    if not varying:
        return set(), {}

    _point_indices, standard, anomalous = detect_anomalous_points(
        data, varying, header, float_rnd=float_rnd)
    if not anomalous:
        return set(), {}

    duplicate_indices = set()
    summary = {}
    for tup, indices in anomalous.items():
        verify_block_duplicates(data, indices, standard, header,
                                dup_freq_tolerance=dup_freq_tolerance)
        actual = len(indices)
        removed = actual - standard
        duplicate_indices.update(indices[standard:])
        summary[tup] = {"standard": standard, "actual": actual, "removed": removed}

    return duplicate_indices, summary


def remove_intra_point_duplicates(data, varying, header, float_rnd=None,
                                  dup_freq_tolerance=None):
    """Remove intra-point block-duplicate eigenmode rows.  Raises ValueError
    if the data does not conform to the expected block-duplicate structure."""
    dup_indices, _ = detect_intra_point_duplicates(
        data, varying, header, float_rnd,
        dup_freq_tolerance=dup_freq_tolerance)
    if not dup_indices:
        return data
    return [r for i, r in enumerate(data) if i not in dup_indices]


def _classify_cols(rows, header_len=HEADER_LEN):
    """Single-pass classification: return (varying, constant) dicts.

    Kept for backward compatibility; new code should prefer _analyse_rows.
    """
    varying, constant, _, _, _ = _analyse_rows(rows, header_len)
    return varying, constant


def _build_grid_set_and_counts(rows, varying, header_len=HEADER_LEN):
    """Single pass: return (grid_set, sorted_unique_eigen_counts).

    Kept for backward compatibility; new code should prefer _analyse_rows.
    """
    pts = set()
    eigen_counts = defaultdict(int)
    for r in rows:
        tup = tuple(fv(r[i]) for i in varying)
        pts.add(tup)
        eigen_counts[tup] += 1
    return pts, sorted(set(eigen_counts.values()))


def _varying_cols(rows, header_len=HEADER_LEN):
    """Return {col_idx: sorted_unique_values} for columns whose float-normalised
    values differ across rows.

    Kept for backward compatibility; prefer _classify_cols for new code.
    """
    varying, _ = _classify_cols(rows, header_len)
    return varying


def _constant_cols(rows, header_len=HEADER_LEN):
    """Return {col_idx: value} for columns whose float-normalised values
    are the same across all rows.

    Kept for backward compatibility; prefer _classify_cols for new code.
    """
    _, constant = _classify_cols(rows, header_len)
    return constant


def _grid_set(rows, varying, header_len=HEADER_LEN):
    """Build the set of actual grid-point tuples (using only varying columns)."""
    pts = set()
    for r in rows:
        pts.add(tuple(fv(r[i]) for i in varying))
    return pts


def _describe_grid(report=None, *, varying=None, header=None):
    """Human-readable grid dimensions string.

    Accepts either a GridReport as the first positional arg, or keyword
    varying/header dicts.  Shows exact values when cardinality ≤ 12,
    otherwise first 3 + last 3 + count.
    """
    if report is not None:
        varying, header = report.varying, report.header

    def _fmt_vals(vals):
        if len(vals) <= 12:
            return str(vals)
        return f"[{vals[0]}, {vals[1]}, {vals[2]}, ..., {vals[-3]}, {vals[-2]}, {vals[-1]}] ({len(vals)} values)"

    parts = []
    for i in sorted(varying):
        vals = varying[i]
        parts.append(f"{header[i]}: {_fmt_vals(vals)}")
    return " x ".join(parts)


def classify_dimensions(reports):
    """Return (grid_axes, group_keys) — two lists of column indices.

    A column is classified as a group_key (not a grid axis) when it is
    constant within the MAJORITY of files and only varies in a minority.
    This avoids labelling a column as a grid axis when only 2 out of 226
    files actually sweep it.
    """
    n = len(reports)
    all_cols = set()
    for rep in reports:
        all_cols.update(rep.varying.keys())
        all_cols.update(rep.constant.keys())

    n_varying = defaultdict(int)
    n_constant = defaultdict(int)
    for rep in reports:
        for ci in rep.varying:
            n_varying[ci] += 1
        for ci in rep.constant:
            n_constant[ci] += 1

    grid_axes = []
    group_keys = []
    for ci in sorted(all_cols):
        if n_varying[ci] > n_constant[ci]:
            grid_axes.append(ci)
        else:
            # constant in majority → check if values differ across files
            vals = set()
            for rep in reports:
                if ci in rep.constant:
                    vals.add(rep.constant[ci])
                elif ci in rep.varying:
                    vals.update(rep.varying[ci])
            if len(vals) > 1:
                group_keys.append(ci)

    return grid_axes, group_keys


def analyse_file(path, header_len=None, return_data=False, path_dims=None,
                 dup_freq_tolerance=None):
    """Analyse a single file. Returns a GridReport, or (GridReport, data_rows)
    when return_data=True.

    If header_len is None, auto-detect the parameter/output boundary.
    When path_dims is specified (e.g. [("m1","m2")]), columns in the same
    path group are treated as a single dimension whose expected cardinality
    is the number of unique tuples rather than the product.
    dup_freq_tolerance overrides the default DUP_FREQ_TOLERANCE for intra-point
    block-duplicate verification (pass a tolerance appropriate for export noise).
    """
    if header_len is None:
        header_len = detect_header_len(path)
    header, data = read_and_parse(path)

    # Map path_dims column names to indices
    _path_groups = _resolve_path_dims(path_dims, header) if path_dims else []

    varying, constant, actual_pts, eigen_counts, path_sets = \
        _analyse_rows(data, header_len, _path_groups)

    expected = 1
    for v in varying.values():
        expected *= len(v)

    # Adjust expected for path groups using tuples already collected in pass 2
    if path_sets:
        for g_in_v, pset in path_sets.items():
            prod = 1
            for ci in g_in_v:
                prod *= len(varying[ci])
            expected = expected // prod * len(pset)

    dup_indices, _dup_summary = detect_intra_point_duplicates(
        data, varying, header, dup_freq_tolerance=dup_freq_tolerance)

    report = GridReport(
        path=Path(path),
        header=header,
        data_rows=len(data),
        varying=varying,
        constant=constant,
        expected_points=expected,
        actual_points=len(actual_pts),
        completeness=len(actual_pts) / expected if expected else 1.0,
        eigen_counts=eigen_counts,
        grid_set=actual_pts,
        duplicate_rows=len(dup_indices),
    )
    if return_data:
        return report, data
    return report


def analyse_files(paths, header_len=None, return_data=False, parallel=False,
                  path_dims=None, dup_freq_tolerance=None):
    """Read and analyse every input file. Returns list of GridReport.

    If header_len is None, auto-detect the parameter/output boundary per file.
    When return_data=True, returns (list[GridReport], dict[int, data_rows]).
    When parallel=True, uses multiprocessing to analyse files concurrently.
    When path_dims is specified, columns in the same group are treated as a
    single path dimension.
    dup_freq_tolerance overrides the default DUP_FREQ_TOLERANCE for intra-point
    block-duplicate verification.
    """
    args = [(p, header_len, return_data, path_dims, dup_freq_tolerance) for p in paths]
    if parallel and len(paths) > 1:
        from multiprocessing import Pool, cpu_count
        workers = min(len(paths), cpu_count())
        with Pool(workers) as pool:
            results = pool.starmap(_analyse_one, args)
        if return_data:
            reports = []
            all_data = {}
            for fi, (rep, data) in enumerate(results):
                reports.append(rep)
                all_data[fi] = data
            return reports, all_data
        return results

    # ---- Serial path -------------------------------------------------------
    if return_data:
        reports = []
        all_data = {}
        for fi, p in enumerate(paths):
            rep, data = analyse_file(p, header_len, return_data=True,
                                     path_dims=path_dims,
                                     dup_freq_tolerance=dup_freq_tolerance)
            reports.append(rep)
            all_data[fi] = data
        return reports, all_data
    return [analyse_file(p, header_len, path_dims=path_dims,
                         dup_freq_tolerance=dup_freq_tolerance)
            for p in paths]


def _resolve_path_dims(path_dims, header):
    """Convert path_dims name tuples to column-index tuples.

    path_dims = [("m1", "m2")] → [[col_m1_idx, col_m2_idx]]
    """
    if not path_dims:
        return []
    result = []
    for group in path_dims:
        indices = []
        for name in group:
            for ci, h in enumerate(header):
                if h == name:
                    indices.append(ci)
                    break
        if len(indices) == len(group):
            result.append(indices)
    return result


def _analyse_one(path, header_len, return_data, path_dims=None,
                 dup_freq_tolerance=None):
    """Top-level helper for multiprocessing — calls analyse_file."""
    return analyse_file(path, header_len, return_data, path_dims,
                        dup_freq_tolerance=dup_freq_tolerance)


def _slice_completeness(rows, varying, header, header_len=HEADER_LEN):
    """For each varying column, check per-slice completeness.

    Returns a list of human-readable lines describing gaps.
    """
    expected_product = 1
    for v in varying.values():
        expected_product *= len(v)

    if len(varying) <= 1:
        return []  # 1-D grid is always trivially complete

    lines = []
    for anchor_idx in sorted(varying):
        anchor_name = header[anchor_idx]
        anchor_vals = varying[anchor_idx]
        sub_expected = expected_product // len(anchor_vals)

        by_anchor = defaultdict(set)
        other_cols = [i for i in varying if i != anchor_idx]
        for r in rows:
            fv_row = tuple(fv(r[i]) for i in range(header_len))
            anchor = fv_row[anchor_idx]
            sub_key = tuple(fv_row[i] for i in other_cols)
            by_anchor[anchor].add(sub_key)

        gaps = []
        for av in anchor_vals:
            actual = len(by_anchor.get(av, set()))
            if actual < sub_expected:
                gaps.append((av, actual, sub_expected))

        if gaps:
            lines.append(f"  By {anchor_name}:")
            for av, act, exp in gaps[:8]:
                lines.append(f"    {anchor_name}={av}: {act}/{exp} sub-points ({act/exp*100:.1f}%)")
            if len(gaps) > 8:
                lines.append(f"    ... and {len(gaps) - 8} more incomplete slices")
    return lines


def find_overlap_region(reports, varying_order=None):
    """Return {grid_point: [file_indices]} for grid points appearing in >1 file.

    A grid point is a tuple of rounded values from the varying columns of its
    source report. Since different files may have different varying columns,
    we build the global varying set (union) and use all of them as keys.

    Columns that are constant within every file but differ *across* files
    (e.g. a parameter split into one value per file) are promoted to the
    canonical varying set so they distinguish grid points from different files.
    """
    if varying_order is None:
        varying_order = get_canonical_varying_order(reports)
    if not varying_order:
        return {}

    # For each file, project its grid set into the canonical key space.
    # For columns this file doesn't vary (constant), use that constant value.
    file_grids = []
    for rep in reports:
        const_map = {i: rep.constant[i] for i in varying_order if i in rep.constant}
        proj = set()
        for pt in rep.grid_set:
            # pt is ordered by rep.varying keys; map to canonical order
            pt_map = dict(zip(sorted(rep.varying.keys()), pt))
            proj.add(tuple(
                pt_map[i] if i in pt_map else const_map[i] for i in varying_order
            ))
        file_grids.append(proj)

    all_grids = defaultdict(list)
    for idx, gset in enumerate(file_grids):
        for gk in gset:
            all_grids[gk].append(idx)

    return {gk: owners for gk, owners in all_grids.items() if len(owners) > 1}


def get_canonical_varying_order(reports):
    """Return sorted list of varying column indices across all reports.

    Columns constant within every file but differing across files are promoted
    to varying so they distinguish grid points from different files.
    """
    all_varying = set()
    for rep in reports:
        all_varying.update(rep.varying.keys())

    all_cols = set()
    for rep in reports:
        all_cols.update(rep.varying.keys())
        all_cols.update(rep.constant.keys())
    for ci in all_cols:
        if ci in all_varying:
            continue
        vals = set()
        for rep in reports:
            if ci in rep.constant:
                vals.add(rep.constant[ci])
        if len(vals) > 1:
            all_varying.add(ci)

    return sorted(all_varying)


def per_slice_density(report, anchor_col):
    """For a given varying column, return {anchor_value: sub_point_count}.

    Sub-points are unique combinations of the OTHER varying dimensions.
    Useful for comparing completeness of a specific slice between files.
    """
    if anchor_col not in report.varying:
        return {}
    other_cols = [i for i in sorted(report.varying) if i != anchor_col]
    if not other_cols:
        # Only one varying column: density is 1 per anchor value
        return {av: 1 for av in report.varying[anchor_col]}

    by_anchor = defaultdict(set)
    for pt in report.grid_set:
        pt_map = dict(zip(sorted(report.varying.keys()), pt))
        anchor_val = pt_map[anchor_col]
        sub_key = tuple(pt_map[i] for i in other_cols)
        by_anchor[anchor_val].add(sub_key)
    return {av: len(sub_pts) for av, sub_pts in by_anchor.items()}
