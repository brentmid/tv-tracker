# Original prompt (Brent, 2026-07-06, on a plane)

Preserved verbatim per the prompt's own instruction. This is the founding requirement set for tv-tracker.

> the "TV Time" app is going out of business in about a week. I want to rebuild a basic web interface that lives on this mac studio that will manage my watch history for shows and movies the same way TV Time did. I am able to export my existing data from the app for you to consume to start up. This can be very basic. Keep a queue of the shows I'm watching and let me check off episodes as we go. Add new shows to the queue. Archive shows I stop watching. Maintain a movie list and check them off as i go. Keep the latest release dates for show episodes up to date by searching the web. Give me a stats page for watch history. Do some light research on what TV time does today so we can build a plan. Keep very detailed notes on the plan in a new subdirectory with it's own git repo - name the subdirectory "tv-tracker". Use the same "local webserver" pattern we used for portfolio-agent. Follow conventions and patterns from other projects as needed - claude.md structure, gitignore, etc. I'm on a flaky airplane connection so be sure to keep detailed notes on the local filesystem as we go so we don't lose progress if this session goes down. ask me any clarifying questions - and keep updating local files as we go - i can't stress this enough - if this connection is severed, i want to pick up later by going to that directory and saying "keep going" - make sure to keep this original prompt as well in a local file

Follow-up decisions given during planning (same session):

- The TV Time GDPR export arrived mid-planning as `~/bin/gdpr-data.zip`; Brent said to inspect it in tmp and add "move it to the correct project directory" to the plan. (Done: it now lives at `baselines/import/gdpr-data.zip`.)
- Web UI reachable via localhost + Tailscale (portfolio-agent pattern).
- TVmaze (keyless) for TV + TMDB (free key, Brent registers) for movies.
- Air-date refresh: manual button for MVP; daily LaunchAgent later.
