"""评测：回归用例、基线对比与消融（设计 §9）。"""

from .runner import DIRECT, FLAGS, GROUPS, TASK_GROUPS, export_blind_review, load_cases, run_case, run_direct, run_suite, summarize, to_markdown, variants

__all__ = ["DIRECT", "FLAGS", "GROUPS", "TASK_GROUPS", "export_blind_review", "load_cases", "run_case", "run_direct", "run_suite", "summarize", "to_markdown", "variants"]
