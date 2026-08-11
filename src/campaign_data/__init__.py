"""Public API for tabular campaign analysis and consolidation."""

from ._version import __version__
from .audit import AuditReport, audit_directory, format_audit_report
from .directory_index import DirectoryIndex, build_directory_index, dump_directory_index
from .grid_analysis import GridReport, analyse_file, analyse_files
from .io_utils import collect_paths, detect_header_len, read_and_parse, read_csv, read_tsv, write_tsv
from .merge import AnalysisResult, agent_quick_merge, analyse_directory, simple_merge, smart_merge
from .runtime import RuntimeInfo, runtime_info
from .splitter import SplitPlan, SplitResult, analyze_split_options, execute_split, plan_split, smart_merge_and_split

__all__ = [
    "AnalysisResult",
    "AuditReport",
    "DirectoryIndex",
    "GridReport",
    "RuntimeInfo",
    "SplitPlan",
    "SplitResult",
    "__version__",
    "agent_quick_merge",
    "analyse_directory",
    "analyse_file",
    "analyse_files",
    "analyze_split_options",
    "audit_directory",
    "build_directory_index",
    "collect_paths",
    "detect_header_len",
    "dump_directory_index",
    "execute_split",
    "format_audit_report",
    "plan_split",
    "read_and_parse",
    "read_csv",
    "read_tsv",
    "runtime_info",
    "simple_merge",
    "smart_merge",
    "smart_merge_and_split",
    "write_tsv",
]
