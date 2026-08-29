# Canvas Dashboard (Home Assistant add-on)

Polls Canvas LMS (via a parent Observer account) for an observed student's
courses, grades, and assignment status, and:

- Pushes sensors into Home Assistant:
  - `sensor.canvas_missing_count`, `sensor.canvas_late_count`,
    `sensor.canvas_upcoming_count` — each with an `items` attribute listing
    the actual assignments (course, name, due date, url).
  - `sensor.canvas_grade_<course>` — one per active course, state = letter
    grade, `score` attribute = numeric percent.
  - `sensor.canvas_last_updated` — timestamp of the last successful poll.
  - `sensor.canvas_token_status` — `ok` / `expiring_soon` / `expired`
    (`days_remaining` attribute), see **Token expiration** below.
- Serves the same due/missing/late + grades dashboard as an ingress panel
  (shows up in the HA sidebar when "Show in sidebar" is enabled).

## Token expiration

Canvas has no API to rotate a personal access token — creating one is a
browser-UI-only action, so this add-on can't generate a replacement for you.
What it does instead:

- If Canvas rejects the token (expired/revoked), the add-on immediately
  raises a **persistent notification** in Home Assistant telling you to
  regenerate it, and `sensor.canvas_token_status` flips to `expired`.
- If you set the `token_expires_at` option (the date Canvas shows on the
  token when you create it, `YYYY-MM-DD`), the add-on also warns you
  **7 days before** it expires — same notification mechanism, plus
  `sensor.canvas_token_status` becomes `expiring_soon`. Leave it blank to
  skip this proactive warning and rely on the expired-token notification
  alone.

When you do regenerate: paste the new token (and, if you're using it, the
new `token_expires_at`) into the add-on's Configuration tab and restart it.

## Install (local add-on, no repo needed)

1. Copy this `addon/` folder onto the HA host at
   `/addons/local/canvas_dashboard/` (e.g. via the Samba or SSH & Terminal
   add-on).
2. In HA: **Settings → Add-ons → Add-on Store**, then refresh (⋮ menu) —
   "Canvas Dashboard" appears under **Local add-ons**. Install it.
3. Open the add-on's **Configuration** tab and fill in:
   - `canvas_domain` — e.g. `smsd.instructure.com`
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
