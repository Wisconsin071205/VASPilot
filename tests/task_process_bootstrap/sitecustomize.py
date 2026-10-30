"""Opt-in bootstrap for real child-process tests; never on production paths."""
import os

if os.environ.get("RSCI_TASK_TEST_STATE"):
    from tests.task_process_support import install
    install()
