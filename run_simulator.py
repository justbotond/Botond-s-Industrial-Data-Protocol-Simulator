"""Entry point for the packaged build (PyInstaller). Same options as `python -m simulator`."""

import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()  # must run first: worker processes re-enter this exe
    if "--opcda-standalone" in sys.argv:  # started by COM when an OPC DA client activates the ProgID
        from simulator.opcda_server import _standalone
        _standalone()
    else:
        from simulator.__main__ import main
        main()
