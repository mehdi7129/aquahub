"""Entry point: python3 -m aquahub_agent, with no pip dependencies."""
from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path
import signal
import sys
import threading

from .core import Application, Config
from .security import ApiError
from .server import UnixServer


def main(argv=None):
    parser = argparse.ArgumentParser(description="Agent local AquaHub")
    parser.add_argument("--socket", type=Path, default=Path("/run/aquahub/agent.sock"))
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/aquahub"))
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--docker-context", default="default", choices=["default"])
    parser.add_argument("--allow-uid", type=int, default=os.getuid())
    parser.add_argument("--no-poll", action="store_true")
    args = parser.parse_args(argv)
    os.umask(0o077)
    app = None
    server = None
    try:
        app = Application(Config(socket_path=args.socket, state_dir=args.state_dir,
                                 bundle_path=args.bundle, docker_context=args.docker_context,
                                 allow_uid=args.allow_uid, no_poll=args.no_poll))
        for name in ("drone", "updates", "portainer", "app_updates", "log_exports", "wireguard"):
            try:
                module = importlib.import_module(f"aquahub_agent.{name}")
            except ModuleNotFoundError as error:
                if error.name != f"aquahub_agent.{name}":
                    raise
                continue
            module.register(app)
        server = UnixServer(app)
        def stop(_signum, _frame):
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        server.serve_forever(poll_interval=.25)
    except (ApiError, ValueError, OSError) as error:
        print(f"AquaHub : {error}", file=sys.stderr)
        return 1
    finally:
        if server:
            server.server_close()
        if app:
            app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
