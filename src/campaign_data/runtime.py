"""Runtime provenance for stable, editable, and source-tree installations."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from importlib import metadata
import json
from pathlib import Path
import sys
from typing import Sequence
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from ._version import __version__


_DISTRIBUTION = "campaign-data"


@dataclass(frozen=True)
class RuntimeInfo:
    distribution: str
    version: str
    install_mode: str
    module_file: str
    python_executable: str
    python_prefix: str
    direct_url: str | None = None
    editable_root: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


def _distribution_direct_url() -> dict | None:
    try:
        distribution = metadata.distribution(_DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return None
    raw = distribution.read_text("direct_url.json")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _file_url_path(url: str | None) -> Path | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None
    path_text = url2pathname(unquote(parsed.path))
    if parsed.netloc:
        path_text = f"//{parsed.netloc}{path_text}"
    try:
        return Path(path_text).resolve()
    except OSError:
        return None


def _source_root(module_file: str) -> Path | None:
    try:
        module_path = Path(module_file).resolve()
    except OSError:
        return None
    for parent in module_path.parents:
        if parent.name == "src" and (parent.parent / "pyproject.toml").is_file():
            return parent.parent.resolve()
    return None


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _classify_install_mode(module_file: str, direct_url: dict | None) -> tuple[str, str | None]:
    module_path = Path(module_file).resolve()
    source_root = _source_root(module_file)
    direct_url_text = direct_url.get("url") if direct_url else None
    editable_root = _file_url_path(direct_url_text)
    editable = bool(
        direct_url
        and isinstance(direct_url.get("dir_info"), dict)
        and direct_url["dir_info"].get("editable") is True
    )
    if ".whl" in module_file.lower():
        return "wheel-archive", None
    if source_root is not None:
        if editable and editable_root is not None and _is_within(module_path, editable_root):
            return "editable", str(editable_root)
        return "source-tree", None
    if editable:
        return "editable", str(editable_root) if editable_root is not None else None
    if direct_url and (
        isinstance(direct_url.get("archive_info"), dict)
        or str(direct_url_text or "").lower().endswith(".whl")
    ):
        return "wheel", None
    return "installed", None


def runtime_info() -> RuntimeInfo:
    module_file = str(Path(__file__).resolve())
    direct_url = _distribution_direct_url()
    install_mode, editable_root = _classify_install_mode(module_file, direct_url)
    return RuntimeInfo(
        distribution=_DISTRIBUTION,
        version=__version__,
        install_mode=install_mode,
        module_file=module_file,
        python_executable=str(Path(sys.executable).resolve()),
        python_prefix=str(Path(sys.prefix).resolve()),
        direct_url=direct_url.get("url") if direct_url else None,
        editable_root=editable_root,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report the imported campaign-data version and source.")
    parser.add_argument("--json", action="store_true", help="Emit JSON only.")
    args = parser.parse_args(argv)
    info = runtime_info()
    if args.json:
        print(json.dumps(info.to_dict(), ensure_ascii=True, sort_keys=True))
    else:
        for key, value in info.to_dict().items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
