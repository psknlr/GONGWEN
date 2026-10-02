"""评测：回归用例、基线对比与消融（设计 §10）。"""

from .runner import FLAGS, GROUPS, load_cases, run_case, run_suite, summarize, to_markdown, variants

__all__ = ["FLAGS", "GROUPS", "load_cases", "run_case", "run_suite", "summarize", "to_markdown", "variants"]
