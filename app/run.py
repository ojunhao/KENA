"""Launch KENA with settings from .env and the environment (see docs/setup-local-live.md). Defaults are local and simulated."""
import argparse
import dataclasses
import logging
import uvicorn
from .config import Settings, load_env_file


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", choices=["local", "claude", "agentcore"], help="Override KENA_AGENT")
    parser.add_argument("--live", action="store_true", help="Send real messages through AWS CDS (same as KENA_LIVE=1)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    load_env_file()
    from .main import create_app  # imported after .env is loaded
    settings = Settings.from_env()
    if args.agent:
        settings = dataclasses.replace(settings, agent=args.agent)
    if args.live:
        settings = dataclasses.replace(settings, live=True)
    if settings.host_is_public(args.host) and not settings.basic_auth:
        parser.error("Set KENA_BASIC_AUTH=user:password before binding to a public interface.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(create_app(settings=settings), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
