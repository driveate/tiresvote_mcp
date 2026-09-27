#!/usr/bin/env python3
"""Read-only live contract probe for the public TiresVote catalog API.

Executes a bounded set of documented GET requests against
``https://api.wheel-size.com/v2/tires/`` and records sanitized responses
for contract analysis.

Usage (explicit opt-in — ``--live`` and the env key are both required):

    export WHEELSIZE_API_KEY=...        # credentials via env only
    python3 scripts/probe_live_contracts.py --live --stage auth
    python3 scripts/probe_live_contracts.py --live --probe-file probes.json
    python3 scripts/probe_live_contracts.py --dry-run --stage auth

Safety properties:

- GET requests only. Paths must exactly match one of the 12 documented
  public routes below; query strings, fragments, traversal and encoded
  separators are rejected before any network I/O.
- Redirects are never followed: 3xx is recorded as evidence and the API
  key is never sent to a redirect destination.
- The API key is read from ``WHEELSIZE_API_KEY`` in the environment, sent
  as the ``user_key`` query parameter and recursively redacted from every
  stored or printed string, header and parameter. Request URLs are never
  printed or persisted.
- ``--probe-file`` entries may not set ``user_key`` or any credential-like
  query parameter, nor ``no_key``/``key_override`` (reserved for the
  built-in ``auth`` stage fixtures).
- Requests are serial with a fixed delay and a hard per-invocation budget.
- 4xx responses are never retried; API-supplied pagination links are never
  followed automatically — pages are requested explicitly by number.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = "https://api.wheel-size.com"
PREFIX = "/v2/tires"
DELAY_S = 0.6
TIMEOUT_S = 30.0
MAX_REQUESTS_PER_RUN = 30
USER_AGENT = "tiresvote-mcp-contract-probe/0.1"

_SLUG = r"[a-z0-9]+(?:[-_][a-z0-9]+)*"
ALLOWED_ROUTES = tuple(
    re.compile(p)
    for p in (
        r"/catalog/",
        rf"/catalog/{_SLUG}/",
        rf"/catalog/{_SLUG}/{_SLUG}/",
        rf"/catalog/{_SLUG}/{_SLUG}/(?:bnb|modes|materials)/",
        r"/search/",
        r"/search/advanced/",
        r"/regions/",
        r"/performance-categories/",
        r"/tests/",
        rf"/tests/{_SLUG}/",
    )
)

# Rejected in probe params: the auth parameter itself and anything that
# smells like a credential or signature.
_FORBIDDEN_PARAM = re.compile(
    r"(?i)(user_?key|api_?key|token|secret|passw|auth|credential|signature)"
)
# Rejected outright in any path component.
_FORBIDDEN_PATH = re.compile(r"[?#%\\]|\.\.|//")


def default_out():
    """Default output inside the git dir when it is one; else require --out."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    gitdir = os.path.join(root, ".git")
    if os.path.isdir(gitdir):
        return os.path.join(gitdir, "coordination", "live-fixtures.json")
    return None


def validate_path(path):
    """Allow only the documented public GET routes, nothing else."""
    if not isinstance(path, str) or not path.startswith("/"):
        raise SystemExit(f"probe path must be an absolute path: {path!r}")
    if _FORBIDDEN_PATH.search(path):
        raise SystemExit(f"probe path rejected (query/fragment/encoding): {path!r}")
    if not any(r.fullmatch(path) for r in ALLOWED_ROUTES):
        raise SystemExit(f"probe path outside documented routes: {path!r}")
    return path


def validate_params(params, probe_id):
    """Check query params: scalar/list values only, no credential keys."""
    if not isinstance(params, dict):
        raise SystemExit(f"probe {probe_id}: params must be an object")
    for key, value in params.items():
        if not isinstance(key, str) or _FORBIDDEN_PARAM.search(key):
            raise SystemExit(
                f"probe {probe_id}: forbidden query parameter {key!r}")
        values = value if isinstance(value, list) else [value]
        for item in values:
            if not isinstance(item, (str, int, float, bool)):
                raise SystemExit(
                    f"probe {probe_id}: non-scalar value for {key!r}")


def validate_probe(probe, from_file):
    if not isinstance(probe, dict) or not isinstance(probe.get("id"), str) \
            or not isinstance(probe.get("path"), str):
        raise SystemExit("each probe needs string 'id' and 'path'")
    if from_file and any(k in probe for k in ("no_key", "key_override")):
        raise SystemExit(
            f"probe {probe.get('id')}: no_key/key_override are reserved "
            "for the built-in auth stage")
    validate_params(probe.get("params", {}), probe["id"])


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects; record 3xx as evidence instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)

# (path_template, {param: value|list}) — only documented public GET paths.
PROBES: dict[str, list[dict]] = {
    "auth": [
        {"id": "auth-no-key", "path": "/catalog/", "params": {},
         "no_key": True},
        {"id": "auth-bad-key", "path": "/catalog/", "params": {},
         "key_override": "invalid-probe-key-0000"},
        {"id": "auth-ok", "path": "/catalog/", "params": {}},
    ],
    "brands": [
        {"id": "brands-all", "path": "/catalog/", "params": {}},
        {"id": "brands-premium", "path": "/catalog/",
         "params": {"price_segment": "premium"}},
    ],
    "shapes": [
        {"id": "brand-catalog", "path": "/catalog/{brand}/",
         "params": {}},
        {"id": "product-detail", "path": "/catalog/{brand}/{product}/",
         "params": {}},
        {"id": "product-modes", "path": "/catalog/{brand}/{product}/modes/",
         "params": {}},
        {"id": "product-bnb", "path": "/catalog/{brand}/{product}/bnb/",
         "params": {}},
        {"id": "product-materials",
         "path": "/catalog/{brand}/{product}/materials/", "params": {}},
        {"id": "query-search", "path": "/search/",
         "params": {"query": None, "per_page": 5}},
        {"id": "adv-search", "path": "/search/advanced/",
         "params": {"per_page": 5}},
        {"id": "regions", "path": "/regions/", "params": {}},
        {"id": "perf-cats", "path": "/performance-categories/", "params": {}},
        {"id": "tests-list", "path": "/tests/", "params": {"per_page": 5}},
        {"id": "test-detail", "path": "/tests/{slug}/", "params": {}},
    ],
}


def scrub(value, secret):
    """Recursively remove the API key (and user_key= pairs) from data."""
    if isinstance(value, dict):
        return {k: scrub(v, secret) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, secret) for v in value]
    if isinstance(value, str):
        out = value
        if secret:
            out = out.replace(secret, "<redacted>")
        if "user_key=" in out:
            out = re.sub(r"user_key=[^&\s\"']*", "user_key=<redacted>", out)
        return out
    return value


def interpolate(path, probe, args):
    for name in ("brand", "product", "slug"):
        if "{%s}" % name in path:
            val = getattr(args, name, None) or probe.get(name)
            if not val:
                raise SystemExit(f"probe {probe['id']} needs --{name}")
            path = path.replace("{%s}" % name, str(val))
    return path


def run_probe(client, probe, args):
    path = interpolate(probe["path"], probe, args)
    params = {}
    for k, v in probe.get("params", {}).items():
        if v is None:
            v = getattr(args, k, None) or probe.get(k)
        if v is not None:
            params[k] = v
    return client.get(probe["id"], path, params,
                      no_key=probe.get("no_key"),
                      key_override=probe.get("key_override"))


class Client:
    def __init__(self, key, budget):
        self.key = key
        self.budget = budget
        self.count = 0
        self.results = []

    def get(self, probe_id, path, params, no_key=False, key_override=None):
        if self.count >= self.budget:
            print(f"budget exhausted, skipped {probe_id}", file=sys.stderr)
            return None
        validate_path(path)
        query = []
        key = None if no_key else (key_override or self.key)
        if key:
            query.append(("user_key", key))
        for k, v in params.items():
            for item in (v if isinstance(v, list) else [v]):
                query.append((k, str(item)))
        url = BASE + PREFIX + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        self.count += 1
        started = time.monotonic()
        status, headers, body_bytes = None, {}, b""
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT,
                              "Accept": "application/json"})
            with OPENER.open(req, timeout=TIMEOUT_S) as resp:
                status = resp.status
                headers = dict(resp.headers.items())
                body_bytes = resp.read(2_000_000)
        except urllib.error.HTTPError as exc:
            status = exc.code
            headers = dict(exc.headers.items()) if exc.headers else {}
            body_bytes = exc.read(2_000_000)
        except Exception as exc:  # transport error — record, don't retry
            status = -1
            body_bytes = repr(exc).encode()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        try:
            body = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            body = {"_raw_text": body_bytes.decode("utf-8", "replace")[:2000]}
        record = {
            "id": probe_id,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "request": {"method": "GET", "path": PREFIX + path,
                        "params": scrub(params, self.key)},
            "status": status,
            "elapsed_ms": elapsed_ms,
            "headers": scrub({k: v for k, v in headers.items()
                              if k.lower() in ("content-type", "cache-control",
                                               "retry-after", "allow", "vary",
                                               "location")}, self.key),
            "body": scrub(body, self.key),
        }
        self.results.append(record)
        print(f"[{self.count}] {probe_id}: {status} "
              f"({elapsed_ms} ms) {PREFIX}{path} "
              f"{scrub(params, self.key)}", file=sys.stderr)
        time.sleep(DELAY_S)
        return record


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="probes run serially with a fixed delay; at most "
               f"{MAX_REQUESTS_PER_RUN} requests per invocation")
    ap.add_argument("--live", action="store_true",
                    help="required opt-in to perform network requests")
    ap.add_argument("--stage", choices=sorted(PROBES),
                    help="run a built-in probe stage")
    ap.add_argument("--probe-file",
                    help="JSON file with a list of probe dicts "
                         "({id, path, params}) — no credential params")
    ap.add_argument("--brand", help="value for the {brand} path placeholder")
    ap.add_argument("--product", help="value for the {product} placeholder")
    ap.add_argument("--slug", help="value for the {slug} placeholder")
    ap.add_argument("--query", help="search query used by the shapes stage")
    ap.add_argument("--budget", type=int, default=MAX_REQUESTS_PER_RUN,
                    help=f"max requests this run (1..{MAX_REQUESTS_PER_RUN})")
    ap.add_argument("--out", default=None,
                    help="output JSON path (default: "
                         ".git/coordination/live-fixtures.json when .git is "
                         "a directory)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the request plan without network I/O")
    args = ap.parse_args()

    if not args.live and not args.dry_run:
        ap.error("refusing to run without --live (or --dry-run)")
    if args.budget < 1 or args.budget > MAX_REQUESTS_PER_RUN:
        ap.error(f"--budget must be within 1..{MAX_REQUESTS_PER_RUN}")
    out = args.out or default_out()
    if not args.dry_run and not out:
        ap.error("no default output location (.git is not a directory); "
                 "pass --out explicitly")

    probes = []
    if args.stage:
        for p in PROBES[args.stage]:
            validate_probe(p, from_file=False)
        probes.extend(PROBES[args.stage])
    if args.probe_file:
        with open(args.probe_file) as fh:
            file_probes = json.load(fh)
        if not isinstance(file_probes, list):
            ap.error("--probe-file must contain a JSON list")
        for p in file_probes:
            validate_probe(p, from_file=True)
        probes.extend(file_probes)
    if not probes:
        ap.error("no probes selected: pass --stage and/or --probe-file")

    if args.dry_run:
        for p in probes:
            print(p["id"], p["path"], p.get("params", {}))
        return

    key = os.environ.get("WHEELSIZE_API_KEY")
    if not key:
        ap.error("WHEELSIZE_API_KEY is not set in the environment")

    client = Client(key, args.budget)
    for probe in probes:
        run_probe(client, probe, args)

    existing = []
    if os.path.exists(out):
        with open(out) as fh:
            try:
                existing = json.load(fh).get("records", [])
            except Exception:
                existing = []
    parent = os.path.dirname(out)
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = {
        "note": "sanitized live responses; user_key redacted; "
                "see docs/live-validation.md",
        "records": existing + client.results,
    }
    tmp = out + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, out)
    print(f"wrote {len(client.results)} records to {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
