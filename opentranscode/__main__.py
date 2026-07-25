"""``python -m opentranscode`` entry point.

Delegates to :func:`opentranscode.cli.main`, then propagates the returned
exit code via :func:`sys.exit`. Defined as a ``main()`` function (not inline
code) so it can be referenced as the
``opentranscode = opentranscode.__main__:main`` console-script entry point
in ``pyproject.toml``.
"""

from __future__ import annotations

import sys

from .cli import main as cli_main


def main() -> int:
    """Module entry point — equivalent to ``opentranscode.cli.main()``."""
    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
