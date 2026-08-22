from pathlib import Path

from campaign_data import analyse_file, audit_directory, collect_paths, simple_merge


def _write(path: Path, rows: list[str]) -> None:
    path.write_text("m1\tm2\tf_thz\n" + "\n".join(rows) + "\n", encoding="utf-8")


def test_analyse_file_reports_grid(tmp_path):
    path = tmp_path / "scan.txt"
    _write(path, ["0\t0\t100", "0\t1\t101", "1\t0\t102", "1\t1\t103"])

    report = analyse_file(path, header_len=2)

    assert report.actual_points == 4
    assert report.expected_points == 4
    assert report.completeness == 1.0
    assert report.varying == {0: [0.0, 1.0], 1: [0.0, 1.0]}


def test_audit_accepts_single_file(tmp_path):
    path = tmp_path / "scan.csv"
    path.write_text(
        "m1,m2,f_thz\n0,0,100\n0,1,101\n1,0,102\n1,1,103\n",
        encoding="utf-8",
    )
    report = audit_directory(tmp_path, header_len=2, use_cache=False)
    assert report.file_count == 1
    assert report.files[0].actual_points == 4


def test_audit_and_simple_merge_preserve_sources(tmp_path):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    _write(first, ["0\t0\t100", "0\t1\t101"])
    _write(second, ["1\t0\t102", "1\t1\t103"])
    output = tmp_path / "merged.txt"

    report = audit_directory(tmp_path, header_len=2, recursive=True, use_cache=False)
    assert report.file_count == 2
    assert collect_paths([tmp_path], recursive=True) == [first, second]

    simple_merge([first, second], output, header_len=2)
    assert output.read_text(encoding="utf-8-sig").splitlines()[0] == "m1\tm2\tf_thz"
    assert first.read_text(encoding="utf-8").startswith("m1\tm2\tf_thz")


def _write_comsol_csv(path: Path) -> None:
    path.write_text(
        "% Model,test.mph\n"
        "% Version,COMSOL 6.4\n"
        "% Table,Evaluation Group 1\n"
        "% spacer (nm),m1,f_thz,field_cx\n"
        "1700,0,85.5+1.2i,-1.5E7+2.7E7i\n"
        "1710,0.002,86.1,3.0E6\n",
        encoding="utf-8",
    )


def test_read_table_tsv_numeric_conversion(tmp_path):
    import pandas as pd

    from campaign_data import read_table

    path = tmp_path / "scan.txt"
    _write(path, ["0\t0\t100", "0\t1\t101"])

    df = read_table(path)
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == ["m1", "m2", "f_thz"]
    assert df["m1"].dtype.kind == "i" or df["m1"].dtype.kind == "f"
    assert df["f_thz"].tolist() == [100.0, 101.0]


def test_read_table_comsol_csv_preamble_and_complex(tmp_path):
    import pandas as pd

    from campaign_data import read_table

    path = tmp_path / "batch.csv"
    _write_comsol_csv(path)

    df = read_table(path)
    assert list(df.columns) == ["spacer (nm)", "m1", "f_thz", "field_cx"]
    assert len(df) == 2
    # Numeric columns converted; complex cells stay str.
    assert pd.api.types.is_numeric_dtype(df["spacer (nm)"])
    assert pd.api.types.is_numeric_dtype(df["m1"])
    assert df["f_thz"].tolist() == ["85.5+1.2i", "86.1"]  # mixed -> str column
    assert df["field_cx"].tolist()[0] == "-1.5E7+2.7E7i"


def test_read_table_max_rows(tmp_path):
    from campaign_data import read_table

    path = tmp_path / "scan.txt"
    _write(path, [f"{i}\t0\t{100 + i}" for i in range(10)])

    df = read_table(path, max_rows=3)
    assert len(df) == 3


def test_read_table_tab_separated_csv_extension(tmp_path):
    """COMSOL exports are frequently tab-separated files named .csv.

    Regression: extension-based delimiter dispatch collapsed such files into
    a single str column. Delimiter must be sniffed from content.
    """
    import pandas as pd

    from campaign_data import read_table

    path = tmp_path / "comsol_export.csv"
    path.write_text(
        "m1\tm2\tfreq (THz)\tfield_cx\n"
        "0\t0\t350.5\t1.9E15-1.3E15i\n"
        "0.1\t0\t351.1\t2.0E15\n",
        encoding="utf-8",
    )

    df = read_table(path)
    assert list(df.columns) == ["m1", "m2", "freq (THz)", "field_cx"]
    assert len(df) == 2
    assert pd.api.types.is_numeric_dtype(df["m1"])
    assert pd.api.types.is_numeric_dtype(df["freq (THz)"])
    assert df["field_cx"].tolist()[0] == "1.9E15-1.3E15i"  # complex stays str


def test_read_table_comma_csv_with_commas_in_tsv_header(tmp_path):
    """A tab-separated file whose header contains commas must stay tab-delimited."""
    from campaign_data import read_table

    path = tmp_path / "tricky.csv"
    path.write_text(
        "abs(cx)^2 (kg^2*m^2/(s^6*A^2))\tf_thz\n1.5\t100\n2.5\t101\n",
        encoding="utf-8",
    )

    df = read_table(path)
    assert list(df.columns) == ["abs(cx)^2 (kg^2*m^2/(s^6*A^2))", "f_thz"]
    assert df["f_thz"].tolist() == [100.0, 101.0]


def test_sniff_delimiter(tmp_path):
    from campaign_data import sniff_delimiter

    tsv = tmp_path / "a.csv"  # tab content, csv name
    tsv.write_text("m1\tm2\n0\t1\n", encoding="utf-8")
    assert sniff_delimiter(tsv) == "\t"

    csv = tmp_path / "b.txt"  # comma content, txt name
    csv.write_text("% Model,test.mph\nm1,m2\n0,1\n", encoding="utf-8")
    assert sniff_delimiter(csv) == ","


def test_peek_table_summary(tmp_path):
    from campaign_data import peek_table

    path = tmp_path / "batch.csv"
    _write_comsol_csv(path)

    text = peek_table(path)
    assert "spacer (nm)" in text
    assert "numeric" in text
    assert "distinct" in text


def test_peek_table_cli(tmp_path, capsys):
    import sys as _sys

    path = tmp_path / "batch.csv"
    _write_comsol_csv(path)

    from campaign_data.__main__ import main

    rc = main(["peek", str(path)])
    captured = capsys.readouterr()
    assert rc == 0
    assert "f_thz" in captured.out
    assert _sys.stderr is not None
