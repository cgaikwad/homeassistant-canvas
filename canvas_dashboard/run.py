#!/usr/bin/env python3
"""
Home Assistant add-on entrypoint: polls Canvas on a schedule, pushes sensor
states to HA's Core API, and serves the dashboard HTML over ingress.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import canvas_lib

OPTIONS_PATH = "/data/options.json"
STATE_CACHE_PATH = "/data/state_cache.json"
INGRESS_PORT = 8099

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
CORE_API = "http://supervisor/core/api"

_latest_html_lock = threading.Lock()
_latest_html = "<html><body><p>Canvas dashboard: waiting on first fetch...</p></body></html>"


def log(message: str) -> None:
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    print(f"[{ts}] {message}", flush=True)


def load_options() -> dict:
    with open(OPTIONS_PATH) as f:
        return json.load(f)


def load_cache() -> dict:
    if os.path.exists(STATE_CACHE_PATH):
        with open(STATE_CACHE_PATH) as f:
            return json.load(f)
    return {}


def save_cache(cache: dict) -> None:
    with open(STATE_CACHE_PATH, "w") as f:
        json.dump(cache, f)


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "course"


def push_state(entity_id: str, state, attributes: dict) -> None:
    url = f"{CORE_API}/states/{entity_id}"
    payload = json.dumps({"state": str(state), "attributes": attributes}).encode()
    req = urllib.request.Request(
        url,
        method="POST",
        data=payload,
        headers={
            "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        log(f"WARNING: failed to push {entity_id}: {e.code} {e.reason} - {detail}")


def call_service(domain: str, service: str, data: dict) -> None:
    url = f"{CORE_API}/services/{domain}/{service}"
    payload = json.dumps(data).encode()
    req = urllib.request.Request(
        url,
        method="POST",
        data=payload,
        headers={
            "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        log(f"WARNING: failed to call {domain}.{service}: {e.code} {e.reason} - {detail}")


def notify(notification_id: str, title: str, message: str) -> None:
    """Create (or update, if already showing) a persistent notification in HA."""
    call_service(
        "persistent_notification",
        "create",
        {"notification_id": notification_id, "title": title, "message": message},
    )


def dismiss(notification_id: str) -> None:
    call_service("persistent_notification", "dismiss", {"notification_id": notification_id})


def push_token_status(options: dict, expired: bool) -> None:
    """Track token health so it's visible as a sensor and, when it's close to expiring
    or already rejected, as a Home Assistant notification (there's no Canvas API to
    rotate the token itself - personal access tokens are UI-only to create)."""
    expires_at_str = (options.get("token_expires_at") or "").strip()
    days_remaining = None
    if expires_at_str:
        try:
            expires_at = datetime.date.fromisoformat(expires_at_str)
            days_remaining = (expires_at - datetime.date.today()).days
        except ValueError:
            log(f"WARNING: token_expires_at '{expires_at_str}' isn't a valid YYYY-MM-DD date")

    if expired:
        state = "expired"
        notify(
            "canvas_token_auth",
            "Canvas token needs replacing",
            "The Canvas add-on's access token was rejected (expired or revoked). "
            "Generate a new personal access token from the observer account "
            "(Canvas → Account → Settings → Approved Integrations → + New Access Token) "
            "and paste it into the Canvas Dashboard add-on's Configuration tab, then "
            "restart it.",
        )
    else:
        dismiss("canvas_token_auth")
        if days_remaining is not None and days_remaining <= 7:
            state = "expiring_soon"
            notify(
                "canvas_token_expiring",
                "Canvas token expiring soon",
                f"The Canvas add-on's access token expires in {days_remaining} day(s). "
                "Generate a new one from the observer account and update it (and the "
                "token_expires_at date) in the add-on's Configuration tab.",
            )
        else:
            state = "ok"
            dismiss("canvas_token_expiring")

    push_state(
        "sensor.canvas_token_status",
        state,
        {
            "friendly_name": "Canvas token status",
            "icon": "mdi:key-alert" if state != "ok" else "mdi:key",
            "days_remaining": days_remaining,
        },
    )


def item_summary(i: dict) -> dict:
    return {
        "course": i["course"],
        "name": i["name"],
        "due_at": i["due_at"].isoformat(),
        "url": i["url"],
        "on_paper": i["on_paper"],
    }


def push_sensors(data: dict) -> None:
    missing = [i for i in data["items"] if i["status"] == "missing"]
    late = [i for i in data["items"] if i["status"] == "late" and not i["submitted"]]
    upcoming = [i for i in data["items"] if i["status"] == "upcoming"]

    push_state(
        "sensor.canvas_missing_count",
        len(missing),
        {
            "friendly_name": "Canvas missing assignments",
            "unit_of_measurement": "items",
            "icon": "mdi:file-alert",
            "items": [item_summary(i) for i in missing],
        },
    )
    push_state(
        "sensor.canvas_late_count",
        len(late),
        {
            "friendly_name": "Canvas late assignments",
            "unit_of_measurement": "items",
            "icon": "mdi:clock-alert",
            "items": [item_summary(i) for i in late],
        },
    )
    push_state(
        "sensor.canvas_upcoming_count",
        len(upcoming),
        {
            "friendly_name": "Canvas upcoming assignments",
            "unit_of_measurement": "items",
            "icon": "mdi:calendar-clock",
            "items": [item_summary(i) for i in upcoming],
        },
    )
    push_state(
        "sensor.canvas_last_updated",
        data["generated_at"].isoformat(),
        {
            "friendly_name": "Canvas last updated",
            "icon": "mdi:refresh",
            "device_class": "timestamp",
        },
    )

    for c in data["courses"]:
        entity_id = f"sensor.canvas_grade_{slugify(c['name'])}"
        push_state(
            entity_id,
            c["letter"] or "N/A",
            {
                "friendly_name": f"Canvas grade: {c['name']}",
                "icon": "mdi:school",
                "course": c["name"],
                "score": c["score"],
            },
        )


def fetch_and_publish(options: dict, cache: dict) -> None:
    client = canvas_lib.CanvasClient(options["canvas_domain"], options["canvas_token"])

    observee_id = options.get("observee_id") or cache.get("observee_id")
    if not observee_id:
        observee_id, name = canvas_lib.resolve_observee_id(client)
        log(f"Resolved observee: {name} (id={observee_id}); caching for future runs")
        cache["observee_id"] = observee_id
        save_cache(cache)

    data = canvas_lib.build_dashboard_data(client, observee_id)
    log(f"Fetched {len(data['courses'])} course(s), {len(data['items'])} item(s) in window")

    push_sensors(data)
    push_token_status(options, expired=False)

    html = canvas_lib.render_html(data)
    global _latest_html
    with _latest_html_lock:
        _latest_html = html


def poll_loop() -> None:
    options = load_options()
    cache = load_cache()
    interval_minutes = int(options.get("poll_interval_minutes") or 30)

    while True:
        try:
            fetch_and_publish(options, cache)
        except canvas_lib.CanvasAuthError as e:
            log(f"ERROR: {e}")
            push_token_status(options, expired=True)
        except RuntimeError as e:
            log(f"ERROR: {e}")
        except Exception as e:  # noqa: BLE001 - keep the loop alive no matter what
            log(f"ERROR: unexpected failure: {e}")
        time.sleep(max(60, interval_minutes * 60))


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib method name
        with _latest_html_lock:
            body = _latest_html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # silence default per-request stderr logging
        pass


def main():
    if not SUPERVISOR_TOKEN:
        log("WARNING: SUPERVISOR_TOKEN not set - sensor pushes to Home Assistant will fail. "
            "Make sure homeassistant_api: true is set in config.yaml.")

    threading.Thread(target=poll_loop, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", INGRESS_PORT), DashboardHandler)
    log(f"Serving dashboard on :{INGRESS_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
