"""Tab-separated CSV I/O, path collection, and float cleaning utilities."""

import csv
import math
from pathlib import Path

HEADER_LEN = 12       # first N columns are grid parameters; remainder are results
FLOAT_RND = 10         # significant digits for normalising float noise in comparison
OUT_FLOAT_RND = 10     # significant digits for output float cleaning
ROUND_MODE = "round"   # "round" (half-even) or "truncate" (chop at N sig digits)


def _sig_truncate(x, sig):
    """Truncate *x* to *sig* significant digits (chop, no rounding)."""
    if x == 0:
        return 0.0
    sign = -1 if x < 0 else 1
    x_abs = abs(x)
    exp = math.floor(math.log10(x_abs))
    scale = 10 ** (sig - 1 - exp)
    return sign * math.floor(x_abs * scale) / scale


def clean_cell(s, decimals=OUT_FLOAT_RND):
    """Remove float noise in output cells, e.g. 237.69999999999996 -> 237.7.

    Uses significant-digit rounding so tiny values (e.g. 7.1e-21) are
    never silently zeroed.
    """
    s = s.strip()
    try:
        num = float(s)
        if num == 0:
            return "0"
        return f"{num:.{decimals}g}"
    except ValueError:
        return s


def fv(s, decimals=None):
    """Normalise a cell to float so 5999.999999999999 == 6000.0.

    When *decimals* is None (the default), the current value of the module-level
    FLOAT_RND is read at call time.  This allows runtime override via
    ``io_utils.FLOAT_RND = 8`` (useful for legacy contaminated data).

    When ROUND_MODE is "truncate", uses truncation instead of half-even
    rounding.  Truncation avoids rounding-boundary splits where two
    nearby values end up on opposite sides of a rounding threshold.
    """
    if decimals is None:
        decimals = FLOAT_RND
    s = s.strip()
    try:
        num = float(s)
        if num == 0:
            return 0.0
        if ROUND_MODE == "truncate":
            return _sig_truncate(num, decimals)
        return float(f"{num:.{decimals}g}")
    except ValueError:
        return s


def read_tsv(path):
    """Read a tab-separated file. Returns list of row-lists including header."""
    # Try UTF-8 first, then fall back to the system default.
    try:
        with open(path, "r", newline="", encoding="utf-8-sig") as f:
            return list(csv.reader(f, delimiter="\t"))
    except UnicodeDecodeError:
        with open(path, "r", newline="") as f:
            return list(csv.reader(f, delimiter="\t"))


def read_csv(path):
    """Read a batch-export CSV file.

    Skips metadata preamble lines (starting with %) and strips the leading
    '% ' from the first header column name.  Returns list of row-lists
    including the cleaned header.
    """
    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f, delimiter=",")
        rows = []
        for row in reader:
            if not row or not row[0].strip():
                continue
            if row[0].strip().startswith("%") and len(row) == 2:
                continue  # metadata line: % Model, % Version, % Date, % Table
            rows.append(row)
    # Clean '% col_name' -> 'col_name' in header
    if rows:
        rows[0] = [c.strip().lstrip("%").strip() for c in rows[0]]
    return rows


def write_tsv(path, rows, clean_floats=False, header_len=None):
    """Write rows to a tab-separated file, optionally cleaning float noise.

    When header_len is provided, the first *header_len* columns (parameter
    columns) are rounded to FLOAT_RND precision via fv(), and remaining
    columns use OUT_FLOAT_RND.  This prevents tiny fp differences from
    different export sources from creating near-duplicate grid keys.
    """
    path = Path(path)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter="\t")
        if clean_floats and header_len is not None:
            def _clean(row):
                for ci, c in enumerate(row):
                    if ci < header_len:
                        yield str(fv(c))
                    else:
                        yield clean_cell(c, OUT_FLOAT_RND)
            writer.writerows(_clean(row) for row in rows)
        elif clean_floats:
            writer.writerows([clean_cell(c) for c in row] for row in rows)
        else:
            writer.writerows(rows)


def read_and_parse(path):
    """Read a TSV or CSV file and return (header, data_rows). Strips header cells."""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        rows = read_csv(path)
    else:
        rows = read_tsv(path)
    header = [c.strip() for c in rows[0]]
    return header, rows[1:]


def _stream_head_rows(path, n):
    """Read only the first n data rows + header. Returns (header, data_rows)."""
    path = Path(path)
    if path.suffix.lower() == '.csv':
        with open(path, 'r', newline='', encoding='utf-8-sig') as f:
            reader = csv.reader(f, delimiter=',')
            rows = []
            for row in reader:
                if not row or not row[0].strip():
                    continue
                if row[0].strip().startswith('%') and len(row) == 2:
                    continue
                rows.append(row)
                if len(rows) > n:
                    break
        if rows:
            rows[0] = [c.strip().lstrip('%').strip() for c in rows[0]]
        header = rows[0] if rows else []
        return header, rows[1:]
    else:
        try:
            with open(path, 'r', newline='', encoding='utf-8-sig') as f:
                reader = csv.reader(f, delimiter='\t')
                rows = []
                for i, row in enumerate(reader):
                    rows.append(row)
                    if i >= n:
                        break
        except UnicodeDecodeError:
            with open(path, 'r', newline='') as f:
                reader = csv.reader(f, delimiter='\t')
                rows = []
                for i, row in enumerate(reader):
                    rows.append(row)
                    if i >= n:
                        break
        header = [c.strip() for c in rows[0]] if rows else []
        return header, rows[1:]


def detect_header_len(path, sample_rows=20):
    """Auto-detect the parameter/output column boundary.

    Scans columns left to right on the first *sample_rows* data rows.
    The first column whose values cannot be parsed as floats marks the
    start of output data (e.g. complex eigen-frequencies).
    Returns the column index or the total column count if all numeric.
    """
    _header, data = _stream_head_rows(path, sample_rows)
    n_cols = len(_header)

    for col_idx in range(n_cols):
        for row in data:
            if col_idx >= len(row):
                continue
            cell = row[col_idx].strip()
            if not cell:
                continue
            try:
                float(cell)
            except ValueError:
                return col_idx
    return n_cols


def collect_paths(sources, recursive=False):
    """Given a list of paths, expand any directory into *.csv + *.txt files.

    When *recursive* is True, also scans subdirectories.
    """
    paths = []
    for s in sources:
        p = Path(s)
        if p.is_dir():
            for ext in ("*.csv", "*.txt"):
                found = sorted(p.rglob(ext) if recursive else p.glob(ext))
                for f in found:
                    if f not in paths:
                        paths.append(f)
        elif p.is_file():
            paths.append(p)
        else:
            print(f"[WARN] Not found: {s}")
    return paths
