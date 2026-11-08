# folk-to-anki

Flags, in Palisade's folk CRM, the people Eli has met or is about to meet (read from his Google
Calendar), and builds Anki cards for them from his networking templates: faces, states, parties,
offices, committee chairs. Built 2026-11-07 to replace hand-making these cards in Roam. The
existing Roam-made cards in the two networking decks are left exactly as they are; this tool only
touches notes it created itself.

## How it works

1. **Calendar → folk (`flag`).** Reads the Google Calendar "secret iCal" feeds (the same URLs the
   executive assistant uses), unfolds the wrapped lines, and collects every attendee or organizer
   on a non-cancelled event with at most `MAX_ATTENDEES` people, skipping Eli's own addresses,
   colleague domains, declined invites and room/shared-calendar addresses. Each external person
   gets a *last met* date (latest past event) and *next meeting* date (earliest event within
   `UPCOMING_DAYS`). People are matched to folk by email, then by normalized full name. Matches
   get the folk custom fields **Meeting status** (`Met` / `Planning to meet`), **Last met** and
   **Next meeting** on the `FOLK_GROUP` group, and are added to that group if they were not in it.
   Calendar people with no folk record are listed in `not-in-folk.txt` for Eli to add by hand;
   the tool never creates folk people.
2. **folk → Anki (`cards`).** Takes every person in `FOLK_GROUP` whose Meeting status is set, or
   whose **Anki card** field names a type, unless Anki card is `Skip`. Card type is the Anki card
   field, else inferred: name matches a current member of Congress → Congressperson; member of
   `FOLK_DC_GROUP` or a `senate.gov`/`house.gov` email → DC staffer; otherwise Networking.
3. Facts come from folk (fields **Party**, **Office** falling back to the primary company,
   **Deal** falling back to description then "job title at company"; a Networking person with neither Deal nor description is warned about in the log, since the fallback is thin for independents, **Photo URL**) and, for
   Congress, from the public `@unitedstates` datasets: state, party, chamber, chair / ranking
   member of full committees, and the official portrait. Photo order: Congress portrait, folk
   Photo URL, `PHOTOS_DIR/<folk id>.jpg`, the picture folk attached to the person in a past
   calendar or email interaction, and finally a web search by a Claude agent (next point). No
   photo means no face card, with a warning in the log.
4. **Photo agent** (`photo_finder.py`, Claude Agent SDK, model `claude-opus-5-5`). It runs through
   the Claude Code login on this Mac, so there is no API key to manage; each search is a short
   agent session (about 7 turns and $0.15 in the trial run). The agent has WebSearch, WebFetch,
   and three in-process tools: `page_images` (lists a page's images with alt text), `view_image`
   (shows it the picture so it can check the face is the right person) and the shared
   `contact_eli`. It answers with a structured `{url, source_page, confidence, reason}`; low
   confidence is discarded, the URL is downloaded to prove it is an image, and a hit is written
   to the person's **Photo URL** field in folk so it is visible and overridable in the CRM.
   Each person is searched once; a miss is retried after `PHOTO_SEARCH_RETRY_DAYS`. The system
   prompt starts with the shared preamble; a `contact_eli` call or `@ELI:` line is recorded in
   the LLM inbox and stops the run (exit code 2).
5. `cards.py` turns a person into notes per template (see the docstring there). Every note has a
   `Key` field `<folk person id>|<kind>`; `anki.py` searches Anki for that key and adds, updates
   or leaves the note alone. Photos go into Anki's media folder as `folk-<id>.jpg`. Note types
   `Folk Basic` and `Folk Cloze` and the decks are created on first run. Nothing is ever deleted.
6. `run --daily` does both steps and records a success stamp so the later launchd slots are no-ops.

## Files

| File | Purpose |
|------|---------|
| `folk_to_anki.py` | CLI and orchestration: `setup`, `flag`, `cards`, `run`, `test` |
| `folk_api.py` | folk REST client; `OWNED_FIELDS` defines the custom fields the tool creates |
| `calendar_feed.py` | iCal fetch, minimal VEVENT parse, met / upcoming contact extraction |
| `congress.py` | Congress name matching, facts and photo from `@unitedstates` data (cached daily) |
| `cards.py` | the templates as code: person → notes |
| `photo_finder.py` | Claude agent that finds a headshot; `photo_finder.py "Name" "context"` tries it by hand |
| `anki.py` | AnkiConnect: note types, decks, media, add-or-update by key |
| `config.py` | reads `config.env` |
| `.venv/` | Python with `anthropic` and `claude-agent-sdk` (untracked; see Common tasks) |
| `tests/fixtures/` | fake folk people and a fake calendar for `test` |
| `com.elityre.folk-to-anki.plist`, `install.sh` | launchd agent (07:30, 12:30, 18:30) and installer |

## Runtime locations

- Config: `config.env` in this folder (untracked; template `config.env.example`).
- State: `~/Library/Application Support/FolkToAnki/state.json` (uploaded photos, photo searches, last success),
  `not-in-folk.txt` (calendar contacts without a folk record), `congress/` (data cache),
  `photos/` (manual photos named `<folk person id>.jpg`).
- Log: `~/Library/Logs/folk-to-anki.log` (launchd's own stdout in `folk-to-anki.launchd.log`).
- Installed agent: `~/Library/LaunchAgents/com.elityre.folk-to-anki.plist`.

## Common tasks

```bash
cd ~/Documents/agent-coding/folk-to-anki
/opt/homebrew/bin/python3 -m venv .venv && .venv/bin/pip install claude-agent-sdk   # once
cp config.env.example config.env            # then fill in FOLK_API_KEY and CALENDAR_ICS_URLS
./folk_to_anki.py setup                     # list folk groups, show which custom fields are missing
./folk_to_anki.py setup --create-fields     # create them on FOLK_GROUP
./folk_to_anki.py flag --dry-run            # what the calendar would change in folk
./folk_to_anki.py cards --dry-run           # print the cards that would be made
./folk_to_anki.py run                       # do both for real
./folk_to_anki.py test                      # end to end with fixtures into deck "Folk to Anki test"
./folk_to_anki.py test --cleanup            # delete that test deck
.venv/bin/python photo_finder.py "Jane Doe" "legislative assistant, Office of Sen. X"   # try the photo agent
./install.sh                                # install / restart the launchd agent
launchctl bootout gui/$(id -u)/com.elityre.folk-to-anki   # stop it
tail -f ~/Library/Logs/folk-to-anki.log
```

`PHOTO_SEARCH=off` in `config.env` disables the agent; `cards --dry-run` never runs it.

Hand-picking people: set **Anki card** in folk to `Congressperson`, `DC staffer` or `Networking`
to card someone the calendar never saw, or `Skip` to never card them. Add **Photo URL** (any
direct image link) for staffers and networking contacts, since folk's API exposes no profile
pictures.

## Settings that matter

- Anki must be open with the AnkiConnect add-on (port 8765); the Roam import relies on the same.
- folk API access needs the Premium plan; the key is made under Settings → API keys. The key
  acts as Eli in the shared Palisade workspace, so field writes are visible to colleagues.
- Google Calendar → Settings → the calendar → "Secret address in iCal format" for each feed.
- The photo agent needs the `claude` CLI (`~/.local/bin/claude`) logged in; its usage is billed to
  that Claude account. The launchd plist puts `~/.local/bin` on PATH for this reason.

## Gotchas learned

- The folk person object has no picture field (checked 2026-11-07 against the API reference);
  only interaction participants carry a `picture`. Hence the Photo URL field and photos folder.
- Google's iCal feed folds lines at 75 characters, so a naive regex sees email domains such as
  `palisaderesearch.o`. Unfold (`\r?\n` followed by a space) before parsing.
- Many Google attendees have no `CN` name in the feed; matching is by email first for that reason.
- folk's PATCH replaces the `groups` list, so adding a person to the group must resend every
  group they already belong to (`folk_api.update_person` does).
- folk rate limit is 20 requests per 2 seconds; the client sleeps on 429. `all_people()` pages
  through the whole workspace once per `flag` run.
- `/usr/bin/python3` under launchd cannot read `~/Documents`; the plist uses the project `.venv`
  (built on Homebrew python), which can.
- In-process Agent SDK tools return MCP content blocks; an image is
  `{"type": "image", "data": <base64>, "mimeType": "image/jpeg"}` and the model really sees it.
  Keep images under about 3.5 MB (the API limit is 5 MB after decoding).
- `max_budget_usd` on the agent is an estimate that can overshoot slightly; the default $1 per
  person is generous, the trial cost $0.14.
