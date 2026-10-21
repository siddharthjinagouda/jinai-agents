"""Entry point inside the sandbox: python -I boot.py <agent_dir>"""
import importlib.util
import json
import os
import sys
import traceback

LIB = os.path.dirname(os.path.abspath(__file__))
agent_dir = os.path.abspath(sys.argv[1])
sys.path[:0] = [agent_dir, LIB]
os.chdir(agent_dir)

import jinai  # noqa: E402

io_dir = os.environ.get("JINAI_IO", ".")
with open(os.path.join(io_dir, "input.json"), encoding="utf-8") as f:
    inp = json.load(f)

try:
    path = os.path.join(agent_dir, "agent.py")
    if not os.path.exists(path):
        raise jinai.JinaiError("agent.py not found")
    spec = importlib.util.spec_from_file_location("agent", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent"] = mod
    spec.loader.exec_module(mod)
    if not callable(getattr(mod, "run", None)):
        raise jinai.JinaiError("agent.py must define run(event)")
    out = mod.run(inp.get("event") or {})
    jinai._finish(out)
except BaseException as e:  # noqa: BLE001
    jinai._fail(e, traceback.format_exc())
    sys.exit(1)
