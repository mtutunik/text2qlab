#!/usr/bin/env python3
"""
QLab MCP server: lets an MCP client (e.g. Claude Code) read and edit QLab 5
cues -- especially Text cues -- through QLab's OSC API over TCP.

Standard library only (works on Python 3.9; the official MCP SDK needs 3.10+).
Speaks MCP over stdio: newline-delimited JSON-RPC 2.0.

Configuration (environment variables, all optional):
    QLAB_HOST       default 127.0.0.1
    QLAB_PORT       default 53000
    QLAB_PASSCODE   OSC access passcode set in QLab (Workspace Settings > Network)
    QLAB_WORKSPACE  workspace name or uniqueID (default: first open workspace
                    that grants OSC edit permission)

Register with Claude Code:
    claude mcp add qlab -- python3 /path/to/qlab_mcp_server.py
"""

import json
import os
import re
import sys
import traceback

from qlab_osc import QLabClient, QLabError

SERVER_NAME = "qlab"
SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

UUID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")

# Group cue mode used for crossfade groups ("Timeline - start all children simultaneously")
GROUP_MODE_TIMELINE = 3

qlab = QLabClient(
    host=os.environ.get("QLAB_HOST", "127.0.0.1"),
    port=int(os.environ.get("QLAB_PORT", "53000")),
)
_connected = False


def log(*args):
    print("[qlab-mcp]", *args, file=sys.stderr, flush=True)


# -----------------------------
# Helpers
# -----------------------------
def ensure_connected():
    global _connected
    if not _connected:
        info = qlab.connect(os.environ.get("QLAB_WORKSPACE") or None,
                            os.environ.get("QLAB_PASSCODE") or None)
        _connected = True
        log("connected:", info)
    return qlab


def resolve_cue(ref):
    """Accept a cue uniqueID or a cue number; return the uniqueID."""
    ref = str(ref).strip()
    if UUID_RE.match(ref):
        return ref
    q = ensure_connected()
    return q.ws(f"cue/{ref}/uniqueID")


def resolve_cue_list(ref):
    """Accept a cue list name or uniqueID; return the uniqueID (or None)."""
    if not ref:
        return None
    q = ensure_connected()
    for cl in q.ws("cueLists"):
        if ref in (cl.get("uniqueID"), cl.get("listName"), cl.get("name")):
            return cl["uniqueID"]
    raise QLabError(f"Cue list '{ref}' not found")


def place_in(cue_id, parent_id):
    """Move cue_id to the end of parent_id unless it is already in it."""
    if not parent_id:
        return
    q = ensure_connected()
    if q.cue(cue_id, "parent") != parent_id:
        q.ws(f"move/{cue_id}", len(q.cue(parent_id, "children") or []), parent_id)


def prepare_insertion(args):
    """
    New cues go after the selected cue in the current cue list. Select
    `after_cue`, or the last cue of `cue_list`, so they land in the right place.
    Returns the target cue list id (new cues get moved there if needed).
    """
    q = ensure_connected()
    target = resolve_cue_list(args.get("cue_list"))
    if args.get("after_cue"):
        q.ws(f"select_id/{resolve_cue(args['after_cue'])}")
        return None
    if target:
        children = q.cue(target, "children") or []
        if children:
            q.ws(f"select_id/{children[-1]['uniqueID']}")
    return target


def osc_args(value):
    """Turn a JSON value into a list of OSC arguments (lists expand to several args)."""
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def set_props(cue_id, props):
    """Set each property; collect failures instead of aborting half-way."""
    q = ensure_connected()
    errors = {}
    for key, value in (props or {}).items():
        try:
            q.cue(cue_id, key, *osc_args(value))
        except QLabError as e:
            errors[key] = str(e)
    return errors


def text_format_props(args, cue_id=None):
    """
    Map font arguments to QLab OSC text-format properties. QLab rejects
    fontFamily/fontStyle as setters; fonts are set with fontFamilyAndStyle
    (or fontName, the PostScript name such as "Tahoma-Bold").
    """
    props = {}
    if args.get("font_name"):
        props["text/format/fontName"] = args["font_name"]
    elif args.get("font_family") or args.get("font_style"):
        family, style = args.get("font_family"), args.get("font_style")
        if not family and cue_id:
            family = ensure_connected().cue(cue_id, "text/format/fontFamily", check=False)
        if family:
            props["text/format/fontFamilyAndStyle"] = [family, style or "Regular"]
    if args.get("font_size") is not None:
        props["text/format/fontSize"] = float(args["font_size"])
    if args.get("color"):
        props["text/format/color"] = [float(c) for c in args["color"]]
    if args.get("alignment"):
        props["text/format/alignment"] = args["alignment"]
    return props


def flatten_cues(cues, depth=0, out=None):
    out = [] if out is None else out
    for c in cues or []:
        out.append({
            "depth": depth,
            "uniqueID": c.get("uniqueID"),
            "number": c.get("number"),
            "type": c.get("type"),
            "name": c.get("listName") or c.get("name"),
        })
        flatten_cues(c.get("cues"), depth + 1, out)
    return out


# -----------------------------
# Tool implementations
# -----------------------------
def tool_list_workspaces(args):
    return qlab.workspaces()


def tool_connect_workspace(args):
    global _connected
    info = qlab.connect(args.get("workspace") or None,
                        args.get("passcode") or os.environ.get("QLAB_PASSCODE") or None)
    _connected = True
    if info.get("permissions") in ("ok", "ok:", None, ""):
        info["warning"] = ("Connected but QLab granted no permissions. In QLab: Workspace "
                           "Settings > Network > OSC Access, enable View/Edit/Control for "
                           "'No passcode' or add a passcode and pass it here.")
    return info


def tool_list_cues(args):
    q = ensure_connected()
    lists = q.ws("cueLists")
    want = args.get("cue_list")
    if want:
        lists = [l for l in lists if want in (l.get("uniqueID"), l.get("listName"),
                                              l.get("name"), l.get("number"))]
        if not lists:
            raise QLabError(f"Cue list '{want}' not found")
    rows = flatten_cues(lists)
    if args.get("include_text"):
        for r in rows:
            if r["type"] == "Text":
                r["text"] = q.cue(r["uniqueID"], "text", check=False)
                r["notes"] = q.cue(r["uniqueID"], "notes", check=False)
    return rows


def tool_get_cue(args):
    q = ensure_connected()
    cue_id = resolve_cue(args["cue"])
    keys = args.get("properties") or [
        "uniqueID", "number", "name", "listName", "type", "notes", "parent",
        "preWait", "duration", "postWait", "continueMode", "armed", "colorName",
    ]
    values = q.cue(cue_id, "valuesForKeys", json.dumps(keys))
    if values.get("type") == "Text" and not args.get("properties"):
        values["text"] = q.cue(cue_id, "text", check=False)
        values["text_format"] = q.cue(cue_id, "text/format", check=False)
        values["opacity"] = q.cue(cue_id, "opacity", check=False)
    return values


def _create_one_text_cue(q, spec, defaults, target_list=None):
    cue_id = q.ws("new", "text")
    place_in(cue_id, target_list)
    props = {"text": spec["text"]}
    for key in ("name", "number", "notes"):
        if spec.get(key) is not None:
            props[key] = str(spec[key])
    merged = dict(defaults)
    merged.update({k: v for k, v in spec.items() if v is not None})
    props.update(text_format_props(merged))
    if merged.get("opacity") is not None:
        props["opacity"] = float(merged["opacity"])
    errors = set_props(cue_id, props)
    result = {"uniqueID": cue_id, "number": q.cue(cue_id, "number", check=False)}
    if errors:
        result["errors"] = errors
    return result


def tool_create_text_cue(args):
    q = ensure_connected()
    target = prepare_insertion(args)
    return _create_one_text_cue(q, args, {}, target)


def tool_create_text_cues(args):
    """Batch create; each new cue is selected, so the next one lands right after it."""
    q = ensure_connected()
    target = prepare_insertion(args)
    defaults = {k: args.get(k) for k in
                ("font_name", "font_family", "font_style", "font_size", "color", "alignment", "opacity")
                if args.get(k) is not None}
    created = []
    for spec in args["cues"]:
        created.append(_create_one_text_cue(q, spec, defaults, target))
        target = None  # later cues follow the (now selected) previous one
    result = {"created": created}
    if args.get("crossfade"):
        ids = [c["uniqueID"] for c in created]
        result["groups"] = _crossfade_sequence(q, ids, args, args.get("previous_cue"))
    return result


def _make_crossfade_group(q, text_id, prev_id, up, down, hold, name=None):
    """
    Same structure as fadegroup.applescript:
      Group (timeline) ── text cue (opacity 0)
                       ├─ fade out of previous text cue (if any)
                       └─ fade in of this text cue (pre-wait = hold)
    """
    q.cue(text_id, "opacity", 0.0)

    q.ws(f"select_id/{text_id}")
    fade_out = None
    if prev_id:
        fade_out = q.ws("new", "fade")
        set_props(fade_out, {"cueTargetId": prev_id, "duration": float(down),
                             "doOpacity": 1, "opacity": 0.0, "stopTargetWhenDone": 1})
    fade_in = q.ws("new", "fade")
    set_props(fade_in, {"cueTargetId": text_id, "preWait": float(hold),
                        "duration": float(up), "doOpacity": 1, "opacity": 1.0})

    q.ws(f"select_id/{text_id}")
    group = q.ws("new", "group")
    if not name:
        name = "Crossfade to " + (q.cue(text_id, "listName", check=False) or "")
    errors = set_props(group, {"mode": GROUP_MODE_TIMELINE, "name": name,
                               "notes": q.cue(text_id, "notes", check=False) or ""})

    index = 0
    for child in [text_id, fade_out, fade_in]:
        if child:
            q.ws(f"move/{child}", index, group)
            index += 1
    result = {"group": group, "text_cue": text_id, "fade_in": fade_in, "fade_out": fade_out}
    if errors:
        result["errors"] = errors
    return result


def _crossfade_sequence(q, text_ids, args, previous_ref=None):
    up = args.get("up_time", 0)
    down = args.get("down_time", 0)
    hold = args.get("hold_time", 0.1)
    names = args.get("group_names") or []
    prev = resolve_cue(previous_ref) if previous_ref else None
    groups = []
    for i, tid in enumerate(text_ids):
        name = names[i] if i < len(names) else None
        groups.append(_make_crossfade_group(q, tid, prev, up, down, hold, name))
        prev = tid
    return groups


def tool_create_crossfade_groups(args):
    q = ensure_connected()
    ids = [resolve_cue(c) for c in args["text_cues"]]
    return _crossfade_sequence(q, ids, args, args.get("previous_cue"))


def tool_update_cue(args):
    ensure_connected()
    cue_id = resolve_cue(args["cue"])
    # Set text first so the format changes apply to the new text
    props = {"text": args["text"]} if args.get("text") is not None else {}
    props.update(args.get("properties") or {})
    props.update(text_format_props(args, cue_id))
    errors = set_props(cue_id, props)
    return {"uniqueID": cue_id, "updated": [k for k in props if k not in errors],
            "errors": errors}


def tool_create_cue(args):
    q = ensure_connected()
    target = prepare_insertion(args)
    cue_id = q.ws("new", args["type"])
    place_in(cue_id, target)
    errors = set_props(cue_id, args.get("properties"))
    return {"uniqueID": cue_id, "errors": errors}


def tool_move_cue(args):
    q = ensure_connected()
    cue_id = resolve_cue(args["cue"])
    parent = resolve_cue(args["parent"]) if args.get("parent") else q.cue(cue_id, "parent")
    q.ws(f"move/{cue_id}", int(args["index"]), parent)
    return {"moved": cue_id, "parent": parent, "index": int(args["index"])}


def tool_delete_cue(args):
    q = ensure_connected()
    deleted = []
    for ref in args["cues"]:
        cue_id = resolve_cue(ref)
        if q.cue(cue_id, "type") == "Cue List":
            # QLab asks for confirmation in a modal dialog, which blocks all OSC
            raise QLabError("Refusing to delete a whole cue list over OSC; delete it in QLab")
        q.ws(f"delete_id/{cue_id}")
        deleted.append(cue_id)
    return {"deleted": deleted}


def tool_send_osc(args):
    q = ensure_connected()
    address = args["address"]
    osc = list(args.get("args") or [])
    if address.startswith("/workspace"):
        return q.send(address, *osc, check=False)
    return q.ws(address.lstrip("/"), *osc, check=False)


# -----------------------------
# Tool definitions
# -----------------------------
CUE_REF = {"type": "string", "description": "Cue number (e.g. '12') or cue uniqueID"}
CUE_LIST_REF = {"type": "string",
                "description": "Cue list name or uniqueID to append to (ignored if after_cue is set)"}
FORMAT_PROPS = {
    "font_family": {"type": "string", "description": "Font family as in Font Book, e.g. 'Tahoma'"},
    "font_name": {"type": "string",
                  "description": "PostScript font name, e.g. 'Tahoma-Bold' (overrides family/style)"},
    "font_style": {"type": "string", "description": "e.g. 'Regular', 'Bold', 'Italic'"},
    "font_size": {"type": "number", "description": "Points"},
    "color": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4,
              "description": "RGBA, each 0..1"},
    "alignment": {"type": "string", "enum": ["left", "center", "right", "justify"]},
}
CROSSFADE_PROPS = {
    "up_time": {"type": "number", "description": "Fade-in duration, seconds (default 0)"},
    "down_time": {"type": "number", "description": "Fade-out duration, seconds (default 0)"},
    "hold_time": {"type": "number", "description": "Pre-wait before fade-in, seconds (default 0.1)"},
    "group_names": {"type": "array", "items": {"type": "string"},
                    "description": "Optional name per group (default 'Crossfade to <text>')"},
    "previous_cue": {"type": "string",
                     "description": "Text cue already on screen before the first one, to fade out"},
}
TEXT_CUE_SPEC = {
    "type": "object",
    "properties": dict({
        "text": {"type": "string", "description": "Cue text; use \\n for line breaks"},
        "name": {"type": "string"},
        "number": {"type": "string"},
        "notes": {"type": "string", "description": "Cue notes (e.g. the speaker)"},
        "opacity": {"type": "number"},
    }, **FORMAT_PROPS),
    "required": ["text"],
}

TOOLS = [
    ("list_workspaces", tool_list_workspaces,
     "List open QLab workspaces.", {"type": "object", "properties": {}}),
    ("connect_workspace", tool_connect_workspace,
     "Connect to a QLab workspace (by name or uniqueID; default: first one granting OSC "
     "edit permission) with an "
     "optional OSC passcode. Other tools connect automatically using env settings.",
     {"type": "object", "properties": {
         "workspace": {"type": "string"}, "passcode": {"type": "string"}}}),
    ("list_cues", tool_list_cues,
     "List cues (flattened tree with depth). Optionally restrict to one cue list and "
     "include the text and notes of Text cues.",
     {"type": "object", "properties": {
         "cue_list": {"type": "string", "description": "Cue list name or uniqueID"},
         "include_text": {"type": "boolean"}}}),
    ("get_cue", tool_get_cue,
     "Get properties of a cue. For Text cues also returns text, format and opacity.",
     {"type": "object", "properties": {
         "cue": CUE_REF,
         "properties": {"type": "array", "items": {"type": "string"},
                        "description": "Specific QLab OSC property keys to fetch"}},
      "required": ["cue"]}),
    ("create_text_cue", tool_create_text_cue,
     "Create one Text cue after `after_cue`, at the end of `cue_list`, or (default) after "
     "the selected cue in the current cue list.",
     {"type": "object",
      "properties": dict(TEXT_CUE_SPEC["properties"], after_cue=CUE_REF, cue_list=CUE_LIST_REF),
      "required": ["text"]}),
    ("create_text_cues", tool_create_text_cues,
     "Create many Text cues in order (e.g. subtitles), with shared font settings, and "
     "optionally wrap each in a crossfade group like fadegroup.applescript.",
     {"type": "object", "properties": dict({
         "cues": {"type": "array", "items": TEXT_CUE_SPEC},
         "after_cue": CUE_REF,
         "cue_list": CUE_LIST_REF,
         "opacity": {"type": "number"},
         "crossfade": {"type": "boolean", "description": "Build crossfade groups"},
     }, **FORMAT_PROPS, **CROSSFADE_PROPS), "required": ["cues"]}),
    ("create_crossfade_groups", tool_create_crossfade_groups,
     "Wrap existing Text cues (in order) into timeline groups that fade the previous text "
     "out and this one in.",
     {"type": "object", "properties": dict({
         "text_cues": {"type": "array", "items": {"type": "string"}}}, **CROSSFADE_PROPS),
      "required": ["text_cues"]}),
    ("update_cue", tool_update_cue,
     "Edit a cue: text, font settings, and/or any QLab OSC property "
     "(e.g. {\"name\": \"x\", \"notes\": \"y\", \"preWait\": 1.5}). "
     "Array values are sent as multiple OSC arguments.",
     {"type": "object", "properties": dict({
         "cue": CUE_REF, "text": {"type": "string"},
         "properties": {"type": "object"}}, **FORMAT_PROPS),
      "required": ["cue"]}),
    ("create_cue", tool_create_cue,
     "Create a cue of any type (e.g. 'group', 'fade', 'memo', 'text', 'video', 'audio') "
     "and set properties.",
     {"type": "object", "properties": {
         "type": {"type": "string"}, "after_cue": CUE_REF, "cue_list": CUE_LIST_REF,
         "properties": {"type": "object"}}, "required": ["type"]}),
    ("move_cue", tool_move_cue,
     "Move a cue to a 0-based index inside a parent group/cue list (default: current parent).",
     {"type": "object", "properties": {
         "cue": CUE_REF, "index": {"type": "integer"}, "parent": CUE_REF},
      "required": ["cue", "index"]}),
    ("delete_cue", tool_delete_cue,
     "Delete cues. Destructive -- confirm with the user first.",
     {"type": "object", "properties": {
         "cues": {"type": "array", "items": {"type": "string"}}}, "required": ["cues"]}),
    ("send_osc", tool_send_osc,
     "Send any QLab OSC command and return the reply data. Addresses not starting with "
     "/workspace are sent to the connected workspace (e.g. '/cue/5/name', '/go').",
     {"type": "object", "properties": {
         "address": {"type": "string"},
         "args": {"type": "array", "items": {"type": ["string", "number", "boolean"]}}},
      "required": ["address"]}),
]
TOOL_FUNCS = {name: func for name, func, _, _ in TOOLS}


# -----------------------------
# MCP / JSON-RPC over stdio
# -----------------------------
def send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(req):
    method = req.get("method")
    params = req.get("params") or {}

    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion", DEFAULT_PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{"name": n, "description": d, "inputSchema": s}
                          for n, _, d, s in TOOLS]}
    if method == "tools/call":
        name = params.get("name")
        func = TOOL_FUNCS.get(name)
        if func is None:
            raise KeyError(f"Unknown tool: {name}")
        try:
            result = func(params.get("arguments") or {})
            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=1)
            return {"content": [{"type": "text", "text": text}], "isError": False}
        except Exception as e:
            log(traceback.format_exc())
            msg = f"{type(e).__name__}: {e}"
            if isinstance(e, OSError) and "timed out" in str(e):
                msg += (" -- QLab did not answer. It may be showing a dialog (check the "
                        "QLab window) or be busy.")
            if "denied" in str(e):
                msg += (" -- QLab denied the request. Enable OSC permissions in Workspace "
                        "Settings > Network (or set QLAB_PASSCODE).")
            return {"content": [{"type": "text", "text": msg}], "isError": True}
    raise LookupError(method)


def main():
    log("starting")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        if "id" not in req:
            continue  # notification (e.g. notifications/initialized)
        try:
            send({"jsonrpc": "2.0", "id": req["id"], "result": handle(req)})
        except LookupError as e:
            send({"jsonrpc": "2.0", "id": req["id"],
                  "error": {"code": -32601, "message": f"Method not found: {e}"}})
        except Exception as e:
            log(traceback.format_exc())
            send({"jsonrpc": "2.0", "id": req["id"],
                  "error": {"code": -32603, "message": str(e)}})
    qlab.close()


if __name__ == "__main__":
    main()
