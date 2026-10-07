# Tesco ID refresh (runs locally, weekly)

tesco.ie serves a "browser has failed some security checks" page to AWS IPs, so `tesco/tesco_ids.py`
cannot run on Batch (test on 2026-10-07; same failure screenshot dated 2026-07-20). It runs from this
Mac instead, weekly, and writes to the same S3 paths (guarded by the empty/50%-shrink check in
`scraper_common.ids`).

- Script: `scripts/run_tesco_ids_local.sh` (`uv run --group scraper python tesco/tesco_ids.py`). Logs:
  `~/Library/Logs/supermarket/tesco_ids_YYYY-MM-DD.log`. Non-zero exit and a macOS notification on failure
  or when the guard rejects the result. Uses the default AWS credentials profile.
- Schedule: `scripts/launchd/com.supermarket.tesco-ids.plist`, Sundays 06:00 local time. The daily
  Lambda rule fires at 08:00 UTC, so a crawl of about an hour finishes before the chunk jobs read
  `ids/latest`. If the Mac is asleep at 06:00, launchd runs the job when it wakes (it may then finish after
  the day's chunks; they just use the previous list). The 20-hour self-skip in `tesco_ids.py` does not
  interfere with a weekly cadence; to force a run sooner use `FORCE_REFRESH=1 ./scripts/run_tesco_ids_local.sh`.

Install:
```bash
mkdir -p ~/Library/Logs/supermarket
sed "s#__REPO__#$PWD#; s#__HOME__#$HOME#" scripts/launchd/com.supermarket.tesco-ids.plist \
  > ~/Library/LaunchAgents/com.supermarket.tesco-ids.plist
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.supermarket.tesco-ids.plist
launchctl print gui/$UID/com.supermarket.tesco-ids
launchctl kickstart gui/$UID/com.supermarket.tesco-ids   # run now
```
Uninstall:
```bash
launchctl bootout gui/$UID/com.supermarket.tesco-ids
rm ~/Library/LaunchAgents/com.supermarket.tesco-ids.plist
```
`tesco_ids.py` is still in `Dockerfile.scraper` but Lambda does not schedule it.
