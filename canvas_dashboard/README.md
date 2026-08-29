# Canvas Dashboard (Home Assistant add-on)

Polls Canvas LMS (via a parent Observer account) for an observed student's
courses, grades, and assignment status, and:

- Pushes sensors into Home Assistant:
  - `sensor.canvas_missing_count` — confirmed missing (Canvas has a digital
    record it's unsubmitted and past due).
  - `sensor.canvas_missing_on_paper_count` — same, but for on-paper
    assignments, where Canvas has no record either way (turned in by hand,
    possibly already graded on paper) — treat this as "worth checking on,"
    not a confirmed miss.
  - `sensor.canvas_late_count`, `sensor.canvas_upcoming_count` — each with
    an `items` attribute listing the actual assignments (course, name, due
    date, url); the missing sensors above carry the same attribute.
  - `sensor.canvas_grade_<course>` — one per active course, state = letter
    grade, `score` attribute = numeric percent.
  - `sensor.canvas_last_updated` — timestamp of the last successful poll.
  - `sensor.canvas_token_status` — `ok` / `expired`, see **Token expiration**
    below.
- Serves the same due/missing/late + grades dashboard as an ingress panel
  (shows up in the HA sidebar when "Show in sidebar" is enabled).

## Token expiration

Canvas has no API to rotate a personal access token — creating one is a
browser-UI-only action, and this instance doesn't even expose the Access
Tokens API to read an expiration date up front — so this add-on can't warn
you in advance or generate a replacement for you.

What it does instead: the moment Canvas actually rejects the token
(expired or revoked), the add-on raises a **persistent notification** in
Home Assistant telling you to regenerate it, and `sensor.canvas_token_status`
flips to `expired`. Generate a new personal access token from the observer
account (Canvas → Account → Settings → Approved Integrations → + New Access
Token), paste it into the add-on's Configuration tab, and restart it — the
notification clears itself on the next successful poll.

## Install (local add-on, no repo needed)

1. Copy this `addon/` folder onto the HA host at
   `/addons/local/canvas_dashboard/` (e.g. via the Samba or SSH & Terminal
   add-on).
2. In HA: **Settings → Add-ons → Add-on Store**, then refresh (⋮ menu) —
   "Canvas Dashboard" appears under **Local add-ons**. Install it.
3. Open the add-on's **Configuration** tab and fill in:
   - `canvas_domain` — your school's Canvas hostname, e.g. `yourschool.instructure.com`
   - `canvas_token` — a personal access token generated *from the observer
     account* (Canvas → Account → Settings → Approved Integrations → New
     Access Token)
   - `observee_id` — leave as `0` to auto-resolve on first run **if** the
     observer account only observes one student; set it explicitly if it
     observes more than one (check the add-on log after a failed first run
     for the list of ids).
   - `poll_interval_minutes` — how often to fetch (default 30).
4. **Start** the add-on, then check its **Log** tab for a successful fetch.
5. Enable **Show in sidebar** to get the dashboard panel.

## Example automation

Notify when something new goes missing:

```yaml
automation:
  - alias: "Canvas: notify on new missing assignment"
    trigger:
      - platform: numeric_state
        entity_id: sensor.canvas_missing_count
        above: 0
    condition:
      - condition: template
        value_template: >
          {{ trigger.to_state.state | int > (trigger.from_state.state | int(0)) }}
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "Canvas: missing assignment"
          message: >
            {{ state_attr('sensor.canvas_missing_count', 'items')
               | map(attribute='name') | list | join(', ') }}
```
