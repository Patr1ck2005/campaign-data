"""Audit module: structured analysis of messy simulation data directories.

Given a directory (or list of files), audit_directory() produces an
AuditReport with per-file mesh fingerprints, campaign clustering, per-value
coverage matrix, and cross-campaign conflicts.  This is the agent-facing
entry point that replaces ad-hoc diagnostic scripts.

Design:
  - Reuses analyse_directory() from merge.py for the heavy lifting
    (path collection, header alignment, per-file GridReport, overlap detection,
    cross-file dedup resolution).
  - Adds mesh-step computation, campaign clustering, coverage matrix, and
    conflict detection on top.
  - format_audit_report() produces a markdown summary for agent consumption.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from campaign_data.merge import analyse_directory, AnalysisResult
from campaign_data.grid_analysis import GridReport


# ============================================================
# Data structures
# ============================================================

@dataclass
class MeshAxis:
    """One varying column's grid fingerprint."""
    col_idx: int
    name: str
    n_points: int
    min_val: float
    max_val: float
    step: float          # most common consecutive difference (0 if <2 points)
    is_path_member: bool = False  # True if part of a path_dims group


@dataclass
class FileFingerprint:
    """Per-file mesh fingerprint for campaign clustering."""
    path: Path
    name: str
    data_rows: int
    expected_points: int
    actual_points: int
    completeness: float
    eigen_counts: list
    duplicate_rows: int
    mesh_axes: dict[str, MeshAxis]   # col_name -> MeshAxis
    varying_names: list[str]
    constant_names: list[str]
    n_modes: int                      # modal eigenmode count (typical bands per grid point)


@dataclass
class Campaign:
    """A group of files sharing the same mesh fingerprint."""
    campaign_id: str
    signature: tuple                   # (varying_names, tuple of (name, step_bucket, range_bucket))
    file_indices: list[int]
    file_names: list[str]
    mesh_description: str              # human-readable summary
    n_files: int


@dataclass
class CoverageEntry:
    """One parameter value's coverage across files."""
    param_name: str
    param_value: float
    file_indices: list[int]
    file_names: list[str]
    campaign_ids: list[str]
    is_conflict: bool                  # True if >1 campaign covers this value


@dataclass
class Conflict:
    """A parameter value covered by multiple campaigns (mesh mismatch)."""
    param_name: str
    param_value: float
    campaign_ids: list[str]
    file_indices: list[int]
    file_names: list[str]
    recommendation: str


@dataclass
class OverlapPair:
    """A pair of files sharing grid points."""
    file_a_idx: int
    file_b_idx: int
    file_a_name: str
    file_b_name: str
    n_shared_points: int


@dataclass
class AuditReport:
    """Full audit result for a data directory."""
    source: Path
    file_count: int
    total_rows: int
    files: list[FileFingerprint]
    campaigns: list[Campaign]
    coverage: list[CoverageEntry]      # per value of the anchor column
    anchor_col_name: str               # which column coverage is reported on
    overlaps: list[OverlapPair]
    conflicts: list[Conflict]
    issues: list[str]
    # Raw analysis result for downstream consumers (recommend_cleanup, etc.)
    analysis: AnalysisResult | None = field(default=None, repr=False)


# ============================================================
# Mesh fingerprinting
# ============================================================

def _bucket_step(step: float) -> str:
    """Bucket a step value into a coarse signature for campaign clustering.

    Buckets: "0" (single point), "<1e-3", "1e-3..1e-2", "1e-2..1e-1", ">1e-1".
    """
    if step == 0:
        return "0"
    if step < 1e-3:
        return "<1e-3"
    if step < 1e-2:
        return "1e-3..1e-2"
    if step < 1e-1:
        return "1e-2..1e-1"
    return ">1e-1"


def _bucket_range(min_val: float, max_val: float) -> str:
    """Bucket the range (max - min) coarsely."""
    span = max_val - min_val
    if span == 0:
        return "0"
    if span < 1e-2:
        return "<1e-2"
    if span < 1e-1:
        return "1e-2..1e-1"
    if span < 1:
        return "1e-1..1"
    return ">1"


def _compute_step(sorted_vals: list) -> float:
    """Most common consecutive difference. 0 if <2 values."""
    if len(sorted_vals) < 2:
        return 0.0
    diffs = [round(sorted_vals[i + 1] - sorted_vals[i], 8)
             for i in range(len(sorted_vals) - 1)]
    diffs = [d for d in diffs if d != 0]  # ignore duplicate values
    if not diffs:
        return 0.0
    return Counter(diffs).most_common(1)[0][0]


def _build_fingerprint(rep: GridReport, path_dims: list) -> FileFingerprint:
    """Build a FileFingerprint from a GridReport."""
    path_col_indices = set()
    for group in path_dims:
        for ci in group:
            path_col_indices.add(ci)

    mesh_axes = {}
    for ci, vals in rep.varying.items():
        col_name = rep.header[ci]
        sorted_vals = sorted(vals)
        step = _compute_step(sorted_vals)
        mesh_axes[col_name] = MeshAxis(
            col_idx=ci,
            name=col_name,
            n_points=len(sorted_vals),
            min_val=float(sorted_vals[0]) if sorted_vals else 0.0,
            max_val=float(sorted_vals[-1]) if sorted_vals else 0.0,
            step=step,
            is_path_member=ci in path_col_indices,
        )

    eigen_counts = list(rep.eigen_counts) if rep.eigen_counts else []
    n_modes = Counter(eigen_counts).most_common(1)[0][0] if eigen_counts else 0

    return FileFingerprint(
        path=rep.path,
        name=rep.path.name,
        data_rows=rep.data_rows,
        expected_points=rep.expected_points,
        actual_points=rep.actual_points,
        completeness=rep.completeness,
        eigen_counts=eigen_counts,
        duplicate_rows=rep.duplicate_rows,
        mesh_axes=mesh_axes,
        varying_names=[rep.header[ci] for ci in sorted(rep.varying.keys())],
        constant_names=[rep.header[ci] for ci in sorted(rep.constant.keys())],
        n_modes=n_modes,
    )


# ============================================================
# Campaign clustering
# ============================================================

def _campaign_signature(fp: FileFingerprint) -> tuple:
    """Build a clustering signature from mesh axes.

    Groups files that have the same varying columns and the same coarse
    mesh fingerprint (step bucket + range bucket per axis).
    """
    varying_key = tuple(sorted(fp.varying_names))
    mesh_key = []
    for name in sorted(fp.mesh_axes.keys()):
        ax = fp.mesh_axes[name]
        mesh_key.append((
            name,
            _bucket_step(ax.step),
            _bucket_range(ax.min_val, ax.max_val),
            ax.n_points,
        ))
    return (varying_key, tuple(mesh_key))


def _campaign_id(sig: tuple, idx: int) -> str:
    """Generate a short campaign ID."""
    return f"C{idx}"


def _mesh_description(fp: FileFingerprint) -> str:
    """Human-readable mesh summary for a fingerprint."""
    parts = []
    for name in sorted(fp.mesh_axes.keys()):
        ax = fp.mesh_axes[name]
        if ax.is_path_member:
            parts.append(f"{name}: path({ax.n_points}pts)")
        elif ax.step == 0:
            parts.append(f"{name}: {ax.n_points}pts")
        else:
            parts.append(f"{name}: {ax.n_points}pts[{ax.min_val:.4g}..{ax.max_val:.4g}] step={ax.step:.4g}")
    return ", ".join(parts)


def _cluster_campaigns(fingerprints: list[FileFingerprint]) -> list[Campaign]:
    """Group fingerprints into campaigns by mesh signature."""
    groups = defaultdict(list)
    for i, fp in enumerate(fingerprints):
        sig = _campaign_signature(fp)
        groups[sig].append(i)

    campaigns = []
    for cid_idx, (sig, file_indices) in enumerate(
            sorted(groups.items(), key=lambda x: -len(x[1]))):
        # Use the first file as representative for description
        rep_fp = fingerprints[file_indices[0]]
        campaigns.append(Campaign(
            campaign_id=_campaign_id(sig, cid_idx),
            signature=sig,
            file_indices=list(file_indices),
            file_names=[fingerprints[i].name for i in file_indices],
            mesh_description=_mesh_description(rep_fp),
            n_files=len(file_indices),
        ))
    return campaigns


# ============================================================
# Coverage matrix
# ============================================================

def _select_anchor_col(fingerprints: list[FileFingerprint],
                       reports: list[GridReport]) -> tuple[str, int]:
    """Select the best column for per-value coverage reporting.

    Preference: a column that distinguishes files (appears as varying or
    constant across files) with the FEWEST unique values but >1.  This
    is typically the scan parameter (e.g., t_tot) rather than the mesh
    axes (e.g., m1 with 101 points).

    Heuristic: among columns that appear in ALL files, pick the one with
    the fewest unique cross-file values (but >1).  Fallback: first varying.
    """
    if not fingerprints:
        return "", -1

    # Collect all column names that appear in every file (as varying or constant)
    all_cols = set()
    for fp in fingerprints:
        cols = set(fp.varying_names) | set(fp.constant_names)
        if not all_cols:
            all_cols = cols
        else:
            all_cols &= cols

    # For each candidate, count unique values across all files
    best_name = None
    best_count = float('inf')
    best_idx = -1
    for name in all_cols:
        vals = set()
        col_idx = -1
        for rep in reports:
            for ci, n in enumerate(rep.header):
                if n == name:
                    col_idx = ci
                    break
            if col_idx in rep.varying:
                vals.update(rep.varying[col_idx])
            elif col_idx in rep.constant:
                vals.add(rep.constant[col_idx])
        n_unique = len(vals)
        # Prefer columns with >1 unique value (distinguishes files) and fewest
        if n_unique > 1 and n_unique < best_count:
            best_count = n_unique
            best_name = name
            best_idx = col_idx

    # Fallback: first varying column from first file
    if best_name is None and fingerprints:
        for fp in fingerprints:
            if fp.varying_names:
                best_name = fp.varying_names[0]
                for ci, n in enumerate(reports[0].header):
                    if n == best_name:
                        best_idx = ci
                        break
                break

    return best_name or "", best_idx


def _build_coverage(fingerprints: list[FileFingerprint],
                    reports: list[GridReport],
                    campaigns: list[Campaign],
                    anchor_name: str,
                    anchor_idx: int) -> list[CoverageEntry]:
    """Build per-value coverage for the anchor column."""
    if anchor_idx < 0 or not campaigns:
        return []

    # Map file index -> campaign_id
    file_to_campaign = {}
    for camp in campaigns:
        for fi in camp.file_indices:
            file_to_campaign[fi] = camp.campaign_id

    # Collect all values and which files cover each
    value_to_files = defaultdict(list)
    for fi, rep in enumerate(reports):
        if anchor_idx in rep.varying:
            for v in rep.varying[anchor_idx]:
                value_to_files[v].append(fi)
        elif anchor_idx in rep.constant:
            value_to_files[rep.constant[anchor_idx]].append(fi)

    entries = []
    for val in sorted(value_to_files.keys()):
        file_indices = value_to_files[val]
        camp_ids = sorted(set(file_to_campaign.get(fi, "?") for fi in file_indices))
        entries.append(CoverageEntry(
            param_name=anchor_name,
            param_value=float(val),
            file_indices=file_indices,
            file_names=[fingerprints[i].name for i in file_indices],
            campaign_ids=camp_ids,
            is_conflict=len(camp_ids) > 1,
        ))
    return entries


# ============================================================
# Conflict detection
# ============================================================

def _detect_conflicts(coverage: list[CoverageEntry],
                      campaigns: list[Campaign],
                      fingerprints: list[FileFingerprint]) -> list[Conflict]:
    """Detect parameter values covered by multiple campaigns."""
    conflicts = []
    for entry in coverage:
        if not entry.is_conflict:
            continue
        # Build recommendation: keep the campaign with finest step on the
        # first non-anchor varying axis (heuristic: finest L)
        camp_step_map = {}
        for camp in campaigns:
            if camp.campaign_id in entry.campaign_ids:
                # Use the first file's first mesh axis step as representative
                rep_fp = fingerprints[camp.file_indices[0]]
                steps = [ax.step for ax in rep_fp.mesh_axes.values()
                         if ax.step > 0 and not ax.is_path_member]
                camp_step_map[camp.campaign_id] = min(steps) if steps else float('inf')
        if camp_step_map:
            best_camp = min(camp_step_map, key=camp_step_map.get)
            recommendation = f"keep {best_camp} (finest step {camp_step_map[best_camp]:.4g})"
        else:
            recommendation = "manual review needed"

        conflicts.append(Conflict(
            param_name=entry.param_name,
            param_value=entry.param_value,
            campaign_ids=entry.campaign_ids,
            file_indices=entry.file_indices,
            file_names=entry.file_names,
            recommendation=recommendation,
        ))
    return conflicts


# ============================================================
# Overlap pairs
# ============================================================

def _build_overlap_pairs(overlap: dict, reports: list[GridReport]) -> list[OverlapPair]:
    """Convert overlap dict to list of OverlapPair."""
    if not overlap:
        return []
    pair_counts = Counter()
    for grid_key, owners in overlap.items():
        for i in range(len(owners)):
            for j in range(i + 1, len(owners)):
                pair_counts[(owners[i], owners[j])] += 1
    pairs = []
    for (a, b), n in pair_counts.most_common():
        pairs.append(OverlapPair(
            file_a_idx=a,
            file_b_idx=b,
            file_a_name=reports[a].path.name,
            file_b_name=reports[b].path.name,
            n_shared_points=n,
        ))
    return pairs


# ============================================================
# Main entry point
# ============================================================

def audit_directory(source, *, path_dims=None, use_cache=True,
                    dup_freq_tolerance=None, header_len=None,
                    recursive=False) -> AuditReport:
    """Audit a data directory and return a structured report.

    This is the caller-facing entry point for understanding messy campaign
    data. Produces per-file fingerprints, campaign clustering, per-value
    coverage matrix, overlap summary, and conflict detection.

    Args:
        source: directory path or list of file paths
        path_dims: path-axis column groups, e.g. [("m1","m2")]. None -> [].
        use_cache: use per-file GridReport disk cache
        dup_freq_tolerance: intra-point block-duplicate tolerance
        header_len: number of grid-parameter columns (auto-detect if None)
        recursive: scan subdirectories

    Returns AuditReport.
    """
    source_path = Path(source) if not isinstance(source, list) else Path(source[0])

    # Normalise source to a list for collect_paths (which iterates, not wraps)
    sources_list = source if isinstance(source, list) else [source]

    result = analyse_directory(
        sources_list,
        header_len=header_len,
        path_dims=path_dims,
        use_cache=use_cache,
        dup_freq_tolerance=dup_freq_tolerance,
        recursive=recursive,
        return_data=False,  # audit doesn't need row data
    )

    if result is None:
        return AuditReport(
            source=source_path,
            file_count=0,
            total_rows=0,
            files=[],
            campaigns=[],
            coverage=[],
            anchor_col_name="",
            overlaps=[],
            conflicts=[],
            issues=["analyse_directory returned None (no files or alignment failure)"],
        )

    reports = result.reports
    if path_dims is None:
        path_dims = []
    else:
        # Resolve path_dims names to indices for fingerprinting
        resolved = []
        header = reports[0].header
        for group in path_dims:
            idx_group = []
            for name in group:
                for ci, h in enumerate(header):
                    if h == name:
                        idx_group.append(ci)
                        break
            if idx_group:
                resolved.append(tuple(idx_group))
        path_dims = resolved

    # Build fingerprints
    fingerprints = [_build_fingerprint(rep, path_dims) for rep in reports]

    # Cluster into campaigns
    campaigns = _cluster_campaigns(fingerprints)

    # Select anchor column and build coverage
    anchor_name, anchor_idx = _select_anchor_col(fingerprints, reports)
    coverage = _build_coverage(fingerprints, reports, campaigns,
                               anchor_name, anchor_idx)

    # Detect conflicts
    conflicts = _detect_conflicts(coverage, campaigns, fingerprints)

    # Build overlap pairs
    overlaps = _build_overlap_pairs(result.overlap, reports)

    total_rows = sum(fp.data_rows for fp in fingerprints)

    return AuditReport(
        source=source_path,
        file_count=len(fingerprints),
        total_rows=total_rows,
        files=fingerprints,
        campaigns=campaigns,
        coverage=coverage,
        anchor_col_name=anchor_name,
        overlaps=overlaps,
        conflicts=conflicts,
        issues=[],
        analysis=result,
    )


# ============================================================
# Report formatting
# ============================================================

def format_audit_report(report: AuditReport, *, max_files_per_campaign: int = 10) -> str:
    """Format an AuditReport as markdown for agent/human consumption."""
    lines = []
    sep = "=" * 60

    # ---- Section 1: Overview ----
    lines.append(sep)
    lines.append("1. Overview")
    lines.append(sep)
    lines.append(f"  Source: {report.source}")
    lines.append(f"  Files: {report.file_count}")
    lines.append(f"  Total rows: {report.total_rows:,}")
    lines.append(f"  Campaigns: {len(report.campaigns)}")
    lines.append(f"  Overlap pairs: {len(report.overlaps)}")
    lines.append(f"  Conflicts: {len(report.conflicts)}")
    if report.issues:
        lines.append(f"  Issues: {len(report.issues)}")
        for issue in report.issues:
            lines.append(f"    - {issue}")
    lines.append("")

    # ---- Section 2: Campaigns ----
    lines.append(sep)
    lines.append("2. Campaigns (files clustered by mesh fingerprint)")
    lines.append(sep)
    for camp in report.campaigns:
        lines.append(f"\n  [{camp.campaign_id}] {camp.n_files} file(s)")
        lines.append(f"    mesh: {camp.mesh_description}")
        lines.append(f"    files:")
        for name in camp.file_names[:max_files_per_campaign]:
            lines.append(f"      - {name}")
        if len(camp.file_names) > max_files_per_campaign:
            lines.append(f"      ... and {len(camp.file_names) - max_files_per_campaign} more")
    lines.append("")

    # ---- Section 3: Coverage matrix ----
    lines.append(sep)
    lines.append(f"3. Per-value coverage (anchor column: {report.anchor_col_name})")
    lines.append(sep)
    if report.coverage:
        lines.append(f"  {'value':>12}  {'files':>5}  {'campaigns':>10}  {'conflict':>8}  file_names")
        for entry in report.coverage:
            conflict_flag = "YES" if entry.is_conflict else ""
            names_short = ", ".join(n[:40] for n in entry.file_names[:3])
            if len(entry.file_names) > 3:
                names_short += f", ...+{len(entry.file_names) - 3}"
            lines.append(f"  {entry.param_value:>12.4g}  {len(entry.file_indices):>5}  "
                         f"{'+'.join(entry.campaign_ids):>10}  {conflict_flag:>8}  {names_short}")
    else:
        lines.append("  (no coverage data)")
    lines.append("")

    # ---- Section 4: Overlaps ----
    lines.append(sep)
    lines.append("4. Cross-file overlaps")
    lines.append(sep)
    if report.overlaps:
        lines.append(f"  {'file_a':<50}  {'file_b':<50}  {'shared_pts':>10}")
        for op in report.overlaps[:20]:
            lines.append(f"  {op.file_a_name[:50]:<50}  {op.file_b_name[:50]:<50}  {op.n_shared_points:>10}")
        if len(report.overlaps) > 20:
            lines.append(f"  ... and {len(report.overlaps) - 20} more pairs")
    else:
        lines.append("  (no overlaps detected)")
    lines.append("")

    # ---- Section 5: Conflicts ----
    lines.append(sep)
    lines.append("5. Conflicts (parameter values covered by multiple campaigns)")
    lines.append(sep)
    if report.conflicts:
        lines.append(f"  {'param':>20}  {'value':>12}  {'campaigns':>10}  recommendation")
        for c in report.conflicts:
            lines.append(f"  {c.param_name:>20}  {c.param_value:>12.4g}  "
                         f"{'+'.join(c.campaign_ids):>10}  {c.recommendation}")
    else:
        lines.append("  (no conflicts - all parameter values covered by a single campaign)")
    lines.append("")

    return "\n".join(lines)
