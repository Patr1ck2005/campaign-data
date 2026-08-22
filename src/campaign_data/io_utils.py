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


def sniff_delimiter(path) -> str:
    """Return '\\t' or ',' for a table file based on its first content line.

    Content-based sniffing: COMSOL exports are tab-separated regardless of
    the ``.csv`` extension, while batch-export metadata CSVs are comma-
    separated.  The first non-empty line decides — tabs win ties because a
    tab-separated header often contains commas inside column names (e.g.
    ``abs(cx)^2+... (kg^2*m^2/(s^6*A^2))``), while comma-separated files
    essentially never contain tab characters.

    Falls back to ',' only when no readable line contains either delimiter.
    """
    try:
        with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
            for _line in f:
                line = _line.rstrip("\r\n")
                if not line.strip():
                    continue
                tabs = line.count("\t")
                commas = line.count(",")
                if tabs == 0 and commas == 0:
                    continue  # single-column junk line; try the next one
                return "\t" if tabs >= commas else ","
    except OSError:
        pass
    return ","


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
    """Read a TSV or CSV file and return (header, data_rows). Strips header cells.

    The delimiter is sniffed from the file content (see :func:`sniff_delimiter`),
    not assumed from the extension — COMSOL exports are frequently
    tab-separated ``.csv`` files.
    """
    path = Path(path)
    rows = _read_delimited(path, sniff_delimiter(path))
    header = [c.strip() for c in rows[0]]
    return header, rows[1:]


def read_table_rows(path):
    """Full read with content-sniffed delimiter. Returns row-lists incl. header."""
    path = Path(path)
    return _read_delimited(path, sniff_delimiter(path))


def _read_delimited(path, delimiter):
    """Dispatch to the comma (batch-export) or tab reader by delimiter."""
    if delimiter == ",":
        return read_csv(path)
    return read_tsv(path)


def _stream_head_rows(path, n):
    """Read only the first n data rows + header. Returns (header, data_rows)."""
    path = Path(path)
    if sniff_delimiter(path) == ',':
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


def read_table(path, max_rows=None):
    """Read a TSV or COMSOL batch-export CSV into a pandas DataFrame.

    One-call ergonomic reader: handles the ``%`` metadata preamble and the
    ``% col_name`` header prefix of batch-export CSVs transparently, and
    plain TSVs directly.  Numeric columns are converted via
    :func:`pandas.to_numeric`; cells that are not plain floats (complex
    eigen-frequencies such as ``85.8+18.5i``, field strings) stay as str.

    Args:
        path: file path (``.csv`` or ``.txt``).
        max_rows: optional row cap applied after parsing (head convenience).

    Returns:
        pandas.DataFrame with a cleaned header.

    Raises:
        ImportError: if pandas is not installed.  Install the extra with
            ``pip install campaign-data[pandas]``.
    """
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - depends on env
        raise ImportError(
            "read_table() requires pandas. "
            "Install it with: pip install campaign-data[pandas]"
        ) from exc

    header, data_rows = read_and_parse(path)
    df = pd.DataFrame(data_rows, columns=header)
    if max_rows is not None:
        df = df.head(max_rows)
    for col in df.columns:
        converted = pd.to_numeric(df[col], errors="coerce")
        # Keep the conversion only where nothing was coerced away; this
        # preserves complex-number and text columns as str.
        if not converted.isna().any():
            df[col] = converted
        elif converted.notna().any():
            # Mixed column: keep str (do not silently produce NaN holes).
            df[col] = df[col].astype(str).str.strip()
    return df


def peek_table(path, max_values=8):
    """Return a human/agent-readable structure summary of a table file.

    Stdlib-only (no pandas): reports shape, per-column dtype class,
    distinct-value counts, and up to *max_values* sample values per column.
    Useful for the "what's in this file?" question without spinning up a
    full audit.
    """
    header, data_rows = _stream_head_rows(path, _peek_sample_target(path))
    n_cols = len(header)

    # Column stats over the sampled rows.
    values_by_col = [[] for _ in range(n_cols)]
    for row in data_rows:
        for ci in range(min(len(row), n_cols)):
            values_by_col[ci].append(row[ci].strip())

    lines = [f"file: {Path(path).name}", f"rows shown: {len(data_rows)}"]
    for ci, name in enumerate(header):
        vals = values_by_col[ci]
        uniq = list(dict.fromkeys(vals))
        kind = _classify_column(uniq)
        samples = ", ".join(uniq[:max_values])
        lines.append(
            f"col {ci:>2} {name}: {kind}, {len(uniq)} distinct"
            f"{'' if len(vals) == len(uniq) or len(uniq) <= max_values else '+'}"
            f" | {samples}"
        )
    return "\n".join(lines)


def _peek_sample_target(path):
    """Rows to sample for peek_table: all rows for small files, else head."""
    total = sum(1 for _ in open(path, "r", encoding="utf-8-sig", errors="ignore"))
    return total if total <= 2000 else 2000


def _classify_column(uniq_values):
    """Classify a column from its distinct string values."""
    if not uniq_values:
        return "empty"
    numeric = 0
    complex_like = 0
    for v in uniq_values:
        s = v.strip()
        if not s:
            continue
        try:
            float(s)
            numeric += 1
        except ValueError:
            if "+" in s or ("-" in s[1:]) and any(ch.isdigit() for ch in s):
                complex_like += 1
    non_empty = max(numeric + complex_like, 1)
    if complex_like == 0 and numeric == non_empty:
        return "numeric"
    if complex_like > 0 and numeric > 0:
        return "mixed-numeric/complex"
    return "text"
