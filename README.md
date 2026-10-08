# Social Announce Bot

Posts to a Discord channel whenever new content appears on a tracked LinkedIn
company page or YouTube channel. Each server sets itself up independently —
one bot install, many servers, each tracking whatever they want.

- **YouTube**: fully automatic, via RSS polling (no API key).
- **LinkedIn**: fully automatic, via `linkedin_scraper.py` scraping the
  public company page (no login, no API key). This is against LinkedIn's
  terms of service and can break or get rate-limited/blocked at any time —
  see the warnings in `linkedin_scraper.py`. Keep polling infrequent.
- **`/announce`**: still there for a one-off manual post of anything.

## Local setup

1. `pip install -r requirements.txt`
2. Create a Discord application at https://discord.com/developers/applications
   - **Bot** tab -> create a bot user, copy its token.
   - **OAuth2 -> URL Generator** -> scopes `bot` + `applications.commands`,
     permissions `Send Messages` + `Embed Links`. Use the generated URL to
     invite it to your server.
3. Copy `.env.example` -> `.env`, fill in `DISCORD_TOKEN`.
4. `python bot.py`

## Using it in a server

Run these once per server (each needs "Manage Server" permission, except `/status`):

- `/setup` - run this in the channel you want announcements posted to.
- `/setup-linkedin url:https://www.linkedin.com/company/<slug>/` - start
  tracking a company page. It immediately scrapes the page once to mark
  existing posts as seen, so only future posts get announced.
- `/setup-youtube channel_id:UCxxxxxxxx` - start tracking a YouTube channel
  (the channel ID, not the @handle).
- `/status` - see what's currently configured for this server.
- `/remove-linkedin` - stop tracking the LinkedIn page (clears its seen-post history too).
- `/remove-youtube` - stop tracking the YouTube channel (clears its seen-video history too).
- `/announce platform:LinkedIn url:<link> note:<text>` - post something by hand.

## Known limitations

- **LinkedIn scraping is fragile by nature.** LinkedIn hides more from
  logged-out visitors over time, and some company pages return little or
  nothing. `/setup-linkedin` tells you right away if the first fetch found
  no posts or got blocked.
- **Shared pages are fetched once per poll pass**, not once per server, so
  tracking the same company from multiple servers doesn't multiply load on
  LinkedIn.
- **Because this scrapes LinkedIn, it is not eligible for Discord's App
  Directory as-is.** The YouTube side alone would be fine to list; shipping
  both together publicly risks the app being rejected or pulled per
  Discord's developer policy (no facilitating ToS violations of other
  platforms). Keep this to private servers, or ship a LinkedIn-free build
  for the Directory and keep this version for yourself.

## Getting the YouTube-only version onto the Discord App Directory

If you want a public listing later, strip the LinkedIn pieces (the
`setup-linkedin` command, `poll_linkedin`, and `linkedin_scraper.py` import)
and follow Discord's directory steps:

1. **Developer Portal -> General Information**: description, icon, and the
   required **Privacy Policy URL** + **Terms of Service URL**.
2. **Installation tab**: enable "Public Bot" with `bot` + `applications.commands`.
3. Past Discord's verification threshold (a server-count number that
   changes - check current figures before you get close), you'll go through
   bot verification, which reviews your privacy policy and data handling.
4. Check https://support.discord.com/hc/en-us/articles/24007107252247 close
   to submission time, since the exact eligibility criteria shift.
