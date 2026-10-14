"""Entry point.

    python -m simulator                         # web UI on http://127.0.0.1:8088
    python -m simulator --port 9000 --host 0.0.0.0
    python -m simulator --headless              # start every enabled instance from config.yaml, no UI
    python -m simulator --headless --export-tags tagmaps
"""

import argparse

from simulator.runner import headless


def main() -> None:
    ap = argparse.ArgumentParser(prog="simulator", description="Botond's Industrial Data Protocol Simulator")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--headless", action="store_true", help="no web UI: start all enabled instances")
    ap.add_argument("--host", default="127.0.0.1", help="web UI bind address (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8088, help="web UI port (default 8088)")
    ap.add_argument("--export-tags", metavar="DIR", help="headless: write a CSV tag map per instance into DIR")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    if args.headless:
        headless(args.config, args.export_tags, args.verbose)
    else:
        from simulator.manager import serve
        serve(args.config, args.host, args.port)


if __name__ == "__main__":
    main()
