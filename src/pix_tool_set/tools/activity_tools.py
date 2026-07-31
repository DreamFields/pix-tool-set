"""Local viewer for the activity log: a zero-dependency HTTP server plus one HTML page.

Why a server rather than just writing a static HTML file: a page opened over
``file://`` cannot read a growing log, so "live" would be impossible and every refresh
would need a regenerated file. A tiny loopback server keeps the page honest - it polls
an endpoint and receives only what was appended since its last cursor.

Design decisions worth stating:

  * Loopback only. This exposes local filesystem contents, so binding anywhere else
    would be a mistake. The bind address is not configurable for that reason.
  * Byte-offset cursor, not entry counts. Concurrent CLI processes append while the
    page polls; an offset is the only cursor that stays correct without re-reading.
  * Payloads are fetched on demand. Result envelopes reach hundreds of KB, so the feed
    carries digests and the page asks for detail only when a row is opened.
  * Poll rather than websockets/SSE. One dependency-free ``GET`` with a cursor is
    sufficient at human interaction rates and far easier to reason about.

The static HTML lives in ``viewer/activity.html`` next to this module, so the page can
also be opened directly against an exported snapshot.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..context import ToolContext
from ..engine import activity
from ..errors import PixToolError, invalid_argument, not_found
from ..results import ToolResult
from ._common import tool, with_session

_NOTE = (
    "Every CLI invocation and every call_tool() call is appended to an activity log under "
    "the user profile; this tool serves that log to a local page that follows it live and "
    "can replay it step by step. The server binds to 127.0.0.1 only, because it exposes "
    "local result payloads. Set PIX_TOOL_SET_NO_LOG=1 to stop recording, or use "
    "activity-log --clear to discard what was recorded."
)

VIEWER_DIR = Path(__file__).resolve().parent.parent / "viewer"
VIEWER_HTML = VIEWER_DIR / "activity.html"


def _page_source() -> str:
    if VIEWER_HTML.exists():
        return VIEWER_HTML.read_text(encoding="utf-8")
    raise not_found(
        "viewer page",
        str(VIEWER_HTML),
        "The packaged activity.html is missing from the installation.",
    )


class _Handler(BaseHTTPRequestHandler):
    server_version = "pix-tool-set-activity"

    # Keep the terminal readable; the page polls several times a second.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)

        if route in ("/", "/index.html", "/activity.html"):
            try:
                body = _page_source().encode("utf-8")
            except PixToolError as exc:
                self._json({"error": exc.message}, 500)
                return
            self._send(200, body, "text/html; charset=utf-8")
            return

        if route == "/api/feed":
            try:
                offset = int((query.get("offset") or ["0"])[0])
            except ValueError:
                offset = 0
            entries, new_offset, size = activity.read_since(max(offset, 0))
            self._json(
                {
                    "entries": entries,
                    "offset": new_offset,
                    "log_bytes": size,
                    "recording_enabled": not activity.disabled(),
                    "log_path": str(activity.index_path()),
                }
            )
            return

        if route == "/api/payload":
            record_id = (query.get("id") or [""])[0]
            payload = activity.read_payload(record_id)
            if payload is None:
                self._json({"error": f"no payload for id {record_id!r}"}, 404)
                return
            self._json({"id": record_id, "envelope": payload})
            return

        if route == "/api/stats":
            self._json(activity.stats())
            return

        self._json({"error": f"unknown route {route!r}"}, 404)


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


# ======================================================================
@tool(
    name="activity-viewer",
    summary=(
        "Serve a local page that follows CLI activity live and can replay the call "
        "history step by step."
    ),
    category="meta",
    requires_session=False,
    parameters=with_session(
        port={"type": "integer", "description": "Loopback port. Default 8787."},
        no_browser={
            "type": "boolean",
            "description": "Do not open the page in the default browser.",
        },
        background={
            "type": "boolean",
            "description": (
                "Return immediately instead of serving until interrupted. The server "
                "then lives only as long as this process, so it is mainly useful for "
                "scripted checks."
            ),
        },
        export={
            "type": "string",
            "description": (
                "Write a standalone HTML file with the history embedded, instead of "
                "serving. Openable offline; live following is unavailable in that mode."
            ),
        },
    ),
    returns="The URL being served, or the path of the exported standalone page.",
    examples=[
        "pix-tool-set activity-viewer",
        "pix-tool-set activity-viewer --port 9000 --no-browser",
        "pix-tool-set activity-viewer --export G:\\out\\pix-activity.html",
    ],
    notes=_NOTE,
)
def activity_viewer(args: dict[str, Any], context: ToolContext) -> ToolResult:
    if args.get("export"):
        return _export_standalone(args, context)

    port = int(args.get("port") or 8787)
    if port < 1 or port > 65535:
        raise invalid_argument("port", f"{port} is not a usable TCP port")
    if not _port_is_free(port):
        raise PixToolError(
            code="port_in_use",
            message=f"Port {port} on 127.0.0.1 is already in use.",
            stage="viewer",
            suggestion="Pass a different --port, or stop whatever is listening there.",
        )

    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    url = f"http://127.0.0.1:{port}/"
    snapshot = activity.stats()

    data = {
        "url": url,
        "port": port,
        "log_path": snapshot["log_path"],
        "calls_recorded": snapshot["total_calls"],
        "recording_enabled": snapshot["recording_enabled"],
        "endpoints": {
            "page": url,
            "feed": f"{url}api/feed?offset=<bytes>",
            "payload": f"{url}api/payload?id=<record id>",
            "stats": f"{url}api/stats",
        },
        "bound_to": "127.0.0.1 only, because the log exposes local result payloads",
    }

    if not args.get("no_browser"):
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    if args.get("background"):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        data["mode"] = "background thread; stops when this process exits"
        result = ToolResult.success(data)
        result.add_diagnostic(
            "warning",
            "Serving in the background of a short-lived CLI process, so the server dies "
            "with it. Run without --background to keep the page alive.",
        )
        return result

    data["mode"] = "serving until interrupted (Ctrl+C)"
    print(json.dumps({"status": "success", "tool": "activity-viewer", "data": data},
                     ensure_ascii=False, indent=2), flush=True)
    print(f"\nserving {url} - press Ctrl+C to stop", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        server.shutdown()
        server.server_close()

    # Already printed above; return a quiet envelope so the CLI does not double-print
    # the same payload.
    return ToolResult.success({"url": url, "stopped": True, "already_reported": True})


def _export_standalone(args: dict[str, Any], context: ToolContext) -> ToolResult:
    """A single HTML file with the history baked in, for sharing or archiving."""
    entries = activity.read_all()
    payloads: dict[str, Any] = {}
    budget = 8 * 1024 * 1024
    used = 0
    skipped = 0
    # Newest first: recent calls are the ones worth inspecting in detail, and the
    # budget stops a huge log from producing an unopenable page.
    for row in reversed(entries):
        record_id = row.get("id")
        if not record_id:
            continue
        envelope = activity.read_payload(record_id)
        if envelope is None:
            continue
        blob = json.dumps(envelope, ensure_ascii=False, default=str)
        if used + len(blob) > budget:
            skipped += 1
            continue
        payloads[record_id] = envelope
        used += len(blob)

    bundle = {
        "generated_at": time.time(),
        "entries": entries,
        "payloads": payloads,
        "stats": activity.stats(entries),
        "standalone": True,
    }
    injected = json.dumps(bundle, ensure_ascii=False, default=str)

    page = _page_source().replace(
        "/*__PIX_ACTIVITY_BOOTSTRAP__*/",
        f"window.__PIX_ACTIVITY_BOOTSTRAP__ = {injected};",
    )
    target = context.resolve_output(str(args["export"]), "pix-activity.html")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding="utf-8")

    data = {
        "path": str(target),
        "bytes": target.stat().st_size,
        "calls_embedded": len(entries),
        "payloads_embedded": len(payloads),
        "payloads_skipped": skipped,
        "mode": "standalone; opens over file:// but cannot follow new calls",
    }
    result = ToolResult.success(data, output_paths=[str(target)])
    if skipped:
        result.degrade(
            f"{skipped} payload(s) were left out to keep the file openable.",
            reason="the embedded payload budget is 8 MB",
            alternative="Serve the log instead of exporting, which fetches payloads on demand.",
        )
    return result


# ======================================================================
@tool(
    name="activity-log",
    summary=(
        "Inspect the recorded call history from the command line: recent entries, "
        "per-tool counts, one full result envelope, or clear it."
    ),
    category="meta",
    requires_session=False,
    parameters=with_session(
        limit={"type": "integer", "description": "Most recent entries to show. Default 20."},
        tool_name={"type": "string", "description": "Only entries for this tool."},
        status={
            "type": "string",
            "enum": ["success", "partial", "error"],
            "description": "Only entries with this status.",
        },
        record_id={
            "type": "string",
            "description": "Return the full stored envelope for one invocation.",
        },
        stats_only={"type": "boolean", "description": "Return only the aggregate counts."},
        clear={"type": "boolean", "description": "Delete the log and every stored payload."},
    ),
    returns="Recent invocations with timings and statuses, plus aggregate counts.",
    examples=[
        "pix-tool-set activity-log --limit 10",
        "pix-tool-set activity-log --status error",
        "pix-tool-set activity-log --record-id 20260731-164829-1d5a7497f1",
        "pix-tool-set activity-log --clear",
    ],
    notes=_NOTE,
)
def activity_log(args: dict[str, Any], context: ToolContext) -> ToolResult:
    if args.get("clear"):
        outcome = activity.clear()
        return ToolResult.success(
            {
                **outcome,
                "log_path": str(activity.index_path()),
                "note": "Recording continues for later calls unless PIX_TOOL_SET_NO_LOG is set.",
            }
        )

    if args.get("record_id"):
        envelope = activity.read_payload(str(args["record_id"]))
        if envelope is None:
            raise not_found(
                "activity record",
                str(args["record_id"]),
                "Run activity-log to list ids that exist; old payloads are pruned.",
            )
        return ToolResult.success({"id": args["record_id"], "envelope": envelope})

    entries = activity.read_all()
    summary = activity.stats(entries)
    if args.get("stats_only"):
        return ToolResult.success(summary)

    rows = entries
    if args.get("tool_name"):
        wanted = str(args["tool_name"])
        rows = [row for row in rows if row.get("tool") == wanted]
    if args.get("status"):
        rows = [row for row in rows if row.get("status") == args["status"]]

    limit = args.get("limit")
    limit = 20 if limit is None else max(int(limit), 0)
    shown = rows[-limit:] if limit else rows

    data = {
        "stats": summary,
        "matched": len(rows),
        "returned": len(shown),
        "entries": shown,
        "viewer": "pix-tool-set activity-viewer",
    }
    if not entries:
        result = ToolResult.partial(data)
        result.degrade(
            "No calls have been recorded yet.",
            reason=(
                "recording is disabled by PIX_TOOL_SET_NO_LOG"
                if activity.disabled()
                else "the log is empty; run any tool and it will appear here"
            ),
        )
        return result
    return ToolResult.success(data)
