"""Adds ../_lib to sys.path so a skill can `from jira_client import ...`."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "_lib")))
