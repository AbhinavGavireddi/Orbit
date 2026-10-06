from urllib.parse import urlsplit
import json

from orbit_common.mcp_door import check_mcp
from orbit_common.page import check_page
from orbit_common.room_devices import ACTUATORS, POWERS, SENSORS, room_payload

AX_ROLES = {
    "AXButton", "AXTextField", "AXTextArea", "AXMenuItem", "AXCheckBox", "AXLink",
    "AXPopUpButton", "AXRadioButton",
}


def _short(value, limit=180) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + "..."


def _point(action: dict) -> str:
    x, y = action.get("x"), action.get("y")
    if x is None or y is None:
        return ""
    return f" at ({x}, {y})"


def card_payload(action: dict):
    """The exact fields the face shows. The store does not parse a page or a tool."""
    kind = action.get("type")
    params = action.get("params", {})
    if kind == "room_call":
        return room_payload(params)
    if kind == "browser_act":
        page = check_page(action)
        return {"url": page["url"], "verb": page["verb"], "target": page["target"]}
    if kind == "mcp_call":
        call = check_mcp(action)
        return {"server": call["server"], "tool": call["tool"]}
    return None


def approval_summary(action: dict) -> str:
    """Build deterministic approval copy from the exact payload, not worker prose."""
    kind, params = action.get("type"), action.get("params", {})
    lines = ["Review summary:"]
    if kind == "ax_perform":
        role = params.get("role")
        title = _short(params.get("title") or params.get("description", ""), 120)
        action_name = params.get("action", "AXPress")
        lines.append(f"- Accessibility action: {action_name} on {role} named \"{title}\".")
        if action_name == "AXSetValue":
            lines.append(f"- New value: \"{_short(params.get('value', ''), 180)}\".")
    elif kind == "computer":
        actions = params.get("actions") or []
        plural = "s" if len(actions) != 1 else ""
        lines.append(f"- Computer-control batch with {len(actions)} step{plural}.")
        for index, step in enumerate(actions, start=1):
            step_kind = step.get("type")
            if step_kind in {"click", "double_click", "move"}:
                button = f" using {step.get('button')} button" if step.get("button") else ""
                lines.append(f"- Step {index}: {step_kind.replace('_', ' ')}{_point(step)}{button}.")
            elif step_kind == "drag":
                path = step.get("path")
                lines.append(f"- Step {index}: drag over {len(path) if isinstance(path, list) else 'unknown'} point(s).")
            elif step_kind == "scroll":
                dx, dy = step.get("scroll_x", step.get("dx", 0)), step.get("scroll_y", step.get("dy", 0))
                lines.append(f"- Step {index}: scroll by ({dx}, {dy}){_point(step)}.")
            elif step_kind == "keypress":
                keys = step.get("keys", step.get("key", ""))
                lines.append(f"- Step {index}: press key(s) \"{_short(keys, 120)}\".")
            elif step_kind == "type":
                lines.append(f"- Step {index}: type \"{_short(step.get('text', ''), 180)}\".")
            elif step_kind == "wait":
                lines.append(f"- Step {index}: wait.")
            elif step_kind == "screenshot":
                lines.append(f"- Step {index}: inspect the screen.")
            else:
                lines.append(f"- Step {index}: {step_kind or 'unknown action'}.")
        checks = params.get("safety_checks") or []
        if checks:
            lines.append(f"- Provider safety checks present: {_short(json.dumps(checks, ensure_ascii=False), 240)}.")
    elif kind == "room_call":
        lines.append(f"- Payload: {room_payload(params)}.")
        lines.append("- Confirm this payload on the screen.")
        return "\n".join(lines)
    elif kind == "browser_act":
        page = check_page(action)
        lines.append(f"- Page: {page['url']}.")
        lines.append(f"- Action: {page['verb']} on \"{page['target']}\".")
        if page["verb"] == "type":
            lines.append(f"- Text: \"{_short(page['text'], 180)}\".")
        lines.append("- Confirm this payload on the screen.")
        return "\n".join(lines)
    elif kind == "mcp_call":
        call = check_mcp(action)
        lines.append(f"- Server: {call['server']}. Tool: {call['tool']}.")
        lines.append("- Confirm this payload on the screen.")
        return "\n".join(lines)
    else:
        lines.append(f"- Native action: {kind or 'unknown'}.")
    lines.append("- Approve only if this matches what you expect on the visible Mac.")
    return "\n".join(lines)


def validate_ax_ref(ref: dict, role: str, title: str, description: str):
    if not isinstance(ref, dict) or ref.get("version") != 1:
        raise ValueError("Invalid Accessibility reference")
    ref_role = ref.get("role")
    ref_title = ref.get("title", "")
    ref_description = ref.get("description", "")
    if ref_role != role or ref_role not in AX_ROLES:
        raise ValueError("Accessibility reference role mismatch")
    if not isinstance(ref_title, str) or not isinstance(ref_description, str):
        raise ValueError("Accessibility reference labels must be strings")
    if len(ref_title) > 500 or len(ref_description) > 500 or not (ref_title or ref_description):
        raise ValueError("Accessibility reference needs a bounded label")
    if title and title not in {ref_title, ref_description}:
        raise ValueError("Accessibility reference title mismatch")
    if description and description != ref_description:
        raise ValueError("Accessibility reference description mismatch")
    app = ref.get("app_bundle_id", "")
    if app is not None and (not isinstance(app, str) or len(app) > 200):
        raise ValueError("Invalid Accessibility app binding")
    path = ref.get("child_path", [])
    if not isinstance(path, list) or len(path) > 7:
        raise ValueError("Invalid Accessibility path")
    for index in path:
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 40:
            raise ValueError("Invalid Accessibility path")


def validate_action(action: dict) -> bool:
    """Return whether approval is necessary; unknown effects fail closed."""
    kind, params = action.get("type"), action.get("params", {})
    if kind == "screenshot":
        return False
    if kind == "ax_snapshot":
        return False
    if kind == "ax_perform":
        role = params.get("role")
        title = params.get("title", "")
        description = params.get("description", "")
        action_name = params.get("action", "AXPress")
        if (not isinstance(role, str) or role not in AX_ROLES or not isinstance(title, str)
                or not isinstance(description, str) or not (title or description)):
            raise ValueError("Accessibility perform needs a supported role and label")
        if len(title) > 500 or len(description) > 500:
            raise ValueError("Accessibility label is too long")
        if "ax_ref" in params:
            validate_ax_ref(params["ax_ref"], role, title, description)
        if action_name not in {"AXPress", "AXSetValue"}:
            raise ValueError("Unknown accessibility action")
        if action_name == "AXSetValue" and not isinstance(params.get("value"), str):
            raise ValueError("Accessibility set-value needs text")
        return True  # Only authority-owned policy can waive consent; worker labels are untrusted.
    if kind == "open_app":
        if params.get("bundle_id") not in {"com.apple.Notes", "com.google.Chrome"}:
            raise ValueError("App is outside typed navigation allowlist; use approved computer actions")
        return False
    if kind == "open_url":
        url = urlsplit(params.get("url", ""))
        if url.scheme != "https" or url.hostname != "open.spotify.com" or url.username or url.password:
            raise ValueError("Typed open_url supports Spotify HTTPS only")
        return False
    if kind == "notes_create":
        if not isinstance(params.get("title"), str) or len(params["title"]) > 200:
            raise ValueError("Invalid note title")
        return False
    if kind in {"dictation_start", "dictation_insert"}:
        if not isinstance(params.get("note_id"), str) or not params["note_id"]:
            raise ValueError("A bound note_id is required")
        if kind == "dictation_insert" and (not isinstance(params.get("text"), str)
                                           or len(params["text"]) > 8000 or not params.get("chunk_id")):
            raise ValueError("A bounded finalized dictation chunk is required")
        return False
    if kind == "dictation_stop":
        return False
    if kind == "open_artifact":
        from uuid import UUID
        UUID(params.get("artifact_id", ""))
        return False
    if kind == "computer":
        if len(json.dumps(params, ensure_ascii=False)) > 16000:
            raise ValueError("Computer batch exceeds reviewable payload budget")
        actions = params.get("actions")
        if not isinstance(actions, list) or not 1 <= len(actions) <= 8:
            raise ValueError("Computer batches must have 1 to 8 actions")
        known = {"click", "double_click", "move", "drag", "scroll", "keypress", "type", "wait", "screenshot"}
        if any(not isinstance(a, dict) or a.get("type") not in known for a in actions):
            raise ValueError("Unknown computer action")
        return bool(params.get("safety_checks")) or any(a["type"] not in {"screenshot", "move", "wait"} for a in actions)
    if kind in {"room_read", "room_call"}:
        if not isinstance(params, dict):
            raise ValueError("Room action has an unknown field")
        device = params.get("device")
        if kind == "room_read":
            if device not in ACTUATORS and device not in SENSORS:
                raise ValueError("Unknown room device")
            if set(params) - {"device"}:
                raise ValueError("Room action has an unknown field")
            return False
        field = ACTUATORS.get(device)
        if field == "power":
            if set(params) - {"device", "power"}:
                raise ValueError("Room action has an unknown field")
            if params.get("power") not in POWERS:
                raise ValueError("Room call needs a power")
        elif field == "job":
            job = params.get("job")
            if set(params) - {"device", "job"}:
                raise ValueError("Room action has an unknown field")
            if not isinstance(job, str) or not job.strip() or len(job) > 200:
                raise ValueError("Room call needs a short job")
        else:
            raise ValueError("Unknown room device")
        return True
    if kind in {"browser_read", "browser_act"}:
        check_page(action)
        return kind == "browser_act"
    if kind in {"mcp_read", "mcp_call"}:
        check_mcp(action)
        return kind == "mcp_call"
    raise ValueError("Unknown native action")
