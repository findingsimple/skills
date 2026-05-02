"""Adds ../_lib to sys.path so a skill can `from jira_client import ...`.

Each client-importing script does `import _libpath  # noqa: F401` before its
`from jira_client import ...` (or gitlab/confluence/bonusly). Keeps skill dirs
runnable as plain scripts (`python3 setup.py`) without a package install step.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "_lib")))
