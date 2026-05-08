"""Tolerant JSON file loader used by skills' apply / report stages.

`load_json` returns `None` on FileNotFoundError (the file simply isn't there
yet — the previous pipeline stage hasn't run) and warns + returns `None` on
JSONDecodeError / OSError (the file is there but unreadable — most often a
truncated write or a permissions glitch).

Callers that want a missing or corrupt file to crash hard should use
`json.load` directly. Callers that want to distinguish "not yet" from
"corrupt" can re-check with `os.path.exists` after a `None` return.
"""

import json
import sys


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as e:
        print("WARNING: %s unreadable (%s)" % (path, e), file=sys.stderr)
        return None
