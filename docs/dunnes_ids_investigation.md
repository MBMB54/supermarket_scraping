# Dunnes ID discovery: investigation (2026-10-07)

## Findings
- `raw/dunnes/ids/latest` was last modified 2026-07-05 (78 KB); the newest dated snapshot is `date=2026-07-05`.
  The ID job has not refreshed since, and it exited 0 each time (zero IDs were "rejected" quietly).
  `run_ids_job` now exits 1 once latest is older than `MAX_STALE_DAYS` (14), now disabled by default (`MAX_STALE_DAYS` unset = off).
- From this machine every page on `www.dunnesstoresgrocery.com` (including `robots.txt`, `sitemap.xml`,
  `sitemap_index.xml`) returns a Cloudflare managed-challenge page ("Just a moment...", HTTP 403) even with
  `curl_cffi` Chrome impersonation. After roughly 100 gateway requests from the same IP,
  `storefrontgateway.dunnesstoresgrocery.com` also began answering 429 with the same challenge page, so
  further probing was stopped.
- Not verified on AWS: whether the Fargate egress IPs are also challenged. The stale file date suggests yes
  (a working job would have refreshed it within 7 days), but job logs were not inspected and no job was submitted.

## Not done, and why
A browser-automation route (patchright, as Tesco IDs use) would need to pass Cloudflare's managed challenge.
That is deliberately circumventing an anti-bot control on a site whose robots.txt could not even be read, so it
was not attempted without the owner's decision. The cheap, non-evasive options are:
1. Run the ID job from a network the site does not challenge (e.g. home IP via a scheduled local run that uploads
   with `Storage`), keeping the shrink guard.
2. Use the gateway product listing/search endpoints (same host the API scraper already uses successfully on AWS)
   if a category-listing endpoint exists; not yet discovered, because probing was rate-limited.
3. Derive IDs from already-scraped raw data (no new products, but no further site load).

## Change made
`dunnes/dunnes_api.py`: `stores_per_chunk=200` was tried then reverted to 60 per user decision (all 103 stores would be cheaper-than-expected only for not-found IDs) (one sampled ID existed in only 3 stores;
a 60-store subset misses such items ~7% of the time). Cost: not-found IDs now take up to 103 requests instead of 60;
revert to 60 if Cloudflare throttling on the gateway appears in the run summaries (`error_samples`).

Old vs new ID comparison could not be produced because no fresh list could be fetched.
