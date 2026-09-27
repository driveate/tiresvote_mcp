#!/usr/bin/env python3
"""Standalone stdio protocol verifier for an installed tiresvote-mcp CLI.

This is a diagnostic tool, intentionally separate from the pytest offline
suite: it spawns a real CLI subprocess and drives the actual JSON-RPC
handshake over its stdin/stdout. All HTTP traffic is intercepted by a local
ephemeral mock on 127.0.0.1 — the script never reaches the public internet
and must never be pointed at it.

Properties:

- Stdlib only; works outside the source tree against a wheel-installed CLI.
- The child's WHEELSIZE_API_KEY is overridden with a SYNTHETIC key and
  TIRES_API_BASE_URL with the loopback mock. Real credentials in the parent
  environment are never read by value or forwarded — the key is replaced,
  not propagated.
- Asserts the exact v1 surface: 12 tires_* tools, 4 prompts, config://status.
- Asserts every stdout line is a JSON-RPC dict (protocol hygiene, including
  anything emitted during child shutdown) and the synthetic key appears ONLY
  inside the one allowed mock request — never in protocol traffic or stderr.
- Any unexpected upstream route or HTTP method fails the audit.
- No installs, no config mutation, bounded waits; the child process, pump
  threads and the mock server are always cleaned up in a ``finally`` block.
  Output is a single safe JSON summary; exit code is 0 only when every
  check passed.

Usage (against an installed CLI — this script performs no installs):
    python3 scripts/check_stdio.py -- tiresvote-mcp
    python3 scripts/check_stdio.py -- /path/to/venv/bin/tiresvote-mcp
    python3 scripts/check_stdio.py                     # <this python> -m tiresvote_mcp
    uv run --no-sync python scripts/check_stdio.py -- python -m tiresvote_mcp
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

SYNTHETIC_KEY = "check-stdio-synthetic-9f2c7a1d"

EXPECTED_TOOLS = {
    "tires_list_brands",
    "tires_list_brand_tires",
    "tires_get_tire",
    "tires_list_sizes",
    "tires_search",
    "tires_search_advanced",
    "tires_get_pros_cons",
    "tires_list_materials",
    "tires_list_tests",
    "tires_get_test",
    "tires_list_regions",
    "tires_list_performance_categories",
}
EXPECTED_PROMPTS = {
    "tire_selection_by_size",
    "tire_comparison",
    "tire_test_explainer",
    "tire_model_brief",
}

MAX_STDERR_LINES = 200
MAX_STDOUT_LINES = 500


class _EarlyExit(Exception):
    """Internal control flow: a stage already recorded its failure."""


class _MockAPI:
    """Loopback-only Tires API stand-in.

    Answers exactly one route — ``GET /v2/tires/catalog/`` — and records every
    (method, route, has_key) triple so the audit can reject unexpected
    requests. Query strings are never echoed back in diagnostics.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, bool]] = []

    def handler_class(self):
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass  # quiet

            def _record(self):
                route = urlsplit(self.path).path
                has_key = any(
                    k == "user_key" and v == SYNTHETIC_KEY
                    for k, v in parse_qsl(urlsplit(self.path).query)
                )
                requests.append((self.command, route, has_key))

            def _json(self, status: int, payload: dict):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._record()
                if urlsplit(self.path).path == "/v2/tires/catalog/":
                    self._json(
                        200,
                        {
                            "data": [
                                {
                                    "slug": "audit-brand",
                                    "display": "Audit Brand",
                                    "price_segment": None,
                                    "products_count": 1,
                                }
                            ],
                            "meta": {"count": 1},
                        },
                    )
                else:
                    self._json(404, {"detail": "unknown mock route"})

            def do_POST(self):
                self._record()
                self._json(405, {"detail": "method not allowed"})

            do_PUT = do_DELETE = do_PATCH = do_HEAD = do_POST

        return Handler


class _StdioProtocol:
    """Minimal JSON-RPC client over the child's stdin/stdout.

    Raw stdout/stderr lines are captured (bounded) for the post-exit audit:
    a misbehaving server could emit a stray line during shutdown, after the
    last response was consumed — the audit runs on every captured line, not
    only those seen mid-run.
    """

    def __init__(self, proc: subprocess.Popen, timeout: float) -> None:
        self._proc = proc
        self._timeout = timeout
        self.lines: queue.Queue[str] = queue.Queue()
        self.raw_stdout: list[str] = []
        self.stderr: list[str] = []
        self._pump_out = threading.Thread(
            target=self._pump_stdout, daemon=True, name="stdout-pump"
        )
        self._pump_err = threading.Thread(
            target=self._pump_stderr, daemon=True, name="stderr-pump"
        )
        self._pump_out.start()
        self._pump_err.start()

    def _pump_stdout(self) -> None:
        for line in self._proc.stdout:
            if len(self.raw_stdout) < MAX_STDOUT_LINES:
                self.raw_stdout.append(line)
            self.lines.put(line)

    def _pump_stderr(self) -> None:
        for line in self._proc.stderr:
            if len(self.stderr) < MAX_STDERR_LINES:
                self.stderr.append(line)

    def join_pumps(self, timeout: float = 5.0) -> None:
        """Wait until both pipe readers hit EOF (child closed its streams)."""
        self._pump_out.join(timeout)
        self._pump_err.join(timeout)

    def drain_queued(self) -> None:
        """Move any queued-but-unconsumed lines — pump already recorded them."""
        while True:
            try:
                self.lines.get_nowait()
            except queue.Empty:
                return

    @staticmethod
    def line_is_protocol(raw: str) -> bool:
        """A line is protocol-clean iff it is a JSON-RPC 2.0 *object*."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return False
        return isinstance(msg, dict) and msg.get("jsonrpc") == "2.0"

    def _next(self) -> dict:
        try:
            raw = self.lines.get(timeout=self._timeout)
        except queue.Empty:
            raise TimeoutError(
                f"no stdout line within {self._timeout:.0f}s"
            ) from None
        if not self.line_is_protocol(raw):
            raise RuntimeError("non-JSON-RPC line on stdout (protocol violation)")
        return json.loads(raw)

    def send(self, method: str, params: dict, ident: int | None = None) -> dict:
        msg: dict = {"jsonrpc": "2.0", "method": method, "params": params}
        if ident is not None:
            msg["id"] = ident
        try:
            self._proc.stdin.write(json.dumps(msg) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as exc:
            raise RuntimeError(f"child stdin closed early ({exc})") from None
        if ident is None:
            return {}
        while True:
            result = self._next()
            if result.get("id") == ident:
                if "error" in result:
                    error = result["error"]
                    message = error.get("message") if isinstance(error, dict) else error
                    raise RuntimeError(f"JSON-RPC error on {method}: {message}")
                result = result.get("result", {})
                if not isinstance(result, dict):
                    raise RuntimeError(f"non-object result for {method}")
                return result


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="check_stdio.py",
        description=__doc__.splitlines()[0],
        epilog="Everything after '--' is the CLI command to audit.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="per-response wait limit; finite positive (default 30)",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="CLI command after '--' (default: <this python> -m tiresvote_mcp)",
    )
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a finite positive number")
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        command = [sys.executable, "-m", "tiresvote_mcp"]
    args.command = command
    return args


def _stop_process(proc: subprocess.Popen | None) -> None:
    """Close stdin, wait briefly, then terminate -> kill. Always returns."""
    if proc is None:
        return
    try:
        if proc.stdin:
            proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    checks: dict[str, str] = {}
    init: dict | None = None
    proc: subprocess.Popen | None = None
    protocol: _StdioProtocol | None = None

    mock = _MockAPI()
    http = ThreadingHTTPServer(("127.0.0.1", 0), mock.handler_class())
    threading.Thread(target=http.serve_forever, daemon=True, name="mock-http").start()

    def fail(stage: str, detail: str) -> None:
        checks[stage] = f"failed: {detail}"

    try:
        env = dict(os.environ)
        env["WHEELSIZE_API_KEY"] = SYNTHETIC_KEY  # replace, never propagate a real key
        env["TIRES_API_BASE_URL"] = f"http://127.0.0.1:{http.server_port}"
        env.pop("TIRES_API_HOST_HEADER", None)

        try:
            proc = subprocess.Popen(
                args.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
        except OSError as exc:
            fail("spawn", f"{exc} (command: {args.command[0]!r})")
            proc = None

        if proc is None:
            raise _EarlyExit

        protocol = _StdioProtocol(proc, args.timeout)

        try:
            init = protocol.send(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "check-stdio", "version": "1"},
                },
                1,
            )
            server_name = init.get("serverInfo", {}).get("name")
            if server_name == "tiresvote":
                checks["initialize"] = "passed"
            else:
                fail("initialize", f"serverInfo.name={server_name!r}")
        except (RuntimeError, TimeoutError) as exc:
            fail("initialize", str(exc))
            init = None

        if init is not None:
            try:
                protocol.send("notifications/initialized", {})
            except RuntimeError as exc:
                fail("initialized_notification", str(exc))

            try:
                listing = protocol.send("tools/list", {}, 2)
                names = [t.get("name") for t in listing.get("tools", [])]
                if len(names) == 12 and set(names) == EXPECTED_TOOLS:
                    checks["tools_list"] = "passed"
                else:
                    extra = sorted(set(names) - EXPECTED_TOOLS)
                    missing = sorted(EXPECTED_TOOLS - set(names))
                    fail("tools_list", f"{len(names)} tools; missing={missing} extra={extra}")
            except (RuntimeError, TimeoutError) as exc:
                fail("tools_list", str(exc))

            try:
                prompts = protocol.send("prompts/list", {}, 3)
                prompt_names = [p.get("name") for p in prompts.get("prompts", [])]
                if len(prompt_names) == 4 and set(prompt_names) == EXPECTED_PROMPTS:
                    checks["prompts_list"] = "passed"
                else:
                    missing = sorted(EXPECTED_PROMPTS - set(prompt_names))
                    extra = sorted(set(prompt_names) - EXPECTED_PROMPTS)
                    fail(
                        "prompts_list",
                        f"{len(prompt_names)} prompts; missing={missing} extra={extra}",
                    )
            except (RuntimeError, TimeoutError) as exc:
                fail("prompts_list", str(exc))

            try:
                status = protocol.send("resources/read", {"uri": "config://status"}, 4)
                contents = status.get("contents", [])
                text = contents[0].get("text", "") if contents else ""
                if "api_key_configured" in text and SYNTHETIC_KEY not in text:
                    checks["resource_read"] = "passed"
                else:
                    fail("resource_read", "status payload missing api_key_configured")
            except (RuntimeError, TimeoutError) as exc:
                fail("resource_read", str(exc))

            try:
                call = protocol.send(
                    "tools/call",
                    {"name": "tires_list_brands", "arguments": {}},
                    5,
                )
                blob = json.dumps(call)
                if not call.get("isError") and "audit-brand" in blob:
                    checks["tool_call"] = "passed"
                else:
                    fail("tool_call", "call failed or audit brand missing")
            except (RuntimeError, TimeoutError) as exc:
                fail("tool_call", str(exc))

            # Mock routing: exactly one GET on the catalog route, carrying the
            # synthetic key — anything else is an unexpected upstream request.
            expected_route = "/v2/tires/catalog/"
            if (
                len(mock.requests) == 1
                and mock.requests[0][0] == "GET"
                and mock.requests[0][1] == expected_route
            ):
                checks["mock_route"] = "passed"
            else:
                seen = sorted({f"{m} {r}" for m, r, _ in mock.requests}) or ["none"]
                fail("mock_route", f"requests={seen}")

            if mock.requests and mock.requests[0][2]:
                checks["key_to_upstream"] = "passed"
            else:
                fail("key_to_upstream", "synthetic key did not reach the mock")

    except _EarlyExit:
        pass  # failure already recorded in checks
    except Exception as exc:  # never let an unexpected error skip cleanup
        fail("harness", f"{type(exc).__name__}: {exc}")
    finally:
        _stop_process(proc)
        # Only after the child exits are stdout/stderr complete: join the pump
        # threads, drain the queue, THEN audit every captured line.
        if protocol is not None:
            protocol.join_pumps()
            protocol.drain_queued()
            bad = [ln.strip()[:120] for ln in protocol.raw_stdout
                   if not _StdioProtocol.line_is_protocol(ln)]
            if protocol.raw_stdout and not bad:
                checks["stdout_protocol"] = "passed"
            elif not protocol.raw_stdout and init is None:
                pass  # spawn/init already failed; nothing to audit
            else:
                fail("stdout_protocol", f"non-protocol stdout lines: {bad[:3]}")

            leaked_stdout = any(SYNTHETIC_KEY in ln for ln in protocol.raw_stdout)
            leaked_stderr = any(SYNTHETIC_KEY in ln for ln in protocol.stderr)
            if leaked_stdout or leaked_stderr:
                where = "stdout" if leaked_stdout else "stderr"
                fail("secret_redaction", f"synthetic key found on {where}")
            elif init is not None:
                checks["secret_redaction"] = "passed"
        http.shutdown()
        http.server_close()

    ok = init is not None and bool(checks) and all(v == "passed" for v in checks.values())
    print(
        json.dumps(
            {"ok": ok, "server": (init or {}).get("serverInfo"), "checks": checks},
            sort_keys=True,
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
