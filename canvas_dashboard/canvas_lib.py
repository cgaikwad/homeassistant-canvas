"""
Canvas fetch/status/render logic, shared between the standalone Mac script
(../canvas_dashboard.py) and this Home Assistant add-on (run.py).

No config-file or CLI concerns here on purpose — just: given a domain/token/
observee_id, fetch data from Canvas and turn it into dashboard data / HTML.
"""

from __future__ import annotations

import datetime
import json
import urllib.error
import urllib.parse
import urllib.request
from html import escape

DAYS_AHEAD = 14   # how far into the future to show upcoming work
DAYS_BEHIND = 14  # how far back to show recently graded/past-due items


class CanvasClient:
    def __init__(self, domain: str, token: str):
        self.base = f"https://{domain}/api/v1"
        self.token = token

    def get_all(self, path: str, params: dict | None = None) -> list:
        """GET a Canvas API path, following pagination, returning combined list results."""
        params = dict(params or {})
        params.setdefault("per_page", "100")
        url = f"{self.base}{path}?{urllib.parse.urlencode(params, doseq=True)}"
        results = []
        while url:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = resp.read()
                    link_header = resp.headers.get("Link", "")
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")
                raise RuntimeError(f"GET {url} failed: {e.code} {e.reason} - {detail}") from e
            data = json.loads(body)
            if isinstance(data, list):
                results.extend(data)
            else:
                return data  # single-object response, no pagination
            url = None
            for part in link_header.split(","):
                part = part.strip()
                if part.endswith('rel="next"'):
                    url = part.split(";")[0].strip().strip("<>")
                    break
        return results

    def get(self, path: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        url = f"{self.base}{path}?{urllib.parse.urlencode(params, doseq=True)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"GET {url} failed: {e.code} {e.reason} - {detail}") from e


def resolve_observee_id(client: CanvasClient) -> tuple[int, str]:
    """Look up the (single) student this observer account observes. Raises RuntimeError
    with a human-readable message if there's none or more than one (caller should set
    observee_id explicitly in that case)."""
    observees = client.get_all("/users/self/observees")
    if not observees:
        raise RuntimeError(
            "This account has no observees. Make sure you paired with your son's pairing "
            "code (Canvas Settings > Pair with Observer) and are logged in as the observer "
            "account when generating the API token."
        )
    if len(observees) > 1:
        names = ", ".join(f"{o['name']} (id={o['id']})" for o in observees)
        raise RuntimeError(
            f"This observer account observes multiple students: {names}. "
            f"Set observee_id explicitly in the add-on configuration."
        )
    return observees[0]["id"], observees[0]["name"]


def fetch_courses(client: CanvasClient, observee_id: int) -> list:
    return client.get_all(
        "/courses",
        {
            "enrollment_state": "active",
            "include[]": ["total_scores", "observed_users", "term"],
            "observed_user_id": observee_id,
        },
    )


def current_grade_for_course(course: dict, observee_id: int):
    """Pull the observed student's current grade out of a course's enrollments."""
    for enr in course.get("enrollments", []):
        if enr.get("user_id") == observee_id or enr.get("associated_user_id") == observee_id:
            score = enr.get("computed_current_score")
            letter = enr.get("computed_current_grade")
            if score is not None or letter is not None:
                return score, letter
    return None, None


def fetch_assignments(client: CanvasClient, course_id: int, observee_id: int) -> list:
    """The observed student's submissions (with the assignment embedded) for one course.

    `include[]=submission` + `as_user_id` on /assignments isn't honored on every Canvas
    instance for observer accounts (it 401s as "Invalid as_user_id" on some). The
    gradebook-style /students/submissions endpoint, scoped with student_ids[], works for
    observers and returns each submission with its assignment nested inside — so we use
    that as the assignment list instead of GET .../assignments directly.
    """
    submissions = client.get_all(
        f"/courses/{course_id}/students/submissions",
        {"student_ids[]": observee_id, "include[]": "assignment"},
    )
    assignments = []
    for sub in submissions:
        a = sub.get("assignment")
        if not a:
            continue
        a = dict(a)
        a["submission"] = sub
        assignments.append(a)
    return assignments


def submission_status(assignment: dict, now: datetime.datetime) -> str:
    """
    Note: Canvas's own `missing` flag is only computed for online submission types
    (online_upload, online_text_entry, etc). For on_paper / no_submission assignments
    (e.g. worksheets turned in physically), Canvas never sets `missing` even when
    nothing's been turned in and the due date has passed — so we also treat "past due,
    nothing submitted, not excused" as missing ourselves rather than relying solely on
    Canvas's flag.
    """
    sub = assignment.get("submission") or {}
    due_at = assignment.get("due_at")
    due_dt = datetime.datetime.fromisoformat(due_at.replace("Z", "+00:00")) if due_at else None

    if sub.get("excused"):
        return "excused"
    if sub.get("late"):
        return "late"
    if sub.get("workflow_state") == "graded" or sub.get("score") is not None:
        return "graded"
    if sub.get("submitted_at"):
        return "submitted"
    if sub.get("missing") or (due_dt and due_dt < now):
        return "missing"
    return "upcoming"


def build_dashboard_data(client: CanvasClient, observee_id: int) -> dict:
    now = datetime.datetime.now(datetime.timezone.utc)
    window_start = now - datetime.timedelta(days=DAYS_BEHIND)
    window_end = now + datetime.timedelta(days=DAYS_AHEAD)

    courses = fetch_courses(client, observee_id)

    course_summaries = []
    items = []

    for course in courses:
        course_name = course.get("name", f"Course {course.get('id')}")
        score, letter = current_grade_for_course(course, observee_id)
        course_summaries.append({"name": course_name, "score": score, "letter": letter})

        assignments = fetch_assignments(client, course["id"], observee_id)

        for a in assignments:
            due_at = a.get("due_at")
            due_dt = datetime.datetime.fromisoformat(due_at.replace("Z", "+00:00")) if due_at else None
            if due_dt and not (window_start <= due_dt <= window_end):
                continue  # outside the window we care about
            if due_dt is None:
                continue  # skip undated assignments

            sub = a.get("submission") or {}
            submission_types = a.get("submission_types") or []
            items.append({
                "course": course_name,
                "name": a.get("name", "(untitled assignment)"),
                "due_at": due_dt,
                "status": submission_status(a, now),
                "submitted": bool(sub.get("submitted_at")),
                # Canvas has no digital record of these being turned in — see submission_status().
                "on_paper": "on_paper" in submission_types or "none" in submission_types,
                "score": sub.get("score"),
                "points_possible": a.get("points_possible"),
                "url": a.get("html_url"),
            })

    items.sort(key=lambda i: i["due_at"])
    course_summaries.sort(key=lambda c: c["name"])
    return {"courses": course_summaries, "items": items, "generated_at": now}


STATUS_STYLE = {
    "missing": ("#b91c1c", "#fee2e2"),
    "late": ("#b45309", "#fef3c7"),
    "graded": ("#15803d", "#dcfce7"),
    "submitted": ("#1d4ed8", "#dbeafe"),
    "excused": ("#6b21a8", "#f3e8ff"),
    "upcoming": ("#374151", "#f3f4f6"),
}


def render_html(data: dict) -> str:
    generated_at = data["generated_at"].astimezone().strftime("%a %b %-d, %Y %-I:%M %p")

    def score_cell(c):
        return "" if c["score"] is None else f"{c['score']:.1f}%"

    course_rows = "\n".join(
        f"<tr><td>{escape(c['name'])}</td>"
        f"<td>{escape(c['letter'] or '')}</td>"
        f"<td>{score_cell(c)}</td></tr>"
        for c in data["courses"]
    )

    def status_badge(status: str, submitted: bool, on_paper: bool) -> str:
        title = ""
        if status == "late" and submitted:
            # Turned in, just after the due date — already handled, no action needed.
            label, color, bg = "late (submitted)", "#15803d", "#dcfce7"
        elif status == "missing" and on_paper:
            # Canvas has no digital record for on-paper work, so this isn't a confirmed
            # miss — just "nothing logged in Canvas as of the due date."
            label, color, bg = "on-paper · unconfirmed", "#7c3aed", "#ede9fe"
            title = "Turned in on paper, not online — Canvas can't confirm this one either way. Worth checking with him/the teacher directly."
        else:
            label = status
            color, bg = STATUS_STYLE.get(status, ("#374151", "#f3f4f6"))
        title_attr = f' title="{escape(title)}"' if title else ""
        return f'<span class="badge" style="color:{color};background:{bg}"{title_attr}>{escape(label)}</span>'

    item_rows = []
    for i in data["items"]:
        due_str = i["due_at"].astimezone().strftime("%a %b %-d, %-I:%M %p")
        score_str = ""
        if i["score"] is not None:
            pts = f"/{i['points_possible']:g}" if i["points_possible"] else ""
            score_str = f"{i['score']:g}{pts}"
        link_open, link_close = (f'<a href="{escape(i["url"])}" target="_blank">', "</a>") if i["url"] else ("", "")
        item_rows.append(
            f"<tr data-status=\"{escape(i['status'])}\">"
            f"<td>{due_str}</td>"
            f"<td>{escape(i['course'])}</td>"
            f"<td>{link_open}{escape(i['name'])}{link_close}</td>"
            f"<td>{status_badge(i['status'], i['submitted'], i['on_paper'])}</td>"
            f"<td>{score_str}</td>"
            f"</tr>"
        )
    item_rows_html = "\n".join(item_rows)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Canvas Dashboard</title>
<style>
  :root {{
    --bg: #f9fafb; --card: #ffffff; --text: #111827; --muted: #6b7280; --border: #e5e7eb;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #0b0f17; --card: #131a26; --text: #e5e7eb; --muted: #9ca3af; --border: #263041; }}
  }}
  body {{ margin:0; padding:2rem; background:var(--bg); color:var(--text);
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
  h1 {{ font-size:1.4rem; margin:0 0 .25rem; }}
  .meta {{ color:var(--muted); font-size:.85rem; margin-bottom:1.5rem; }}
  .card {{ background:var(--card); border:1px solid var(--border); border-radius:10px;
           padding:1.25rem; margin-bottom:1.5rem; }}
  h2 {{ font-size:1.05rem; margin:0 0 1rem; }}
  table {{ width:100%; border-collapse:collapse; font-size:.9rem; }}
  th {{ text-align:left; padding:.5rem .6rem; color:var(--muted); font-weight:600;
        border-bottom:1px solid var(--border); cursor:pointer; user-select:none; }}
  td {{ padding:.5rem .6rem; border-bottom:1px solid var(--border); }}
  tr:last-child td {{ border-bottom:none; }}
  a {{ color:inherit; }}
  .badge {{ padding:.15rem .5rem; border-radius:999px; font-size:.78rem; font-weight:600; }}
  .filters {{ margin-bottom:.75rem; }}
  .filters button {{ background:var(--bg); border:1px solid var(--border); color:var(--text);
                      padding:.3rem .7rem; border-radius:999px; font-size:.8rem; margin-right:.4rem;
                      cursor:pointer; }}
  .filters button.active {{ background:var(--text); color:var(--bg); }}
</style>
</head>
<body>
  <h1>Canvas Dashboard</h1>
  <div class="meta">Generated {generated_at}</div>

  <div class="card">
    <h2>Due soon / missing / late</h2>
    <div class="filters" id="filters">
      <button data-filter="all" class="active">All</button>
      <button data-filter="missing">Missing</button>
      <button data-filter="late">Late</button>
      <button data-filter="upcoming">Upcoming</button>
      <button data-filter="graded">Graded</button>
    </div>
    <table id="items">
      <thead><tr><th>Due</th><th>Course</th><th>Assignment</th><th>Status</th><th>Score</th></tr></thead>
      <tbody>
      {item_rows_html}
      </tbody>
    </table>
  </div>

  <div class="card">
    <h2>Current grades</h2>
    <table>
      <thead><tr><th>Course</th><th>Letter</th><th>Score</th></tr></thead>
      <tbody>
      {course_rows}
      </tbody>
    </table>
  </div>

<script>
  document.getElementById('filters').addEventListener('click', (e) => {{
    const btn = e.target.closest('button');
    if (!btn) return;
    document.querySelectorAll('#filters button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const filter = btn.dataset.filter;
    document.querySelectorAll('#items tbody tr').forEach(row => {{
      const status = row.dataset.status;
      const show = filter === 'all' || status === filter;
      row.style.display = show ? '' : 'none';
    }});
  }});
</script>
</body>
</html>
"""
