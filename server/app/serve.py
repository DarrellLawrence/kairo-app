"""Production entry point: `python -m app.serve --env-file <data>/kairo.env`.

On kairo-home the Windows boot task runs exactly this. It binds 127.0.0.1
only (no public ports; Tailscale serve publishes it to the tailnet) and logs
to <data>/logs/kairo.log with rotation.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
from pathlib import Path

HOST = "127.0.0.1"  # never configurable: no public ports (KICKOFF 2)


def load_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines; '#' comments; optional surrounding quotes. Values win over the inherited env."""
    loaded: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        loaded[key] = value
    os.environ.update(loaded)
    return loaded


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "kairo.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    handlers: list[logging.Handler] = [file_handler]
    if sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        handlers.append(stream)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Kairo API on 127.0.0.1.")
    parser.add_argument("--env-file", type=Path, help="kairo.env written by the installer")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    if args.env_file:
        load_env_file(args.env_file)

    import uvicorn

    from .settings import get_settings

    settings = get_settings()
    setup_logging(settings.data_dir / "logs")
    port = args.port or int(os.environ.get("KAIRO_PORT", "8000"))
    logging.getLogger("kairo").info("starting on %s:%s, data in %s", HOST, port, settings.data_dir)
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=HOST,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        log_config=None,
    )


if __name__ == "__main__":
    main()
