"""Workflow Canvas REST client (server-side calls carry no Origin, which its guard allows)."""
import json
import urllib.error
import urllib.request

from . import config


class CanvasError(RuntimeError):
    pass


def call_tool(name, args=None, timeout=10):
    req = urllib.request.Request(f"{config.WORKFLOW_CANVAS_URL}/api/tools/{name}", data=json.dumps(args or {}).encode(),
                                 headers={"Content-Type": "application/json", "x-client-name": "session-tracker"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.load(r)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise CanvasError(f"Workflow Canvas unavailable at {config.WORKFLOW_CANVAS_URL}: {e}") from e
    if not body.get("ok"):
        raise CanvasError(str(body.get("error") or body))
    return (body.get("result") or {}).get("json") or {}


def list_documents():
    return call_tool("list_documents").get("documents", [])


def create_document(title, template=None):
    args = {"title": title, "open": False}
    if template:
        args["template"] = template
    return call_tool("create_document", args)


def doc_url(doc_id):
    return f"{config.WORKFLOW_CANVAS_URL}/?doc={doc_id}"

