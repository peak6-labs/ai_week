#!/usr/bin/env python
"""Launch script for the Kalshi Trader FastAPI dashboard."""

import sys
import socket
import argparse
import uvicorn


def get_local_ip():
    """Get the local IP address for network access."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def main():
    parser = argparse.ArgumentParser(
        description="Start the Kalshi Trader FastAPI dashboard"
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="Port to run on (default: 8000)"
    )
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Host to bind to (default: 0.0.0.0)",
    )

    args = parser.parse_args()

    local_ip = get_local_ip()
    print(f"Dashboard running at http://{args.host}:{args.port}")
    print(f"Local network access: http://{local_ip}:{args.port}")

    uvicorn.run(
        "kalshi_trader.ui.server:app",
        host=args.host,
        port=args.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
