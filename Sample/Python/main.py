"""Compatibility entry point for the modular TTB strategy runner.

Use ``python main.py --help`` for the available monitoring, strategy and
virtual-order options.  Reusable API calls live under the ``ttb`` package.
"""

import sys

from sim_hft_bot import main


if __name__ == "__main__":
    sys.exit(main())
