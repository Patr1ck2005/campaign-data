"""Deduplication engine for interrupted scan overlaps.

When a parameter scan is interrupted and restarted, the breakpoint parameter
value may have partial data in the first file and complete data in the second.
This module detects overlapping grid points and decides which file contributes each.
"""

from collections import defaultdict
from dataclasses import dataclass

from campaign_data.io_utils import fv
from campaign_data.grid_analysis import (
    find_overlap_region, per_slice_density, GridReport,
    get_canonical_varying_order,
)


def _build_projection(rep, varying_order):
    """Build a projection list to map a file's rows to canonical grid keys.

    Returns a list of (kind, ref) tuples where kind is 'varying' (ref = CSV
    column index) or 'const' (ref = the fv-rounded constant value).
    """
    proj = []
    for ci in varying_order:
        if ci in rep.varying:
            proj.append(('varying', ci))
        else:
            proj.append(('const', rep.constant[ci]))
    return proj


def _project_row(row, proj):
    """Compute the canonical grid key for a data row using a projection."""
    parts = []
    for kind, ref in proj:
        if kind == 'varying':
            parts.append(fv(row[ref]))
        else:
            parts.append(ref)
    return tuple(parts)


def resolve_cross_file_overlaps(reports, overlap=None, varying_order=None,
                                return_densities=False):
    """Return per-grid-point dedup decisions for cross-file overlaps.

    For each overlapping grid point, compares per-slice completeness across
    files that own it. The file with higher sub-grid density at the outermost
    varying dimension wins that point.

    Returns a dict {canonical_grid_key: winning_file_index}, or
    (resolutions_dict, densities_dict) when return_densities=True.
    """
    if overlap is None:
        overlap = find_overlap_region(reports, varying_order)
    if not overlap:
        return ({}, {}) if return_densities else {}

    if varying_order is None:
        varying_order = get_canonical_varying_order(reports)
    anchor_col = varying_order[0]

    densities = {}
    for i, rep in enumerate(reports):
        densities[i] = per_slice_density(rep, anchor_col)

    resolutions = {}
    for gk, owners in overlap.items():
        gk_map = dict(zip(varying_order, gk))
        av = gk_map[anchor_col]

        best_i = owners[0]
        best_density = densities[best_i].get(av, 0)
        for i in owners[1:]:
            di = densities[i].get(av, 0)
            if di > best_density:
                best_density = di
                best_i = i
            elif di == best_density:
                # Same sub-grid completeness — prefer the file with more
                # data rows (more eigenmodes computed per grid point).
                if reports[i].data_rows > reports[best_i].data_rows:
                    best_i = i
        resolutions[gk] = best_i

    if return_densities:
        return resolutions, densities
    return resolutions


def detect_breakpoints(reports, overlap=None, varying_order=None,
                       return_densities=False):
    """Deprecated alias for resolve_cross_file_overlaps.

    'Breakpoints' was legacy interruption jargon; the function actually resolves
    cross-file grid-point overlaps. Use resolve_cross_file_overlaps instead.
    """
    import warnings
    warnings.warn(
        "detect_breakpoints is deprecated; use resolve_cross_file_overlaps",
        DeprecationWarning,
        stacklevel=2,
    )
    return resolve_cross_file_overlaps(reports, overlap=overlap,
                                       varying_order=varying_order,
                                       return_densities=return_densities)


def dedup_summary(resolutions, reports, overlap, densities=None,
                  varying_order=None):
    """Build a human-readable summary of dedup decisions.

    Returns a list of DedupDecision, one per distinct anchor-value range
    within each file-pair resolution group.
    """
    if not resolutions:
        return []

    if varying_order is None:
        varying_order = get_canonical_varying_order(reports)
    anchor_col = varying_order[0]
    anchor_name = reports[0].header[anchor_col]

    # Group resolutions by (loser, anchor_val) → winner.
    # Only consider files that actually own the overlapping point as losers.
    by_key = defaultdict(lambda: defaultdict(set))
    for gk, winner in resolutions.items():
        gk_map = dict(zip(varying_order, gk))
        av = gk_map[anchor_col]
        owners = overlap.get(gk, [])
        losers = set(owners) - {winner}
        for loser in sorted(losers):
            by_key[(loser, winner)][av].add(winner)

    decisions = []
    for (loser, winner), av_map in sorted(by_key.items()):
        anchor_vals = sorted(av_map.keys())

        # Per-slice density comparison — use cached densities if provided
        if densities is not None:
            density_winner = densities.get(winner, {})
            density_loser = densities.get(loser, {})
        else:
            density_winner = per_slice_density(reports[winner], anchor_col)
            density_loser = per_slice_density(reports[loser], anchor_col)

        resolutions_dict = {}
        diff_vals = []
        for av in anchor_vals:
            resolutions_dict[av] = winner
            cw = density_winner.get(av, 0)
            cl = density_loser.get(av, 0)
            if cw != cl:
                diff_vals.append(
                    f"{anchor_name}={av}: file[{winner}]({cw}) vs file[{loser}]({cl})"
                )

        if diff_vals:
            detail = "; ".join(diff_vals)
        elif reports[winner].data_rows > reports[loser].data_rows:
            detail = (
                f"equal sub-grid density, "
                f"file[{winner}]({reports[winner].data_rows:,} rows) "
                f"> file[{loser}]({reports[loser].data_rows:,} rows)"
            )
        else:
            detail = "equal completeness, keeping first."
        rationale = (
            f"Resolved on outermost varying dimension '{anchor_name}'. {detail}"
        )

        decisions.append(DedupDecision(
            anchor_col=anchor_col,
            anchor_col_name=anchor_name,
            resolutions=resolutions_dict,
            rationale=rationale,
        ))

    return decisions


def iter_apply_dedup(all_data, resolutions, reports, varying_order=None):
    """Generator: yields deduplicated rows one at a time.

    Same logic as apply_dedup but yields rows instead of accumulating a list.
    Enables streaming merge without holding the full merged result in memory
    alongside the source data.
    """
    if not resolutions:
        for fi in sorted(all_data):
            yield from all_data[fi]
        return

    if varying_order is None:
        varying_order = get_canonical_varying_order(reports)
    projections = {}
    for fi in all_data:
        projections[fi] = _build_projection(reports[fi], varying_order)

    for fi in sorted(all_data):
        proj = projections[fi]
        for r in all_data[fi]:
            gk = _project_row(r, proj)
            if gk in resolutions and resolutions[gk] != fi:
                continue
            yield r


def apply_dedup(all_data, resolutions, reports, varying_order=None):
    """Apply per-grid-point dedup decisions to raw data rows.

    Args:
        all_data: {file_index: [data_rows]} — raw rows from each file
        resolutions: {canonical_grid_key: winning_file_index} from resolve_cross_file_overlaps()
        reports: list of GridReport for column reference
        varying_order: optional pre-computed canonical varying order

    Returns:
        list of rows with overlapping duplicates removed
    """
    return list(iter_apply_dedup(all_data, resolutions, reports, varying_order))



@dataclass
class DedupDecision:
    """Records which file to use for each overlapping grid slice."""
    anchor_col: int
    anchor_col_name: str
    resolutions: dict              # {anchor_value: chosen_file_index}
    rationale: str
