"""``nuroflow-api`` console script: run the API with uvicorn.

Local:      nuroflow-api --reload
Container:  nuroflow-api --host 0.0.0.0 --workers 2 --proxy-headers
"""

from __future__ import annotations

import argparse

import uvicorn


def run() -> None:
    parser = argparse.ArgumentParser(prog="nuroflow-api", description="Run the NuroFlow API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes (dev)")
    parser.add_argument(
        "--proxy-headers",
        action="store_true",
        help="trust X-Forwarded-* from --forwarded-allow-ips (use behind a reverse proxy)",
    )
    parser.add_argument("--forwarded-allow-ips", default="127.0.0.1")
    args = parser.parse_args()

    uvicorn.run(
        "nuroflow.api.main:app_factory",
        factory=True,
        host=args.host,
        port=args.port,
        workers=None if args.reload else args.workers,
        reload=args.reload,
        proxy_headers=args.proxy_headers,
        forwarded_allow_ips=args.forwarded_allow_ips,
        server_header=False,  # don't advertise the server software
        access_log=False,  # RequestContextMiddleware writes a structured access log
        log_config=None,  # keep our logging configuration
    )


if __name__ == "__main__":
    run()
