"""python -m hyxlab.simui [--port 8877] — interactive market-replay UI."""

from __future__ import annotations

import argparse
import asyncio
import contextlib

from hyxlab.simui import session as sess
from hyxlab.simui.server import run


def main() -> None:
    ap = argparse.ArgumentParser(description="hyxlab simui (paper-trading replay UI)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8877)
    ap.add_argument("--stream-db", default=sess.STREAM_DB)
    ap.add_argument("--archive-db", default=sess.ARCHIVE_DB)
    args = ap.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(args.host, args.port, args.stream_db, args.archive_db))


if __name__ == "__main__":
    main()
