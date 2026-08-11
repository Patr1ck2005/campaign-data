"""Analysis report for parameter-scan data directories.

Four-section structure:
  1. Overview   — what's here?
  2. Groups     — which files share the same grid structure?
  3. Assessment — can they merge? overlap? redundancy?
  4. Gaps       — what parameter combinations are missing?

Also retains the legacy per-function print API for backward compatibility.
"""

from collections import defaultdict

from campaign_data.io_utils import read_tsv
from campaign_data.grid_analysis import (
    _varying_cols, _grid_set, _describe_grid, _slice_completeness,
    find_overlap_region, get_canonical_varying_order, classify_dimensions,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fmt_vals(vals):
    """Format a sorted value list: all when ≤12, else first3+last3+count."""
    if len(vals) <= 12:
        return str(vals)
    return (f"[{vals[0]}, {vals[1]}, {vals[2]}, ..., "
            f"{vals[-3]}, {vals[-2]}, {vals[-1]}] ({len(vals)} values)")


def _group_files_by_signature(reports):
    """Group files by their sorted varying column names.

    Returns list of (signature_tuple, [report_indices]).
    """
    groups = defaultdict(list)
    for i, rep in enumerate(reports):
        sig = tuple(sorted(rep.header[ci] for ci in rep.varying))
        groups[sig].append(i)
    # Sort groups by size descending
    return sorted(groups.items(), key=lambda x: -len(x[1]))


def _detect_path_signature(grid_axes, all_pts, varying_order, header,
                           path_cols=None, ratio_threshold=0.5):
    """Detect column pairs whose unique-pair count is much less than the
    Cartesian product - the signature of path-type data.

    Returns list of (ci, cj, n_pairs, n_product, ratio) for suggested
    path groups, sorted by ascending ratio (most path-like first).
    Skips columns already in *path_cols*.
    """
    if path_cols is None:
        path_cols = set()
    suggestions = []
    for i, ci in enumerate(grid_axes):
        if ci in path_cols:
            continue
        for cj in grid_axes[i + 1:]:
            if cj in path_cols:
                continue
            pairs = set()
            vals_ci = set()
            vals_cj = set()
            for gk in all_pts:
                gmap = dict(zip(varying_order, gk))
                vals_ci.add(gmap[ci])
                vals_cj.add(gmap[cj])
                pairs.add((gmap[ci], gmap[cj]))
            product = len(vals_ci) * len(vals_cj)
            if product == 0 or len(pairs) <= 1:
                continue
            ratio = len(pairs) / product
            if ratio < ratio_threshold:
                suggestions.append((ci, cj, len(pairs), product, ratio))
    suggestions.sort(key=lambda x: x[4])
    return suggestions


# ---------------------------------------------------------------------------
# Section 1 — Overview
# ---------------------------------------------------------------------------

def print_overview(reports, total_size_bytes=None):
    """Print file inventory and total data volume."""
    n_csv = sum(1 for r in reports if r.path.suffix.lower() == '.csv')
    n_txt = len(reports) - n_csv
    total_rows = sum(r.data_rows for r in reports)
    header_len = len(reports[0].header) if reports else 0
    param_cols = max(len(r.varying) + len(r.constant) for r in reports) if reports else 0

    print("=" * 70)
    print("1. Overview")
    print("=" * 70)
    print(f"  Files:        {len(reports)} ({n_csv} CSV, {n_txt} TXT)")
    if total_size_bytes is not None:
        gb = total_size_bytes / (1024 ** 3)
        print(f"  Total size:   {gb:.2f} GB" if gb >= 0.01 else
              f"  Total size:   {total_size_bytes/1024**2:.1f} MB")
    print(f"  Total rows:   {total_rows:,}")
    print(f"  Columns:      {header_len} ({param_cols} param + output)")


# ---------------------------------------------------------------------------
# Section 2 — Groups
# ---------------------------------------------------------------------------

def print_groups(reports):
    """Print per-file dimensions, compressed by structure group."""
    from campaign_data.grid_analysis import build_file_groups

    header = reports[0].header
    file_groups = build_file_groups(reports, header)

    print("\n" + "=" * 70)
    print("2. Per-file dimensions")
    print("=" * 70)

    for fg in file_groups:
        if len(fg.members) == 1:
            # Single-file group: full detail (legacy style)
            m = fg.members[0]
            rep = next(r for r in reports if r.path == m.path)
            print(f"\n  {rep.path.name}")
            print(f"    rows: {rep.data_rows:,}")
            vary_parts = []
            for ci in sorted(fg.varying):
                vals = fg.varying[ci]
                vary_parts.append(f"{header[ci]}: {_fmt_vals(vals)}")
            print(f"    varying: {' x '.join(vary_parts) if vary_parts else '(none)'}")
            print(f"    grid: {rep.actual_points:,} / {fg.expected_points:,}  [{m.tag}]")
            print(f"    eigens/pt: {rep.eigen_counts}")
            if rep.constant:
                const_parts = [f"{header[ci]}={rep.constant[ci]}"
                               for ci in sorted(rep.constant)]
                print(f"    constant: {', '.join(const_parts)}")
            if rep.duplicate_rows:
                print(f"    intra-point duplicates: {rep.duplicate_rows} rows")
        else:
            # Multi-file group: shared structure once, members compact
            vary_parts = []
            for ci in sorted(fg.varying):
                vals = fg.varying[ci]
                vary_parts.append(f"{header[ci]}: {_fmt_vals(vals)}")
            print(f"\n  [{len(fg.members)} files]  varying: "
                  f"{' x '.join(vary_parts)}")
            print(f"    expected grid: {fg.expected_points:,} pts")
            print(f"    constant across group: ", end="")
            # Find constants whose value is the SAME across all members
            common_const = {}
            for ci in set.intersection(
                    *[set(m.constant_values.keys()) for m in fg.members]):
                vals = {m.constant_values[ci] for m in fg.members}
                if len(vals) == 1:
                    common_const[ci] = vals.pop()
            if common_const:
                print(', '.join(f"{header[ci]}={v}"
                                for ci, v in sorted(common_const.items())), end="")
            print()

            # Compact member lines showing only distinguishing values
            for rep in reports:
                if rep.path not in [m.path for m in fg.members]:
                    continue
                dist = {ci: v for ci, v in rep.constant.items()
                        if ci not in common_const}
                dist_str = ', '.join(f"{header[ci]}={v}"
                                     for ci, v in sorted(dist.items()))
                print(f"    {rep.path.name}:  {dist_str}  "
                      f"{rep.actual_points:,}/{fg.expected_points:,}  "
                      f"[{rep.completeness*100:.0f}%]  "
                      f"eigens/pt={rep.eigen_counts}"
                      + (f"  DUPS={rep.duplicate_rows}" if rep.duplicate_rows else ""))

    # Structure group summary (kept for quick overview)
    if len(file_groups) > 1:
        print(f"\n  --- Structure groups (files sharing the same varying columns) ---")
        for fg in file_groups:
            sig_str = ', '.join(fg.signature)
            if len(fg.members) > 1:
                print(f"    varying=[{sig_str}]: {len(fg.members)} files")
                for m in fg.members:
                    print(f"      {m.path.name}")
            else:
                print(f"    varying=[{sig_str}]: {fg.members[0].path.name}")

        # Cross-group constant param analysis
        grid_axes_g, group_keys_g = classify_dimensions(reports)
        if group_keys_g:
            print(f"    --- constant params that VARY across groups ---")
            for ci in group_keys_g:
                vals = set()
                for rep in reports:
                    if ci in rep.constant:
                        vals.add(rep.constant[ci])
                    elif ci in rep.varying:
                        vals.update(rep.varying[ci])
                vals = sorted(vals)
                print(f"    {header[ci]}: {_fmt_vals(vals)}")


# ---------------------------------------------------------------------------
# Section 3 — Merge assessment
# ---------------------------------------------------------------------------

def print_merge_assessment(reports, overlap=None, resolutions=None,
                           varying_order=None, path_dims=None):
    """Print merge compatibility, overlap, redundancy, and merged overview."""
    if varying_order is None:
        varying_order = get_canonical_varying_order(reports)
    if overlap is None:
        overlap = find_overlap_region(reports, varying_order)
    if path_dims is None:
        path_dims = []
    # Resolve path dimension groups to column indices
    path_groups = []
    header = reports[0].header
    for group in path_dims:
        indices = []
        for name in group:
            for ci, h in enumerate(header):
                if h == name:
                    indices.append(ci)
                    break
        if len(indices) == len(group):
            path_groups.append(sorted(indices))
    path_cols = set().union(*path_groups) if path_groups else set()

    header = reports[0].header

    print("\n" + "=" * 70)
    print("3. Merge assessment")
    print("=" * 70)

    # --- 3a: Compatibility ---
    groups = _group_files_by_signature(reports)
    group_sigs = [sig for sig, _ in groups]

    if len(group_sigs) == 1:
        print(f"\n  All files share the same grid structure → compatible.")
    else:
        print(f"\n  {len(group_sigs)} distinct grid structures found:")
        for sig, indices in groups:
            print(f"    varying=[{', '.join(sig)}] : {len(indices)} files")
        print(f"  → Header alignment will be attempted during merge.")

    # --- 3b: Overlap ---
    if not overlap:
        print(f"\n  Overlap: 0 overlapping grid points.")
        print(f"  → No dedup needed. Files are disjoint or complementary.")
    else:
        overlap_pairs = defaultdict(int)
        for gk, owners in overlap.items():
            for a in range(len(owners)):
                for b in range(a + 1, len(owners)):
                    overlap_pairs[(owners[a], owners[b])] += 1
        print(f"\n  Overlap: {len(overlap):,} grid points appear in >1 file")
        print(f"  → {len(overlap_pairs)} file-pairs have overlap. Dedup will be applied.")
        # Show top 5 overlapping pairs
        for (i, j), cnt in sorted(overlap_pairs.items(), key=lambda x: -x[1])[:5]:
            pi = reports[i].path.name
            pj = reports[j].path.name
            print(f"    {pi}  x  {pj}: {cnt:,} pts")

    # --- 3c: Redundancy ---
    if overlap:
        total_covered = defaultdict(int)
        for gk, owners in overlap.items():
            for o in owners:
                total_covered[o] += 1
        redundant = []
        for i, rep in enumerate(reports):
            if total_covered.get(i, 0) >= rep.actual_points * 0.999:
                redundant.append(rep.path.name)
        if redundant:
            print(f"\n  Redundancy: {len(redundant)} file(s) fully covered by others:")
            for name in redundant:
                print(f"    {name}")
        else:
            if overlap:
                print(f"\n  Redundancy: no file fully covered by others.")
    else:
        print(f"\n  Redundancy: none (no overlap).")

    # --- 3d: Merged overview ---
    grid_axes, group_keys = classify_dimensions(reports)
    total_rows = sum(r.data_rows for r in reports)

    # Count actual unique canonical grid points
    from collections import defaultdict as dd
    all_pts = dd(list)
    for ri, rep in enumerate(reports):
        cmap = {i: rep.constant[i] for i in varying_order if i in rep.constant}
        for pt in rep.grid_set:
            pm = dict(zip(sorted(rep.varying.keys()), pt))
            gk = tuple(pm[i] if i in pm else cmap[i] for i in varying_order)
            all_pts[gk].append(ri)
    actual_pts = len(all_pts)

    # Compute merged expected points — based on varying_order (canonical),
    # with path groups collapsed to unique tuple count
    merged_expected = 1
    for ci in varying_order:
        vals = set()
        for r in reports:
            if ci in r.varying: vals.update(r.varying[ci])
            elif ci in r.constant: vals.add(r.constant[ci])
        merged_expected *= len(vals)

    for group in path_groups:
        group_in_varying = [ci for ci in group if ci in varying_order]
        if len(group_in_varying) >= 2:
            prod = 1
            for ci in group_in_varying:
                vals = set()
                for r in reports:
                    if ci in r.varying: vals.update(r.varying[ci])
                    elif ci in r.constant: vals.add(r.constant[ci])
                prod *= len(vals)
            tuples = set()
            for gk in all_pts:
                gmap = dict(zip(varying_order, gk))
                tuples.add(tuple(gmap.get(ci) for ci in group_in_varying))
            n_unique = len(tuples)
            merged_expected = merged_expected // prod * n_unique

    print(f"\n  --- Merged result ---")
    print(f"  Total rows:    {total_rows:,}")
    pct_str = f"({actual_pts/merged_expected*100:.1f}%)" if merged_expected else ""
    print(f"  Total grid pts: {actual_pts:,} / {merged_expected:,} {pct_str}")
    print(f"  Dimensions:    {len(varying_order)}")

    # Show dimensions, grouping path axes
    shown_path = set()
    for group in path_groups:
        group_in_axes = [ci for ci in group if ci in grid_axes]
        if len(group_in_axes) >= 2:
            names = " + ".join(header[ci] for ci in group_in_axes)
            tuples = set()
            for gk in all_pts:
                gmap = dict(zip(varying_order, gk))
                tuples.add(tuple(gmap.get(ci) for ci in group_in_axes))
            print(f"    [path axis]  ({names}): {len(tuples)} unique pairs")
            shown_path.update(group_in_axes)

    for ci in grid_axes:
        if ci in shown_path:
            continue
        vals = set()
        for r in reports:
            if ci in r.varying: vals.update(r.varying[ci])
            elif ci in r.constant: vals.add(r.constant[ci])
        vals = sorted(vals)
        print(f"    [grid axis]  {header[ci]}: {_fmt_vals(vals)}")

    for ci in group_keys:
        vals = set()
        for r in reports:
            if ci in r.constant: vals.add(r.constant[ci])
            elif ci in r.varying: vals.update(r.varying[ci])
        vals = sorted(vals)
        print(f"    [group key]  {header[ci]}: {_fmt_vals(vals)}")

    # --- 3e: Grid regularity ---
    # Check: for each non-path grid axis, does every value have the same
    # sub-grid size?  Irregularities flag non-rectangular data.
    regular_axes = [ci for ci in grid_axes if ci not in path_cols]
    irregular = []
    for ci in regular_axes:
        vals = set()
        for r in reports:
            if ci in r.varying: vals.update(r.varying[ci])
            elif ci in r.constant: vals.add(r.constant[ci])
        vals = sorted(vals)
        other = [c for c in varying_order if c != ci]
        by_val = {}
        for gk in all_pts:
            gmap = dict(zip(varying_order, gk))
            v = gmap[ci]
            if v not in by_val:
                by_val[v] = set()
            by_val[v].add(tuple(gmap[c] for c in other))

        if by_val:
            sizes = {len(s) for s in by_val.values()}
            if len(sizes) > 1:
                # Show the irregularity pattern
                min_sz, max_sz = min(sizes), max(sizes)
                min_vals = [v for v in vals if v in by_val and len(by_val[v]) == min_sz]
                max_vals = [v for v in vals if v in by_val and len(by_val[v]) == max_sz]
                name = header[ci]
                nb = len(by_val)
                print(f"\n  Grid regularity: {name} has irregular coverage")
                print(f"    sub-grid sizes: {sorted(sizes)}")
                if len(min_vals) <= 6:
                    print(f"    values with {min_sz} sub-points: {min_vals}")
                else:
                    print(f"    {len(min_vals)} values have {min_sz} sub-points "
                          f"[{min_vals[0]}, ..., {min_vals[-1]}]")
                if len(max_vals) <= 6:
                    print(f"    values with {max_sz} sub-points: {max_vals}")
                else:
                    print(f"    {len(max_vals)} values have {max_sz} sub-points")
                irregular.append(ci)

    if irregular:
        # Irregularity may indicate path-type data the user hasn't declared.
        # Suggest path_dims pairs whose unique-pair count is much less than
        # the Cartesian product.
        suggestions = _detect_path_signature(
            grid_axes, all_pts, varying_order, header, path_cols)
        if suggestions:
            print(f"\n  -> Path-axis suggestion: irregular coverage may indicate "
                  f"path-type data (k-space line, cross, diagonal, etc.).")
            for ci, cj, n_pairs, n_product, ratio in suggestions:
                print(f"     ({header[ci]}, {header[cj]}): {n_pairs:,} unique pairs "
                      f"vs {n_product:,} Cartesian product ({ratio*100:.1f}%)")
            suggested = [f'("{header[ci]}", "{header[cj]}")'
                         for ci, cj, _, _, _ in suggestions]
            print(f"     try: smart_merge(..., path_dims=[{', '.join(suggested)}])")

    if not irregular:
        # Check path axes too
        if path_groups:
            print(f"\n  Grid regularity: Cartesian product of")
            parts = []
            for group in path_groups:
                gi = [ci for ci in group if ci in grid_axes]
                if gi:
                    parts.append(f"[path ({'+'.join(header[ci] for ci in gi)})]")
            for ci in regular_axes:
                parts.append(f"[{header[ci]}]")
            print(f"    {' × '.join(parts)} appears fully populated" if parts else "")

    # Size estimate (rough: 27 cols × ~30 bytes/cell)
    est_mb = total_rows * 27 * 30 / (1024 * 1024)
    print(f"\n  Estimated merged file size: ~{est_mb:.0f} MB")

    # Intra-point duplicates
    total_dup_rows = sum(r.duplicate_rows for r in reports)
    dup_files = [r for r in reports if r.duplicate_rows > 0]
    if dup_files:
        print(f"\n  Intra-point duplicates: {total_dup_rows:,} rows across "
              f"{len(dup_files)} files")
        for r in dup_files[:3]:
            pct = r.duplicate_rows / r.data_rows * 100
            print(f"    {r.path.name}: {r.duplicate_rows} rows ({pct:.1f}%)")
        if len(dup_files) > 3:
            print(f"    ... and {len(dup_files) - 3} more files")

    # Dedup decisions summary
    if resolutions:
        print(f"\n  Dedup: {len(resolutions):,} grid-point decisions made.")


# ---------------------------------------------------------------------------
# Section 4 — Gaps
# ---------------------------------------------------------------------------

def print_gaps(reports, path_dims=None):
    """Print per-dimension coverage — which values have data, which don't.

    Gap analysis is done on grid_axes only. Group keys are listed but
    not included in sub-point calculations (they are not grid dimensions).
    Path dimension groups are treated as a single axis for sub-point calc.
    """
    if path_dims is None:
        path_dims = []
    grid_axes, group_keys = classify_dimensions(reports)
    varying_order = get_canonical_varying_order(reports)
    header = reports[0].header

    # Resolve path groups
    path_groups = []
    for group in path_dims:
        indices = sorted(header.index(name) if name in header else -1
                        for name in group)
        if all(i >= 0 for i in indices):
            path_groups.append([i for i in indices if i in grid_axes])
    path_cols = set().union(*path_groups) if path_groups else set()

    print("\n" + "=" * 70)
    print("4. Gaps — per-dimension coverage")
    print("=" * 70)

    # Build canonical point set for coverage analysis
    all_pts = set()
    for rep in reports:
        cmap = {i: rep.constant[i] for i in varying_order if i in rep.constant}
        for pt in rep.grid_set:
            pm = dict(zip(sorted(rep.varying.keys()), pt))
            gk = tuple(pm[i] if i in pm else cmap[i] for i in varying_order)
            all_pts.add(gk)

    if not grid_axes:
        print("\n  No grid axes to analyze.")
        return

    # Path group summaries - shown once per group, since individual member
    # columns are skipped in the per-axis loop below. Path groups have no
    # well-defined "expected" pair count (the path shape is project-specific),
    # so we report observed pair count without a completeness ratio.
    for group in path_groups:
        if len(group) < 2:
            continue
        tuples = set()
        for gk in all_pts:
            gmap = dict(zip(varying_order, gk))
            tuples.add(tuple(gmap[ci] for ci in group))
        names = " + ".join(header[ci] for ci in group)
        print(f"\n  ({names}) [path]: {len(tuples):,} unique pairs across all files")

    # Analyze each grid axis — use only other grid_axes for sub-point calc
    # Skip individual columns that are part of a path group (shown together)
    shown = set()
    for ci in grid_axes:
        if ci in path_cols:
            continue
        shown.add(ci)
        vals = set()
        for rep in reports:
            if ci in rep.varying:
                vals.update(rep.varying[ci])
            elif ci in rep.constant:
                vals.add(rep.constant[ci])
        vals = sorted(vals)

        other_axes = [c for c in grid_axes if c != ci]
        by_val = defaultdict(set)
        for gk in all_pts:
            gmap = dict(zip(varying_order, gk))
            by_val[gmap[ci]].add(tuple(gmap[c] for c in other_axes))

        with_data = [v for v in vals if v in by_val]
        without_data = [v for v in vals if v not in by_val]

        name = header[ci]
        print(f"\n  {name}: {len(with_data)}/{len(vals)} values have data")
        if without_data:
            if len(without_data) <= 6:
                print(f"    missing: {without_data}")
            else:
                print(f"    missing: [{without_data[0]}, ..., {without_data[-1]}] ({len(without_data)} values)")
        else:
            print(f"    all values covered")

        # Slice completeness based on other grid_axes only. Path groups in
        # other_axes contribute their unique pair count (not the Cartesian
        # product of member cardinalities).
        if len(other_axes) >= 1:
            other_total = 1
            for c in other_axes:
                if c in path_cols:
                    continue  # handled as path group below
                ov = set()
                for rep in reports:
                    if c in rep.varying: ov.update(rep.varying[c])
                    elif c in rep.constant: ov.add(rep.constant[c])
                other_total *= len(ov)
            # Multiply by unique pair count for each path group in other_axes
            for group in path_groups:
                members_in_other = [cj for cj in group
                                    if cj in other_axes and cj in path_cols]
                if members_in_other:
                    tuples = set()
                    for gk in all_pts:
                        gmap = dict(zip(varying_order, gk))
                        tuples.add(tuple(gmap[cj] for cj in members_in_other))
                    other_total *= len(tuples)
            incomplete_slices = []
            for v in with_data:
                n = len(by_val[v])
                if n < other_total:
                    incomplete_slices.append((v, n, other_total))
            if incomplete_slices:
                _slice_examples = incomplete_slices[:5]
                for v, n, total in _slice_examples:
                    print(f"    {name}={v}: {n}/{total} sub-points ({n/total*100:.0f}%)")
                if len(incomplete_slices) > 5:
                    print(f"    ... and {len(incomplete_slices)-5} more incomplete slices")

    # Group keys — per-group value distribution, detect gaps within each group
    if group_keys:
        print(f"\n  Group key coverage (per group):")
        groups = _group_files_by_signature(reports)
        for ci in group_keys:
            name = header[ci]
            print(f"\n    {name}:")
            all_union = set()

            for sig, indices in groups:
                reps = [reports[i] for i in indices]
                vals = set()
                for r in reps:
                    if ci in r.constant:
                        vals.add(r.constant[ci])
                    elif ci in r.varying:
                        vals.update(r.varying[ci])
                vals = sorted(vals)
                all_union.update(vals)

                if len(vals) <= 1:
                    print(f"      Group varying=[{', '.join(sig)}]: "
                          f"{len(vals)} value ({list(vals)[0] if vals else 'none'})")
                    continue

                # Detect step and gaps within this group's values
                diffs = [round(vals[i+1]-vals[i], 10) for i in range(len(vals)-1)]
                if diffs:
                    from collections import Counter
                    dom = Counter(diffs).most_common(1)[0][0]
                    gaps = []
                    for i, d in enumerate(diffs):
                        if abs(d - dom) > 1e-9:
                            m = round(d / dom) - 1
                            if m > 0:
                                for k in range(1, m + 1):
                                    gaps.append(round(vals[i] + k * dom, 10))

                    label = f"Group varying=[{', '.join(sig)}]"
                    print(f"      {label}: {len(vals)} values "
                          f"[{vals[0]}, {vals[-1]}], step {dom}")
                    if gaps:
                        ng = len(gaps)
                        if ng <= 8:
                            print(f"        {ng} gaps: {gaps}")
                        else:
                            print(f"        {ng} gaps: [{gaps[0]}, {gaps[1]}, ..., {gaps[-2]}, {gaps[-1]}]")
                        # Detect gap rhythm
                        if len(gaps) >= 2:
                            gd = {round(gaps[i+1]-gaps[i], 10) for i in range(len(gaps)-1)}
                            if len(gd) == 1:
                                every = round(gd.pop() / dom)
                                print(f"        pattern: every {every}th skipped")
                    else:
                        print(f"        no gaps")
                else:
                    print(f"      Group varying=[{', '.join(sig)}]: "
                          f"{len(vals)} values")

            # Combined summary
            all_union = sorted(all_union)
            if len(all_union) > 1:
                udiffs = [round(all_union[i+1]-all_union[i], 10) for i in range(len(all_union)-1)]
                if udiffs:
                    from collections import Counter
                    udom = Counter(udiffs).most_common(1)[0][0]
                    ugaps = sum(1 for d in udiffs if abs(d - udom) > 1e-9)
                    print(f"      Combined: {len(all_union)} values "
                          f"[{all_union[0]}, {all_union[-1]}], step {udom}"
                          + (f", no gaps" if ugaps == 0 else f", {ugaps} gaps"))


# ---------------------------------------------------------------------------
# Legacy function wrappers — kept for backward compatibility with merge.py
# ---------------------------------------------------------------------------

def print_per_file_report(reports):
    """Legacy: print one-line-per-file summary."""
    print("=" * 70)
    print("Per-file grid analysis")
    print("=" * 70)
    for rep in reports:
        dim_str = _describe_grid(rep)
        tag = "OK" if rep.completeness >= 1.0 else f"{rep.completeness*100:.1f}% full"
        print(f"\n  {rep.path.name}")
        print(f"    rows:       {rep.data_rows:,}")
        print(f"    grid dims:  {dim_str}")
        print(f"    grid:       {rep.actual_points:,} / {rep.expected_points:,}  [{tag}]")
        print(f"    eigens/pt:  {rep.eigen_counts}")


def print_overlap_report(reports, overlap=None):
    """Legacy: aggregated overlap summary."""
    print("\n" + "=" * 70)
    print("Overlap analysis")
    print("=" * 70)

    if overlap is None:
        overlap = find_overlap_region(reports)
    if not overlap:
        print("\n  No overlapping grid points -- files are disjoint.")
        return

    overlap_pairs = defaultdict(int)
    for gk, owners in overlap.items():
        for a in range(len(owners)):
            for b in range(a + 1, len(owners)):
                overlap_pairs[(owners[a], owners[b])] += 1

    def _group_key(path):
        name = path.stem
        for pattern in ['batch_rsl_delta_factor', 'batch_rsl_h_grating',
                        'Flat(600Q-90f-3e6)-fill-h_space', 'Flat(600Q-90f-3e6)-kxky',
                        'FP_Void_PEC', 'FP_Grating_PEC', 'batch_rsl_substrate']:
            if name.startswith(pattern):
                return pattern
        return name[:30]

    groups = defaultdict(list)
    for i, rep in enumerate(reports):
        groups[_group_key(rep.path)].append(i)

    print("\n  Overlap summary:")
    shown = set()
    for (i, j), cnt in sorted(overlap_pairs.items(), key=lambda x: -x[1]):
        gi = _group_key(reports[i].path)
        gj = _group_key(reports[j].path)
        key = (min(i, j), max(i, j))
        if key in shown:
            continue
        shown.add(key)
        pct_i = cnt / reports[i].actual_points * 100
        pct_j = cnt / reports[j].actual_points * 100
        ni = len(groups[gi])
        nj = len(groups[gj])
        i_label = f"{gi} ({ni} files)" if ni > 1 else reports[i].path.name
        j_label = f"{gj} ({nj} files)" if nj > 1 else reports[j].path.name
        pct_str = ""
        if pct_i >= 99.9:
            pct_str = f" — {reports[j].path.name} 完全被覆盖"
        elif pct_j >= 99.9:
            pct_str = f" — {reports[i].path.name} 完全被覆盖"
        else:
            pct_str = f" ({pct_i:.0f}% / {pct_j:.0f}% of each)"
        print(f"    {reports[i].path.name}")
        print(f"    x {reports[j].path.name}")
        print(f"    -> {cnt:,} pts{pct_str}")
        print()


def print_gap_summary(reports, varying_order=None):
    """Legacy: pre-merge gap analysis."""
    from campaign_data.grid_analysis import get_canonical_varying_order as _g
    if varying_order is None:
        varying_order = _g(reports)
    print_gaps(reports)


def print_redundancy_warnings(reports, overlap):
    """Legacy: redundancy warnings."""
    if not overlap:
        return
    total_covered = defaultdict(int)
    for gk, owners in overlap.items():
        for o in owners:
            total_covered[o] += 1
    print("\n" + "=" * 70)
    print("Redundancy check")
    print("=" * 70)
    warnings = []
    for i, rep in enumerate(reports):
        if total_covered.get(i, 0) >= rep.actual_points * 0.999:
            warnings.append(f"  {rep.path.name}: {rep.actual_points:,} pts 全部被其他文件覆盖, 可考虑移除")
    if warnings:
        print("")
        for w in warnings:
            print(w)
    else:
        print("\n  No fully redundant files detected.")


def print_dedup_decisions(resolutions, reports, overlap, densities=None,
                         varying_order=None):
    """Legacy: dedup decision details."""
    if not resolutions:
        print("\n  No deduplication decisions (no overlaps to resolve).")
        return
    from campaign_data.dedup import dedup_summary
    decisions = dedup_summary(resolutions, reports, overlap,
                              densities=densities, varying_order=varying_order)
    print("\n" + "=" * 70)
    print("Deduplication decisions")
    print("=" * 70)
    for dec in decisions:
        print(f"\n  Resolving overlap on: {dec.anchor_col_name} (col {dec.anchor_col})")
        for av, choice in sorted(dec.resolutions.items()):
            print(f"    {dec.anchor_col_name}={av}: keep from file[{choice}]")
        if dec.rationale:
            print(f"    Rationale: {dec.rationale}")


def _check_regularity_in_memory(header, data, header_len, path_dims=None):
    """Core regularity check operating on in-memory rows.

    Returns (expected, actual, completeness_pct, is_complete, gap_lines).
    """
    from campaign_data.grid_analysis import _resolve_path_dims

    varying = _varying_cols(data, header_len)
    if not varying:
        return None

    expected = 1
    for v in varying.values():
        expected *= len(v)

    _path_groups = _resolve_path_dims(path_dims, header) if path_dims else []
    if _path_groups:
        for group in _path_groups:
            group_in_varying = [ci for ci in group if ci in varying]
            if len(group_in_varying) >= 2:
                prod = 1
                for ci in group_in_varying:
                    prod *= len(varying[ci])
                tuples = set()
                for r in data:
                    from campaign_data.io_utils import fv
                    tuples.add(tuple(fv(r[ci]) for ci in group_in_varying))
                expected = expected // prod * len(tuples)

    actual = len(_grid_set(data, varying))
    pct = actual / expected * 100 if expected else 100.0
    is_complete = pct >= 99.99
    gap_lines = None if is_complete else _slice_completeness(data, varying, header, header_len)

    return expected, actual, pct, is_complete, varying, gap_lines


def print_merged_regularity(out_path=None, path_dims=None, *,
                            rows=None, header=None, header_len=None):
    """Check whether the merged output forms a regular Cartesian grid.

    Accepts either a file path (*out_path*) or in-memory data
    (*rows*, *header*, *header_len*) to avoid an extra I/O round-trip.
    """
    if rows is not None:
        # In-memory path (no file I/O)
        if header_len is None:
            header_len = len(header)
    else:
        from campaign_data.io_utils import detect_header_len
        header_len = detect_header_len(out_path)
        rows = read_tsv(out_path)
        header = [c.strip() for c in rows[0]]
        rows = rows[1:]

    data = rows[1:] if header is None else rows
    # Normalise: header is list[str], data is list[list[str]]
    if header is None:
        header = [c.strip() for c in rows[0]]
        data = rows[1:]
    else:
        data = rows

    result = _check_regularity_in_memory(header, data, header_len, path_dims)
    if result is None:
        print("\n  No varying parameters found -- cannot check grid.")
        return

    expected, actual, pct, is_complete, varying, gap_lines = result

    print("\n" + "=" * 70)
    print("Merged-grid regularity check")
    print("=" * 70)

    dim_str = _describe_grid(varying=varying, header=header)
    print(f"\n  Merged grid: {dim_str}")
    print(f"  Expected points: {expected:,}")
    print(f"  Actual points:   {actual:,}")
    print(f"  Completeness:    {pct:.2f}%")

    if is_complete:
        print("\n  >> The merged dataset IS a complete regular grid.")
    else:
        print(f"\n  >> The merged dataset is NOT a complete grid ({expected - actual:,} missing).")
        if gap_lines:
            print("\n  Gap summary (per-dimension slice completeness):")
            for line in gap_lines:
                print(line)
