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
