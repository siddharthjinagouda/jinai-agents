"""python -m jinai_agents serve [--host 0.0.0.0] [--port 8300]"""
import argparse
import logging

import uvicorn


def main():
    p = argparse.ArgumentParser(prog="jinai-agents")
    sub = p.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve", help="run the API, web app, scheduler and runner")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8300)
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if a.cmd == "serve":
        uvicorn.run("jinai_agents.api:app", host=a.host, port=a.port, workers=1)


if __name__ == "__main__":
    main()
