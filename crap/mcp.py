from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from pathlib import Path

from .util import process_options, terminate_tree


class MCPError(RuntimeError):
    pass


class StdioMCP:
    """Small synchronous JSON-RPC client. Notifications never count as replies."""
    def __init__(self, command: list[str], cwd: Path, env: dict, stderr: Path, timeout=90):
        self.command, self.cwd, self.env, self.stderr = command, cwd, env, stderr
        self.timeout = timeout
        self.sequence = 0
        self.messages = queue.Queue()

    def __enter__(self):
        self.stderr.parent.mkdir(parents=True, exist_ok=True)
        self.log = self.stderr.open("ab")
        try:
            self.proc = subprocess.Popen(self.command, cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=self.log, text=True, encoding="utf-8",
                                         **process_options())
            self.reader = threading.Thread(target=self._read, daemon=True)
            self.reader.start()
            response = self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                    "clientInfo": {"name": "crap", "version": "0.1.0"}})
            if response.get("protocolVersion") not in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
                raise MCPError("Unsupported MCP protocol version")
            self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _read(self):
        try:
            for line in self.proc.stdout:
                try:
                    self.messages.put(json.loads(line))
                except json.JSONDecodeError:
                    self.messages.put(MCPError("MCP server wrote non-JSON to stdout"))
        finally:
            self.messages.put(MCPError("MCP server disconnected"))

    def send(self, message):
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict) -> dict:
        self.sequence += 1
        request_id = self.sequence
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        end = time.monotonic() + self.timeout
        while True:
            try:
                message = self.messages.get(timeout=max(0, end - time.monotonic()))
            except queue.Empty as exc:
                raise MCPError(f"MCP request timed out: {method}") from exc
            if isinstance(message, Exception):
                raise message
            if "method" in message:
                if "id" in message:
                    if message["method"] == "ping":
                        self.send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
                    else:
                        self.send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "Client capability not supported"}})
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise MCPError(str(message["error"]))
            return message["result"]

    def tools(self) -> list[dict]:
        result, cursor = [], None
        while True:
            page = self.request("tools/list", {"cursor": cursor} if cursor else {})
            result.extend(page["tools"])
            cursor = page.get("nextCursor")
            if not cursor:
                return result

    def call(self, name: str, arguments: dict) -> dict:
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise MCPError(json.dumps(result))
        value = result.get("structuredContent")
        if isinstance(value, dict):
            return value
        for item in result.get("content", []):
            if item.get("type") == "text":
                try:
                    value = json.loads(item["text"])
                    if isinstance(value, dict):
                        return value
                except json.JSONDecodeError:
                    pass
        raise MCPError("Expected a structured JSON tool result")

    def __exit__(self, *args):
        if hasattr(self, "proc"):
            terminate_tree(self.proc)
            for stream in (self.proc.stdin, self.proc.stdout):
                if stream:
                    stream.close()
            if hasattr(self, "reader"):
                self.reader.join(timeout=2)
        self.log.close()
