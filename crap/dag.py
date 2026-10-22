from __future__ import annotations

import json
from pathlib import Path

from .store import Store
from .util import digest, ident, safe_file, source_digest, write_json


def read_graph(work: Path) -> dict:
    graph = json.loads(safe_file(work, "research/dag.json").read_text(encoding="utf-8"))
    if graph.get("version") != 1 or not isinstance(graph.get("nodes"), list):
        raise ValueError("DAG requires version=1 and a nodes array")
    nodes = {}
    for n in graph["nodes"]:
        ident(n["id"])
        if n["id"] in nodes:
            raise ValueError(f"Duplicate node: {n['id']}")
        if not isinstance(n.get("statement"), str) or not n["statement"].strip():
            raise ValueError(f"Node {n['id']} needs a mathematical statement")
        if not isinstance(n.get("depends_on", []), list):
            raise ValueError("depends_on must be a list")
        if n.get("owner", "codex") not in ("codex", "aristotle"):
            raise ValueError("Node owner must be codex or aristotle")
        for key in ("declaration", "lean_type", "lean_file", "proof_sketch"):
            if not isinstance(n.get(key, ""), str):
                raise ValueError(f"{key} must be a string")
        if n.get("lean_file"):
            p = n["lean_file"]
            if not (p == "Submission.lean" or (p.startswith("Submission/") and p.endswith(".lean"))):
                raise ValueError("Node code must live in Submission.lean or Submission/*.lean")
            safe_file(work, p)
        if n.get("declaration") and any(c in n["declaration"] for c in "\r\n; \t"):
            raise ValueError("declaration must be a single Lean name")
        for key in ("check", "delegate"):
            if not isinstance(n.get(key, False), bool):
                raise ValueError(f"{key} must be a boolean")
        nodes[n["id"]] = n
    targets = graph.get("targets", [])
    if not isinstance(targets, list) or any(t not in nodes for t in targets):
        raise ValueError("Every DAG target must refer to a node")
    active, done, ordered = set(), set(), []

    def visit(key):
        if key not in nodes:
            raise ValueError(f"Unknown dependency: {key}")
        if key in active:
            raise ValueError(f"Dependency cycle at {key}")
        if key in done:
            return
        active.add(key)
        for dep in nodes[key].get("depends_on", []):
            visit(dep)
        active.remove(key)
        done.add(key)
        ordered.append(nodes[key])

    for key in nodes:
        visit(key)
    graph["nodes"] = ordered
    return graph


def sync(store: Store, problem: str, work: Path) -> dict:
    graph = read_graph(work)
    # Conservative invalidation: a local helper can be imported indirectly or by
    # generated Lean code. Recheck all nodes when any submission source changes.
    code_hash = source_digest(work)
    previous = {r["id"]: r for r in store.rows("SELECT * FROM nodes WHERE problem=?", (problem,))}
    hashes = {}
    current = []
    for node in graph["nodes"]:
        key = node["id"]
        contract = {k: node.get(k) for k in ("statement", "lean_type", "declaration", "lean_file", "depends_on")}
        fingerprint = digest([contract, code_hash, [hashes[d] for d in node.get("depends_on", [])]])
        hashes[key] = fingerprint
        old = previous.get(key)
        status = old["status"] if old and old["fingerprint"] == fingerprint else "planned"
        detail = old["detail"] if old and old["fingerprint"] == fingerprint else ""
        if old and old["fingerprint"] != fingerprint:
            status = "stale"
        store.execute("INSERT OR REPLACE INTO nodes(problem,id,fingerprint,status,payload,detail) VALUES(?,?,?,?,?,?)",
                      (problem, key, fingerprint, status, json.dumps(node, ensure_ascii=False), detail))
        current.append(key)
    for key in previous.keys() - set(current):
        store.execute("DELETE FROM nodes WHERE problem=? AND id=?", (problem, key))
    publish(store, problem, work)
    return graph


def publish(store: Store, problem: str, work: Path):
    rows = store.rows("SELECT id,status,fingerprint,detail,payload FROM nodes WHERE problem=?", (problem,))
    states = {r["id"]: r["status"] for r in rows}
    for row in rows:
        n = json.loads(row.pop("payload"))
        row["ready"] = all(states.get(d) == "checked" for d in n.get("depends_on", []))
    write_json(safe_file(work, "research/node-status.json"), rows)


def set_status(store, problem, node, status, detail=""):
    store.execute("UPDATE nodes SET status=?,detail=? WHERE problem=? AND id=?", (status, detail, problem, node))
