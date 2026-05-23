"""Sprint Health MCP — entry point.

Azure App Service's internal load balancer rewrites the Host header from the
public hostname to a private infrastructure hostname.  Even though the MCP SDK's
DNS-rebinding protection is disabled in server.py (the correct fix), we still
rewrite the Host header to 'localhost' here as belt-and-suspenders defence, and
we keep the /health probe so App Service can report the app as Running.

Endpoints:
    GET  /health  → 200 "ok"           (App Service health probe)
    POST /mcp     → MCP streamable HTTP transport
    GET  /mcp     → MCP SSE event stream

Copilot Studio MCP Server URL:
    https://<app-name>.azurewebsites.net/mcp
"""
import os
import sys
import uvicorn

from server import mcp  # FastMCP instance with all seven tools registered


class ReverseProxyHostFix:
    """ASGI middleware that:
    1. Answers GET /health with 200 "ok" for the App Service health probe.
    2. Rewrites the Host header to 'localhost' before the MCP app sees it,
       neutralising Azure's internal hostname rewriting.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # ── Health probe ──────────────────────────────────────────────────────
        if scope.get("path") == "/health":
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"text/plain; charset=utf-8"]],
            })
            await send({"type": "http.response.body", "body": b"ok"})
            return

        # ── Log original Host header ──────────────────────────────────────────
        original_host = next(
            (v.decode("utf-8", errors="replace")
             for k, v in scope.get("headers", [])
             if k.lower() == b"host"),
            "NOT_PRESENT",
        )
        print(
            f"[HostFix] {scope.get('method', '?')} {scope.get('path', '?')} "
            f"| original Host: {original_host}",
            flush=True,
        )

        # ── Replace Host with 'localhost:8000' ────────────────────────────────
        # Use 'localhost:8000' (with port) rather than plain 'localhost' so the
        # value matches the SDK's wildcard pattern "localhost:*" if protection
        # were ever re-enabled.
        fixed_headers = [
            (k, v) for k, v in scope.get("headers", [])
            if k.lower() != b"host"
        ]
        fixed_headers.append((b"host", b"localhost:8000"))
        scope = {**scope, "headers": fixed_headers}

        print("[HostFix] Host rewritten → localhost:8000", flush=True)

        await self.app(scope, receive, send)


app = ReverseProxyHostFix(mcp.streamable_http_app())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"[Startup] Starting Sprint Health MCP on port {port}", flush=True)
    print(f"[Startup] Python {sys.version}", flush=True)
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        lifespan="on",
        log_level="info",
    )
