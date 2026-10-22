"""Deterministic stdio MCP fixture. Stores jobs across client restarts."""
import json
import pathlib
import sys
import time

root = pathlib.Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
mode = sys.argv[2] if len(sys.argv) > 2 else "normal"
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [
            {"name": "prove", "inputSchema": {"type": "object", "properties": {"code": {}, "wait": {}, "hint": {}, "context_files": {}}}},
            {"name": "check_proof", "inputSchema": {"type": "object", "properties": {"project_id": {}}}},
        ]}
    elif method == "tools/call":
        params = request["params"]
        if params["name"] == "prove":
            job = "remote-" + str(len(list(root.glob("*.json"))) + 1)
            (root / (job + ".json")).write_text(json.dumps(params["arguments"]), encoding="utf-8")
            if mode == "drop":
                sys.exit(0)
            if mode == "hang":
                time.sleep(30)
            value = {"status": "submitted", "project_id": job}
        else:
            job = params["arguments"]["project_id"]
            args = json.loads((root / (job + ".json")).read_text(encoding="utf-8"))
            value = {"status": "proved", "project_id": job, "code": args["code"].replace("sorry", "decide")}
        result = {"content": [{"type": "text", "text": json.dumps(value)}]}
    else:
        result = {}
    # Verify the client ignores unrelated progress notifications.
    print(json.dumps({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}), flush=True)
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
