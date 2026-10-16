# Scheduling

The backend runs its own scheduler (APScheduler) in the configured `timezone`:

| Job | When | What it does |
| --- | --- | --- |
| Morning brief | `brief_times.morning` (default 08:00) | Builds the morning brief, stores it and pushes it to connected clients |
| Evening wrap-up | `brief_times.evening` (default 18:30) | Open items, unacknowledged delegations and a proposed rollover |
| Sync | every `scheduler.sync_interval_minutes` (default 15; 0 turns it off) | Pulls calendars, mail and tasks, scores what changed, and sends an alert when a P1 item is new or changed |

If the backend was down at a scheduled time, the job still runs when it comes back, provided that is within 15 minutes. Missed runs are never stacked up.

Any client can fetch the last brief it missed with `GET /brief/latest?kind=morning|evening|alert`. `GET /brief/today?kind=morning|evening` builds one on demand without storing or pushing it.

## Alternative: cron or GitHub Actions

Use this if the backend runs on a host that sleeps, or if you would rather drive the timing yourself.

1. Set `SCHEDULER_ENABLED=false` in `.env`.
2. Get an API token: `curl -X POST "$BASE/auth/token" -H 'content-type: application/json' -d '{"secret":"<API_AUTH_TOKEN>","client_name":"cron"}'`.
3. Call the job endpoints on your own schedule.

### cron

Times are in the cron host's timezone.

```cron
*/15 * * * *  curl -fsS -X POST -H "Authorization: Bearer $TOKEN" "$BASE/sync"
0 8 * * *     curl -fsS -X POST -H "Authorization: Bearer $TOKEN" "$BASE/brief/deliver?kind=morning"
30 18 * * *   curl -fsS -X POST -H "Authorization: Bearer $TOKEN" "$BASE/brief/deliver?kind=evening"
```

### GitHub Actions

GitHub schedules run in UTC and can start a few minutes late. Save `ASSISTANT_URL` and `ASSISTANT_TOKEN` as repository secrets. 02:30 UTC is 08:00 in Asia/Kolkata.

```yaml
name: assistant-schedule
on:
  schedule:
    - cron: "*/15 * * * *"  # sync
    - cron: "30 2 * * *"    # morning brief
    - cron: "0 13 * * *"    # evening wrap-up (18:30 IST)
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - name: Call the assistant
        env:
          BASE: ${{ secrets.ASSISTANT_URL }}
          TOKEN: ${{ secrets.ASSISTANT_TOKEN }}
        run: |
          case "${{ github.event.schedule }}" in
            "30 2 * * *") path="brief/deliver?kind=morning" ;;
            "0 13 * * *") path="brief/deliver?kind=evening" ;;
            *) path="sync" ;;
          esac
          curl -fsS -X POST -H "Authorization: Bearer $TOKEN" "$BASE/$path"
```

The backend must be reachable from the internet for this (see the Cloudflare Tunnel notes in `docs/DEPLOY.md`, added with Milestone 14).
