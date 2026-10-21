"""Entry point for the frozen WUP_Explorer executable (PyInstaller)."""
import multiprocessing
import sys

from aorc_tools.explorer.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
