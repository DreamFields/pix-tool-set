"""Regression for the activity log and its viewer.

Runs against an isolated log directory so a developer's real history is untouched.

Covers what actually matters for "show me what I ran and what came back":

  1. every invocation is recorded, through both the CLI and call_tool
  2. failures are recorded too, not just successes
  3. the byte-offset cursor delivers exactly the new entries and nothing twice
  4. payloads are retrievable, and id traversal is refused
  5. digests mark collapsed containers unambiguously
  6. the exported snapshot is genuinely self-contained
  7. recording can be switched off, and never breaks the call it wraps
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

SESSION = "Tiled"
PORT = 8791

PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    if condition:
        PASSED.append(label)
        print(f"  PASS  {label}")
    else:
        FAILED.append(f"{label}: {detail}")
        print(f"  FAIL  {label} :: {detail}")
    return condition


def cli(args: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["pix-tool-set", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, shell=True,
    )


# ----------------------------------------------------------------------
def stage_record(env: dict, log_dir: Path) -> None:
    print("[1] both entry points record, including failures")
    cli(["frame-stats", "--session", SESSION], env)
    cli(["texture-info", "--resource-id", "999999999", "--session", SESSION], env)

    # call_tool must land in the same log as the CLI.
    os.environ["PIX_TOOL_SET_ACTIVITY_DIR"] = str(log_dir)
    from pix_tool_set import call_tool
    from pix_tool_set.engine import activity

    call_tool("activity-log", {"stats_only": True})

    entries = activity.read_all()
    tools = [e["tool"] for e in entries]
    check("cli success recorded", "frame-stats" in tools, str(tools))
    check("cli failure recorded",
          any(e["tool"] == "texture-info" and e["status"] == "error" for e in entries),
          str([(e["tool"], e["status"]) for e in entries]))
    check("call_tool recorded",
          any(e["entry"] == "python:call_tool" for e in entries),
          str([e["entry"] for e in entries]))
    check("command line reconstructed",
          any("pix-tool-set frame-stats" in (e.get("command") or "") for e in entries),
          str([e.get("command") for e in entries][:3]))
    check("durations are positive",
          all(float(e.get("duration_ms") or -1) >= 0 for e in entries))
    failed = next((e for e in entries if e["status"] == "error"), None)
    check("failure keeps its error code",
          bool(failed and (failed.get("error") or {}).get("code")),
          str(failed.get("error") if failed else None))


def stage_cursor(env: dict) -> None:
    print("[2] the byte cursor yields new entries once and only once")
    from pix_tool_set.engine import activity

    first, cursor, size = activity.read_since(0)
    check("initial read returns everything", len(first) >= 3, str(len(first)))
    check("cursor equals file size", cursor == size, f"{cursor} vs {size}")

    empty, same, _ = activity.read_since(cursor)
    check("re-reading at cursor yields nothing", empty == [], str(len(empty)))
    check("cursor does not drift", same == cursor, f"{same} vs {cursor}")

    cli(["activity-log", "--stats-only"], env)
    delta, moved, _ = activity.read_since(cursor)
    check("new call delivered incrementally", len(delta) == 1, str(len(delta)))
    check("cursor advanced", moved > cursor, f"{moved} vs {cursor}")

    over, reset, _ = activity.read_since(10 ** 9)
    check("overshooting the cursor restarts rather than erroring", len(over) > 0, str(len(over)))
    check("restart lands on a valid cursor", reset == moved, f"{reset} vs {moved}")


def stage_payload() -> None:
    print("[3] payloads are retrievable and ids are validated")
    from pix_tool_set.engine import activity

    entries = activity.read_all()
    record_id = entries[0]["id"]
    envelope = activity.read_payload(record_id)
    check("payload found", envelope is not None)
    check("payload is a full envelope",
          bool(envelope) and "status" in envelope and "data" in envelope,
          str(sorted(envelope or {})))
    check("traversal id refused", activity.read_payload("../../secrets") is None)
    check("unknown id refused", activity.read_payload("nope-not-here") is None)


def stage_digest() -> None:
    print("[4] digests mark collapsed containers unambiguously")
    from pix_tool_set.engine import activity

    entries = activity.read_all()
    blob = json.dumps(entries, ensure_ascii=False)
    check("no bare bracket placeholders", "items]" not in blob)
    check("no bare brace placeholders", '"{' not in blob.replace('"{}"', ""))
    nested = [e for e in entries if isinstance(e.get("summary"), dict)]
    marked = any(
        isinstance(v, str) and v.startswith(("<dict:", "<list:"))
        for e in nested for v in e["summary"].values()
    )
    check("collapsed values carry an explicit marker", marked or not nested)
    check("digest never carries a huge string",
          all(len(json.dumps(e.get("summary") or {}, ensure_ascii=False)) < 4000 for e in entries))


def stage_server(env: dict) -> None:
    print("[5] the local server serves the page, feed, payload and stats")
    from pix_tool_set.tools import activity_tools
    from http.server import ThreadingHTTPServer

    server = activity_tools._Server(("127.0.0.1", 0), activity_tools._Handler)
    bound = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{bound}"

    def get(path: str):
        with urllib.request.urlopen(base + path, timeout=10) as response:
            return response.status, response.read()

    try:
        status, body = get("/")
        check("page served", status == 200 and b"<!DOCTYPE html>" in body, str(status))
        check("page ships the bootstrap slot", b"__PIX_ACTIVITY_BOOTSTRAP__" in body)

        status, body = get("/api/feed?offset=0")
        feed = json.loads(body)
        check("feed served", status == 200 and len(feed["entries"]) > 0)
        check("feed reports a cursor", isinstance(feed.get("offset"), int))

        status, body = get(f"/api/feed?offset={feed['offset']}")
        check("feed at cursor is empty", json.loads(body)["entries"] == [])

        status, body = get("/api/feed?offset=not-a-number")
        check("bad cursor does not 500", status == 200, str(status))

        record_id = feed["entries"][0]["id"]
        status, body = get(f"/api/payload?id={record_id}")
        check("payload served", status == 200 and "envelope" in json.loads(body))

        try:
            get("/api/payload?id=../../etc/passwd")
            check("traversal rejected over HTTP", False, "request succeeded")
        except urllib.error.HTTPError as exc:
            check("traversal rejected over HTTP", exc.code == 404, str(exc.code))

        status, body = get("/api/stats")
        check("stats served", status == 200 and "total_calls" in json.loads(body))

        try:
            get("/nope")
            check("unknown route rejected", False, "request succeeded")
        except urllib.error.HTTPError as exc:
            check("unknown route rejected", exc.code == 404, str(exc.code))

        # Live behaviour: a call made while serving must reach a polling client.
        cursor = json.loads(urllib.request.urlopen(
            f"{base}/api/feed?offset=0", timeout=10).read())["offset"]
        cli(["activity-log", "--stats-only"], env)
        time.sleep(0.3)
        live = json.loads(get(f"/api/feed?offset={cursor}")[1])
        check("live call reaches a polling client", len(live["entries"]) == 1,
              str(len(live["entries"])))
    finally:
        server.shutdown()
        server.server_close()


def stage_export(env: dict, log_dir: Path) -> None:
    print("[6] the exported snapshot is self-contained")
    target = log_dir / "snapshot.html"
    proc = cli(["activity-viewer", "--export", str(target)], env)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        check("export succeeds", False, (proc.stdout or proc.stderr)[-300:])
        return
    check("export succeeds", payload.get("status") == "success", str(payload.get("error")))
    check("file written", target.exists())

    page = target.read_text(encoding="utf-8")
    check("bootstrap slot replaced", "/*__PIX_ACTIVITY_BOOTSTRAP__*/" not in page)
    # Renders are inlined as data URIs, so the rule is "nothing fetched over the network
    # or off the filesystem", not "no src attributes at all".
    remote = re.findall(r'(?:src|href)\s*=\s*["\'](https?:|file:|//|[A-Za-z]:\\)', page)
    check("no external resources", not remote, str(remote[:4]))

    match = re.search(r"window\.__PIX_ACTIVITY_BOOTSTRAP__ = (\{.*?\});\n", page, re.S)
    check("bundle parseable", match is not None)
    if match:
        bundle = json.loads(match.group(1))
        check("standalone flag set", bundle.get("standalone") is True)
        ids = {e["id"] for e in bundle["entries"]}
        check("every entry has an embedded payload",
              ids == set(bundle["payloads"]),
              f"{len(ids - set(bundle['payloads']))} missing")
        # Renders must travel with the snapshot, or an offline page shows broken images.
        wanted = {
            (e.get("render") or {}).get("name")
            for e in bundle["entries"] if e.get("render")
        }
        wanted.discard(None)
        embedded = set(bundle.get("renders") or {})
        check("every recorded render is inlined", wanted <= embedded,
              f"missing {sorted(wanted - embedded)}")
    check("offline branch guarded", "if (STANDALONE) {" in page)


def stage_optout(log_dir: Path) -> None:
    print("[7] recording can be switched off without affecting the call")
    from pix_tool_set.engine import activity

    before = len(activity.read_all())
    env = dict(os.environ, PIX_TOOL_SET_ACTIVITY_DIR=str(log_dir), PIX_TOOL_SET_NO_LOG="1")
    proc = cli(["frame-stats", "--session", SESSION], env)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {}
    check("the call still succeeds with logging off",
          payload.get("status") == "success", str(payload.get("status")))
    check("nothing was appended", len(activity.read_all()) == before,
          f"{len(activity.read_all())} vs {before}")

    # An unwritable log directory must not break a call either.
    env_bad = dict(os.environ, PIX_TOOL_SET_ACTIVITY_DIR="Z:\\nonexistent\\pix-activity")
    proc = cli(["activity-log", "--stats-only"], env_bad)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {}
    check("an unwritable log directory does not break the call",
          payload.get("status") in {"success", "partial"}, str(payload.get("status")))


def main() -> int:
    log_dir = Path(tempfile.mkdtemp(prefix="pixts-activity-"))
    env = dict(os.environ, PIX_TOOL_SET_ACTIVITY_DIR=str(log_dir))
    print(f"isolated log dir: {log_dir}\n")
    try:
        stage_record(env, log_dir)
        print()
        stage_cursor(env)
        print()
        stage_payload()
        print()
        stage_digest()
        print()
        stage_server(env)
        print()
        stage_export(env, log_dir)
        print()
        stage_optout(log_dir)
    finally:
        shutil.rmtree(log_dir, ignore_errors=True)

    print()
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    for entry in FAILED:
        print("  -", entry)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
