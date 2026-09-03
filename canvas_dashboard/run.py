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
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import canvas_lib

OPTIONS_PATH = "/data/options.json"
STATE_CACHE_PATH = "/data/state_cache.json"
INGRESS_PORT = 8099

# Manual refresh button: minimum gap between actual Canvas calls, so repeated
# clicks (or an unauthenticated LAN-port iframe reloading) can't hammer the
# Canvas API.
REFRESH_COOLDOWN_SECONDS = 30

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
CORE_API = "http://supervisor/core/api"

# Loaded once in main() and then treated as shared, mutable state across the
# poll-loop thread and request-handler threads (fetch_and_publish() mutates
# `cache` in place and persists it via save_cache()).
_options: dict = {}
_cache: dict = {}

# Guards _latest_html and _last_fetch_at together, since fetch_and_publish()
# always updates them as a pair.
_latest_html_lock = threading.Lock()
_latest_html = "<html><body><p>Canvas dashboard: waiting on first fetch...</p></body></html>"
_last_fetch_at: datetime.datetime | None = None

# Serializes actual fetch_and_publish() calls across the poll loop and the
# manual refresh endpoint, so at most one Canvas fetch is ever in flight at
# a time.
_fetch_lock = threading.Lock()


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


def push_token_status(expired: bool) -> None:
    """Track token health as a sensor and, when Canvas actually rejects it, a Home
    Assistant notification (there's no Canvas API to rotate the token itself -
    personal access tokens are UI-only to create, and this instance doesn't even
    expose the Access Tokens API to check an expiration date up front)."""
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
        state = "ok"
        dismiss("canvas_token_auth")

    push_state(
        "sensor.canvas_token_status",
        state,
        {
            "friendly_name": "Canvas token status",
            "icon": "mdi:key-alert" if state != "ok" else "mdi:key",
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
    # Split "missing" into a confirmed bucket (Canvas has a digital record it's unsubmitted
    # and past due) and an on-paper bucket (Canvas has no record either way - see
    # canvas_lib.submission_status) so an automation can treat them differently.
    missing = [i for i in data["items"] if i["status"] == "missing" and not i["on_paper"]]
    missing_on_paper = [i for i in data["items"] if i["status"] == "missing" and i["on_paper"]]
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
        "sensor.canvas_missing_on_paper_count",
        len(missing_on_paper),
        {
            "friendly_name": "Canvas missing assignments (on paper, unconfirmed)",
            "unit_of_measurement": "items",
            "icon": "mdi:file-question",
            "items": [item_summary(i) for i in missing_on_paper],
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
    push_token_status(expired=False)

    html = canvas_lib.render_html(data)
    global _latest_html, _last_fetch_at
    with _latest_html_lock:
        _latest_html = html
        _last_fetch_at = datetime.datetime.now(datetime.timezone.utc)


def poll_loop() -> None:
    interval_minutes = int(_options.get("poll_interval_minutes") or 30)

    while True:
        try:
            with _fetch_lock:
                fetch_and_publish(_options, _cache)
        except canvas_lib.CanvasAuthError as e:
            log(f"ERROR: {e}")
            push_token_status(expired=True)
        except RuntimeError as e:
            log(f"ERROR: {e}")
        except Exception as e:  # noqa: BLE001 - keep the loop alive no matter what
            log(f"ERROR: unexpected failure: {e}")
        time.sleep(max(60, interval_minutes * 60))


def handle_manual_refresh() -> tuple[int, dict]:
    """Handles POST /api/refresh: force a fetch now, subject to the cooldown
    and fetch-lock coordination described above. Returns (http_status, json_body)."""
    with _latest_html_lock:
        last = _last_fetch_at
    if last is not None:
        elapsed = (datetime.datetime.now(datetime.timezone.utc) - last).total_seconds()
        if elapsed < REFRESH_COOLDOWN_SECONDS:
            wait = int(REFRESH_COOLDOWN_SECONDS - elapsed) + 1
            return 429, {"ok": False, "error": f"Please wait {wait}s before refreshing again."}

    if not _fetch_lock.acquire(blocking=True, timeout=30):
        return 503, {"ok": False, "error": "A refresh is already in progress. Try again shortly."}

    try:
        # Someone else's fetch (the scheduled poll, or a near-simultaneous refresh
        # click) may have run to completion while we were waiting for the lock
        # above - if so, ride on that result instead of hitting Canvas again.
        with _latest_html_lock:
            updated_while_waiting = _last_fetch_at != last
        if updated_while_waiting:
            return 200, {"ok": True}

        fetch_and_publish(_options, _cache)
    except canvas_lib.CanvasAuthError as e:
        log(f"ERROR (manual refresh): {e}")
        push_token_status(expired=True)
        return 502, {
            "ok": False,
            "error": "Canvas rejected the access token. Check the Home Assistant notification.",
        }
    except Exception as e:  # noqa: BLE001 - report back instead of 500ing the request
        log(f"ERROR (manual refresh): unexpected failure: {e}")
        return 502, {"ok": False, "error": "Refresh failed. It will retry automatically."}
    finally:
        _fetch_lock.release()

    return 200, {"ok": True}


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib method name
        # Deliberately no on-open refresh here - data only updates on the
        # poll_interval_minutes timer (poll_loop) or the manual refresh button.
        with _latest_html_lock:
            body = _latest_html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # This server is deliberately unauthenticated (LAN-only) so it can be embedded
        # in a Lovelace Webpage/iframe card for non-admin users - see README. Explicitly
        # allow framing from any origin (the HA frontend is a different origin/port)
        # rather than relying on there being no framing restriction by default.
        self.send_header("Content-Security-Policy", "frame-ancestors *")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802 - stdlib method name
        path = urllib.parse.urlparse(self.path).path
        if path != "/api/refresh":
            self.send_response(404)
            self.end_headers()
            return

        # Log receipt unconditionally, before anything can go wrong below - so
        # "nothing in the log" always means the request never reached the
        # add-on (e.g. a client-side/network issue), never a silently-swallowed
        # server-side failure.
        log(f"Manual refresh requested from {self.client_address[0]}")
        try:
            status, payload = handle_manual_refresh()
        except Exception as e:  # noqa: BLE001 - always answer and log, never die silently
            log(f"ERROR (manual refresh): unhandled exception: {e}")
            log(traceback.format_exc())
            status, payload = 500, {"ok": False, "error": "Unexpected add-on error. Check the add-on log."}

        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", "frame-ancestors *")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # silence default per-request stderr logging
        pass


def main():
    global _options, _cache
    if not SUPERVISOR_TOKEN:
        log("WARNING: SUPERVISOR_TOKEN not set - sensor pushes to Home Assistant will fail. "
            "Make sure homeassistant_api: true is set in config.yaml.")

    _options = load_options()
    _cache = load_cache()

    threading.Thread(target=poll_loop, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", INGRESS_PORT), DashboardHandler)
    log(f"Serving dashboard on :{INGRESS_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
