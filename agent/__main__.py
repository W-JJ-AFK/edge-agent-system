"""让 `python -m agent` 等价于 `python -m agent.cli`。"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
