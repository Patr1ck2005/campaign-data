"""Merge orchestrator: smart merge (with dedup) and simple merge (concatenation)."""

from pathlib import Path
from dataclasses import dataclass

from campaign_data.io_utils import (
    read_tsv, read_csv, read_table_rows, write_tsv, collect_paths, HEADER_LEN,
)
from campaign_data.grid_analysis import (
    analyse_files, find_overlap_region, get_canonical_varying_order,
    classify_dimensions,
)
from campaign_data.dedup import resolve_cross_file_overlaps, iter_apply_dedup
from campaign_data.reporting import (
    print_overview, print_groups, print_merge_assessment, print_gaps,
    print_merged_regularity,
)


def _align_headers(paths, header_len):
    """Reconcile column order across files so headers align.

    When the same scan is exported with different column permutations,
    this reorders non-reference files to match the first file's parameter-column
    order.  Returns the adjusted path list, or None if column *sets* differ.

    Only reads the full file when alignment is actually needed (header mismatch).
    For files whose headers already match, only the header row is skimmed.
    """
    paths = [Path(p) for p in paths]
    if len(paths) < 2:
        return list(paths)

    from campaign_data.io_utils import _stream_head_rows

    # Read only the header row of the reference file
    ref_h, _ = _stream_head_rows(paths[0], 1)
    ref_header = [c.strip() for c in ref_h]
    ref_set = set(ref_header[:header_len])

    aligned_paths = [paths[0]]
    for p in paths[1:]:
        # Skim header only — avoid reading the full file unless needed
        p_h, _ = _stream_head_rows(p, 1)
        h = [c.strip() for c in p_h]

        if h[:header_len] == ref_header[:header_len]:
            aligned_paths.append(p)
            continue

        if set(h[:header_len]) != ref_set:
            print(f"[ERROR] Header mismatch: {p.name}")
            print(f"  Expected: {ref_header[:header_len]}")
            print(f"  Got:      {h[:header_len]}")
            return None

        # Header sets match but order differs — full read + reorder + write
        p_rows = read_table_rows(p)
        reorder = [h.index(name) for name in ref_header[:header_len]]
        reorder.extend(range(header_len, len(h)))
        aligned_rows = [[row[i] for i in reorder] for row in p_rows]

        aligned_path = p.parent / (p.stem + "_aligned.txt")
        write_tsv(aligned_path, aligned_rows, clean_floats=False)
        print(f"  [align] {p.name} -> {aligned_path.name}")
        aligned_paths.append(aligned_path)
    return aligned_paths


@dataclass
class AnalysisResult:
    """Structured result of analyse_directory. Consumed by format_merge_report
    and execute_merge / execute_split."""
    sources: list
    paths: list                   # aligned paths (may include _aligned.txt temp files)
    aligned_paths: list           # temp files to clean up after execution
    reports: list                 # list[GridReport]
    varying_order: list           # canonical varying column indices
    overlap: dict                 # {grid_key: [file_indices]} from find_overlap_region
    resolutions: dict             # {grid_key: winning_file_index} from resolve_cross_file_overlaps
    densities: dict               # per-file slice densities (for dedup_summary)
    header_len: int
    path_dims: list               # resolved path_dims (always a list, never None)
    all_data: dict | None         # {file_index: [rows]}; None in dry_run mode
    orig_paths: list              # original paths (pre-alignment) for size calculation
    total_size: int               # total bytes of original files


def analyse_directory(sources, *, header_len=None, path_dims=None,
                      use_cache=True, parallel=False, float_rnd=None,
                      float_rnd_mode=None, dup_freq_tolerance=None,
                      recursive=False, return_data=True):
    """Analyse sources and return structured AnalysisResult.

    This is the analysis-only phase of smart_merge: path collection, header
    alignment, per-file grid analysis (with caching), canonical varying order,
    overlap detection, and cross-file dedup resolution.  No reports printed,
    no files written.

    Does NOT override module-level FLOAT_RND/ROUND_MODE.  Callers that need
    the override (e.g. smart_merge for write_tsv compatibility) must manage
    it themselves and pass float_rnd/float_rnd_mode here for cache keying.

    Args:
        sources: list of directory or file paths
        header_len: number of grid-parameter columns (auto-detect if None)
        path_dims: path-axis column groups, e.g. [("m1","m2")]. None -> [].
        use_cache: use per-file GridReport disk cache
        parallel: multiprocessing for analyse_files
        float_rnd: FLOAT_RND override (for cache keying only)
        float_rnd_mode: ROUND_MODE override (for cache keying only)
        dup_freq_tolerance: intra-point block-duplicate tolerance
        recursive: scan subdirectories
        return_data: if True, retain in-memory row data for execution phase;
                     if False, all_data is None (dry-run mode)

    Returns AnalysisResult, or None if no files are found or header alignment
    fails. A single file is valid for audit/index consumers; ``smart_merge``
    separately requires at least two files.
    """
    import campaign_data.io_utils as _iou
    from campaign_data.io_utils import detect_header_len
    from campaign_data.directory_index import (
        _load_report_cache, _save_report_cache,
    )

    if path_dims is None:
        path_dims = []

    paths = collect_paths(sources, recursive=recursive)
    if not paths:
        print("No CSV/TXT files found. Check sources.")
        return None

    print(f"Found {len(paths)} files to analyse.")

    if header_len is None:
        header_len = detect_header_len(paths[0])

    paths = _align_headers(paths, header_len)
    if paths is None:
        return None

    aligned_paths = [p for p in paths if p.stem.endswith("_aligned")]

    # ---- analysis (with data retention for merge phase) --------------
    cache_hits = 0
    if use_cache:
        first = Path(sources[0])
        cache_root = first if first.is_dir() else first.parent

        cached_map = {}
        uncached_idx = []
        all_data = {}
        for fi, p in enumerate(paths):
            rep = _load_report_cache(cache_root, p, header_len, _iou.FLOAT_RND,
                                     _iou.ROUND_MODE, dup_freq_tolerance)
            if rep is not None:
                cached_map[fi] = rep
                cache_hits += 1
            else:
                uncached_idx.append((fi, p))

        if uncached_idx:
            uc_paths = [p for _, p in uncached_idx]
            if return_data:
                new_reports, new_data = analyse_files(
                    uc_paths, header_len, return_data=True,
                    parallel=parallel, path_dims=path_dims,
                    dup_freq_tolerance=dup_freq_tolerance)
                for (fi, _), rep in zip(uncached_idx, new_reports):
                    cached_map[fi] = rep
                    _save_report_cache(cache_root, rep, header_len, _iou.FLOAT_RND,
                                       _iou.ROUND_MODE, dup_freq_tolerance)
                all_data.update(
                    {fi: new_data[i] for i, (fi, _)
                     in enumerate(uncached_idx)})
            else:
                new_reports = analyse_files(
                    uc_paths, header_len, return_data=False,
                    parallel=parallel, path_dims=path_dims,
                    dup_freq_tolerance=dup_freq_tolerance)
                for (fi, _), rep in zip(uncached_idx, new_reports):
                    cached_map[fi] = rep
                    _save_report_cache(cache_root, rep, header_len, _iou.FLOAT_RND,
                                       _iou.ROUND_MODE, dup_freq_tolerance)

        if return_data:
            for fi in cached_map:
                if fi not in all_data:
                    all_data[fi] = read_table_rows(paths[fi])[1:]

        reports = [cached_map[fi] for fi in sorted(cached_map)]
        if cache_hits:
            print(f"  [cache] {cache_hits}/{len(paths)} reports from cache")
    else:
        if return_data:
            reports, all_data = analyse_files(
                paths, header_len, return_data=True, parallel=parallel,
                path_dims=path_dims, dup_freq_tolerance=dup_freq_tolerance)
        else:
            reports = analyse_files(
                paths, header_len, return_data=False, parallel=parallel,
                path_dims=path_dims, dup_freq_tolerance=dup_freq_tolerance)
            all_data = None

    varying_order = get_canonical_varying_order(reports)
    overlap = find_overlap_region(reports, varying_order)

    if overlap:
        resolutions, densities = resolve_cross_file_overlaps(
            reports, overlap, varying_order, return_densities=True)
    else:
        resolutions, densities = {}, {}

    orig_paths = collect_paths(sources, recursive=recursive)
    total_size = sum(p.stat().st_size for p in orig_paths)

    return AnalysisResult(
        sources=sources,
        paths=paths,
        aligned_paths=aligned_paths,
        reports=reports,
        varying_order=varying_order,
        overlap=overlap,
        resolutions=resolutions,
        densities=densities,
        header_len=header_len,
        path_dims=path_dims,
        all_data=all_data if return_data else None,
        orig_paths=orig_paths,
        total_size=total_size,
    )


def smart_merge(sources, out_path=None, clean_floats=True, header_len=None,
                dry_run=False, use_cache=True, parallel=False,
                split_by=None, split_out_dir=None, float_rnd=None,
                path_dims=None, recursive=False, float_rnd_mode=None,
                dup_freq_tolerance=None):
    """Smart merge with interruption-aware deduplication.

    Pipeline:
    1. collect_paths(sources) — expand directories to file list
    2. analyse_files(paths) — per-file grid analysis
    3. resolve_cross_file_overlaps(reports) — find overlaps, decide which file wins per slice
    4. Print 4-section analysis report
    5. apply_dedup(data, decisions) — produce deduplicated row list
    6. write_tsv(out_path, merged_rows) — output (or split if split_by set)
    7. print_merged_regularity(out_path) — verify result

    Args:
        sources: list of directory paths or file paths
        out_path: output file path. If None, auto-writes to sources[0]/merged.txt
        clean_floats: if True, clean float noise in output cells
        header_len: number of grid-parameter columns
        dry_run: if True, analyse and report only (no file written)
        use_cache: if True, use per-file GridReport disk cache
        parallel: if True, use multiprocessing for analyse_files
        split_by: list of column indices/names — if set, split instead of merge
        split_out_dir: output directory for split files
        float_rnd: override FLOAT_RND (use 8 for legacy contaminated data)
        recursive: if True, scan subdirectories of source dirs (default False)
        float_rnd_mode: override ROUND_MODE — "round" or "truncate"

    Returns:
        Path to output file, SplitResult, dict (dry_run), or None on error.
    """
    # ---- resolve path_dims default -----------------------------------
    # Default: no path dims (pure Cartesian). Path-type data will trigger
    # "irregular coverage" in the report with a path_dims suggestion.
    # Users with path-type data must pass path_dims=[("col1","col2"), ...] explicitly.
    if path_dims is None:
        path_dims = []

    # ---- override FLOAT_RND / ROUND_MODE ----------------------------------
    import campaign_data.io_utils as _iou
    _saved_fr = None
    _saved_mode = None
    if float_rnd is not None:
        _saved_fr = _iou.FLOAT_RND
        _iou.FLOAT_RND = float_rnd
    if float_rnd_mode is not None:
        _saved_mode = _iou.ROUND_MODE
        _iou.ROUND_MODE = float_rnd_mode

    try:
        paths = collect_paths(sources, recursive=recursive)
        if len(paths) < 2:
            print("Need at least 2 files to merge. Check sources.")
            return None

        print(f"Found {len(paths)} files to merge.")

        # ---- auto-detect header_len if not specified --------------------------
        if header_len is None:
            from campaign_data.io_utils import detect_header_len
            header_len = detect_header_len(paths[0])

        # ---- align headers ---------------------------------------------------
        paths = _align_headers(paths, header_len)
        if paths is None:
            return None

        _aligned_paths = [p for p in paths if p.stem.endswith('_aligned')]

        try:
            # ---- analysis (with data retention for merge phase) --------------
            cache_hits = 0
            if use_cache:
                from campaign_data.directory_index import (
                    _load_report_cache, _save_report_cache,
                )
                first = Path(sources[0])
                cache_root = first if first.is_dir() else first.parent

                cached_map = {}
                uncached_idx = []
                all_data = {}
                for fi, p in enumerate(paths):
                    rep = _load_report_cache(cache_root, p, header_len, _iou.FLOAT_RND, _iou.ROUND_MODE, dup_freq_tolerance)
                    if rep is not None:
                        cached_map[fi] = rep
                        cache_hits += 1
                    else:
                        uncached_idx.append((fi, p))

                if uncached_idx:
                    uc_paths = [p for _, p in uncached_idx]
                    if dry_run:
                        new_reports = analyse_files(
                            uc_paths, header_len, return_data=False,
                            parallel=parallel, path_dims=path_dims,
                            dup_freq_tolerance=dup_freq_tolerance)
                        for (fi, _), rep in zip(uncached_idx, new_reports):
                            cached_map[fi] = rep
                            _save_report_cache(cache_root, rep, header_len, _iou.FLOAT_RND, _iou.ROUND_MODE, dup_freq_tolerance)
                    else:
                        new_reports, new_data = analyse_files(
                            uc_paths, header_len, return_data=True,
                            parallel=parallel, path_dims=path_dims,
                            dup_freq_tolerance=dup_freq_tolerance)
                        for (fi, _), rep in zip(uncached_idx, new_reports):
                            cached_map[fi] = rep
                            _save_report_cache(cache_root, rep, header_len, _iou.FLOAT_RND, _iou.ROUND_MODE, dup_freq_tolerance)
                        all_data.update(
                            {fi: new_data[i] for i, (fi, _)
                             in enumerate(uncached_idx)})

                if not dry_run:
                    for fi in cached_map:
                        if fi not in all_data:
                            all_data[fi] = read_table_rows(paths[fi])[1:]

                reports = [cached_map[fi] for fi in sorted(cached_map)]
                if cache_hits:
                    print(f"  [cache] {cache_hits}/{len(paths)} reports from cache")
            else:
                if dry_run:
                    reports = analyse_files(paths, header_len, return_data=False,
                                            parallel=parallel, path_dims=path_dims,
                                            dup_freq_tolerance=dup_freq_tolerance)
                    all_data = {}
                else:
                    reports, all_data = analyse_files(
                        paths, header_len, return_data=True, parallel=parallel,
                        path_dims=path_dims,
                        dup_freq_tolerance=dup_freq_tolerance)

            # ---- canonical varying order (computed once) --------------------
            varying_order = get_canonical_varying_order(reports)

            # ---- 4-section report (overview, groups, merge, gaps) ----------
            orig_paths = collect_paths(sources, recursive=recursive)
            total_size = sum(p.stat().st_size for p in orig_paths)
            print_overview(reports, total_size)
            print_groups(reports)

            overlap = find_overlap_region(reports, varying_order)

            if overlap:
                resolutions, densities = resolve_cross_file_overlaps(
                    reports, overlap, varying_order, return_densities=True)
            else:
                resolutions, densities = {}, {}

            print_merge_assessment(reports, overlap, resolutions,
                                   varying_order=varying_order,
                                   path_dims=path_dims)
            print_gaps(reports, path_dims=path_dims)

            if dry_run:
                print("\n  [dry_run] No output written.")
                grid_axes, group_keys = classify_dimensions(reports)
                from campaign_data.splitter import analyze_split_options
                return {
                    "paths": paths,
                    "reports": reports,
                    "overlap": overlap,
                    "resolutions": resolutions,
                    "varying_order": varying_order,
                    "header_len": header_len,
                    "grid_axes": grid_axes,
                    "group_keys": group_keys,
                    "split_options": analyze_split_options(
                        reports, varying_order),
                }

            # ---- resolve output path ---------------------------------------
            if out_path is None:
                first = Path(sources[0])
                base = first if first.is_dir() else first.parent
                out_path = base / "merged.txt"
            else:
                out_path = Path(out_path)

            # ---- split mode ------------------------------------------------
            if split_by:
                from campaign_data.splitter import plan_split, execute_split
                from campaign_data.grid_analysis import remove_intra_point_duplicates

                # Apply intra-point dedup to in-memory data
                if all_data:
                    for fi in list(all_data):
                        rep = reports[fi]
                        if rep.duplicate_rows:
                            all_data[fi] = remove_intra_point_duplicates(
                                all_data[fi], rep.varying, rep.header,
                                dup_freq_tolerance=dup_freq_tolerance)

                plan = plan_split(reports, split_by, varying_order=varying_order)
                split_dir = (Path(split_out_dir) if split_out_dir
                             else out_path.parent / "split")
                if resolutions:
                    result = execute_split(reports, plan, split_dir,
                                           all_data=all_data,
                                           resolutions=resolutions,
                                           varying_order=varying_order,
                                           clean_floats=clean_floats)
                else:
                    result = execute_split(reports, plan, split_dir,
                                           all_data=all_data if all_data else None,
                                           varying_order=varying_order,
                                           clean_floats=clean_floats)
                print(f"\n  Split complete: {result.total_rows_written:,} rows -> "
                      f"{len(result.output_paths)} files")
                if result.skipped_by_dedup:
                    print(f"  Dedup skipped: {result.skipped_by_dedup:,} rows")
                for op in result.output_paths[:5]:
                    rc = result.rows_per_file.get(str(op), 0)
                    print(f"    {op.name}: {rc:,} rows")
                if len(result.output_paths) > 5:
                    print(f"    ... and {len(result.output_paths) - 5} more files")
                return result

            # ---- apply dedup (streaming) -----------------------------------
            merged_rows = list(iter_apply_dedup(all_data, resolutions, reports,
                                                varying_order=varying_order))

            # ---- intra-point duplicate removal ----------------------------
            from campaign_data.grid_analysis import remove_intra_point_duplicates
            n_before = len(merged_rows)
            merged_rows = remove_intra_point_duplicates(
                merged_rows, varying_order, reports[0].header,
                dup_freq_tolerance=dup_freq_tolerance)
            n_removed = n_before - len(merged_rows)

            # ---- write output ----------------------------------------------
            all_rows = [reports[0].header] + merged_rows
            write_tsv(out_path, all_rows, clean_floats=clean_floats,
                      header_len=header_len)
            print(f"\n  Merged output: {out_path}  ({len(merged_rows):,} data rows)"
                  + (f"  [{n_removed:,} intra-point duplicates removed]"
                     if n_removed else ""))

            # ---- post-merge regularity (in-memory, no file re-read) -----------
            print_merged_regularity(path_dims=path_dims,
                                    rows=merged_rows, header=reports[0].header,
                                    header_len=header_len)
            return out_path

        finally:
            for p in _aligned_paths:
                if p.exists():
                    p.unlink()
    finally:
        if _saved_fr is not None:
            _iou.FLOAT_RND = _saved_fr
        if _saved_mode is not None:
            _iou.ROUND_MODE = _saved_mode


def simple_merge(paths, out_path, clean_floats=True, path_dims=None,
                 dup_freq_tolerance=None, header_len=None):
    """Backward-compatible simple concatenation (original merge_csv behaviour).

    Concatenates files without deduplication. Useful when files are known to
    be disjoint (e.g. different parameter ranges with no overlap).
    """
    # Default: no path dims. See smart_merge for rationale.
    if path_dims is None:
        path_dims = []

    if header_len is None:
        from campaign_data.io_utils import detect_header_len
        header_len = detect_header_len(paths[0])
    paths = _align_headers(paths, header_len)
    if paths is None:
        return None

    _aligned_paths = [p for p in paths if p.stem.endswith('_aligned')]

    try:
        reports = analyse_files(paths, path_dims=path_dims,
                                dup_freq_tolerance=dup_freq_tolerance)
        varying_order = get_canonical_varying_order(reports)

        print_overview(reports)
        print_groups(reports)
        overlap = find_overlap_region(reports, varying_order)
        print_merge_assessment(reports, overlap, varying_order=varying_order,
                               path_dims=path_dims)
        print_gaps(reports, path_dims=path_dims)

        all_rows = [reports[0].header]
        for rep in reports:
            all_rows.extend(read_table_rows(rep.path)[1:])

        out_path = Path(out_path)
        write_tsv(out_path, all_rows, clean_floats=clean_floats,
                  header_len=header_len)
        print(f"\n  Merged output: {out_path}  ({len(all_rows) - 1:,} data rows)")

        print_merged_regularity(path_dims=path_dims,
                                rows=all_rows[1:], header=all_rows[0],
                                header_len=header_len)
        return out_path

    finally:
        for p in _aligned_paths:
            if p.exists():
                p.unlink()


def agent_quick_merge(source_dir, out_path=None, dry_run=True, recursive=False):
    """Return structured results for Agent consumption (no printing, returns dict).

    Args:
        source_dir: directory containing CSV/TXT files
        out_path: output path (None = auto at source_dir/merged.txt)
        dry_run: True = analyse only, False = full merge
        recursive: if True, scan subdirectories (default False)

    Returns:
        dict: paths, reports, overlap, decisions, output_path
    """
    paths = collect_paths([source_dir], recursive=recursive)
    from campaign_data.io_utils import detect_header_len
    paths = _align_headers(paths, detect_header_len(paths[0]))
    if paths is None:
        return {"error": "header mismatch"}

    _aligned_paths = [p for p in paths if p.stem.endswith('_aligned')]

    try:
        reports = analyse_files(paths)
        varying_order = get_canonical_varying_order(reports)
        overlap = find_overlap_region(reports, varying_order)
        resolutions = (resolve_cross_file_overlaps(reports, overlap, varying_order)
                       if overlap else {})

        result = {
            "paths": paths,
            "reports": reports,
            "overlap": overlap,
            "resolutions": resolutions,
            "output_path": None,
        }

        if not dry_run:
            result["output_path"] = smart_merge(
                [source_dir], out_path=out_path, dry_run=False,
            )

        return result

    finally:
        for p in _aligned_paths:
            if p.exists():
                p.unlink()
