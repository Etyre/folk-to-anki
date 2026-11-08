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
   **Deal** falling back to description then "job title at company", **Photo URL**) and, for
   Congress, from the public `@unitedstates` datasets: state, party, chamber, chair / ranking
   member of full committees, and the official portrait. Photo order: Congress portrait, folk
   Photo URL, `PHOTOS_DIR/<folk id>.jpg`, the picture folk attached to the person in a past
   calendar or email interaction. No photo means no face card, with a warning in the log.
4. `cards.py` turns a person into notes per template (see the docstring there). Every note has a
   `Key` field `<folk person id>|<kind>`; `anki.py` searches Anki for that key and adds, updates
   or leaves the note alone. Photos go into Anki's media folder as `folk-<id>.jpg`. Note types
   `Folk Basic` and `Folk Cloze` and the decks are created on first run. Nothing is ever deleted.
5. `run --daily` does both steps and records a success stamp so the later launchd slots are no-ops.

## Files

| File | Purpose |
|------|---------|
| `folk_to_anki.py` | CLI and orchestration: `setup`, `flag`, `cards`, `run`, `test` |
| `folk_api.py` | folk REST client; `OWNED_FIELDS` defines the custom fields the tool creates |
| `calendar_feed.py` | iCal fetch, minimal VEVENT parse, met / upcoming contact extraction |
| `congress.py` | Congress name matching, facts and photo from `@unitedstates` data (cached daily) |
| `cards.py` | the templates as code: person → notes |
| `anki.py` | AnkiConnect: note types, decks, media, add-or-update by key |
| `config.py` | reads `config.env` |
| `tests/fixtures/` | fake folk people and a fake calendar for `test` |
| `com.elityre.folk-to-anki.plist`, `install.sh` | launchd agent (07:30, 12:30, 18:30) and installer |

## Runtime locations

- Config: `config.env` in this folder (untracked; template `config.env.example`).
- State: `~/Library/Application Support/FolkToAnki/state.json` (uploaded photos, last success),
  `not-in-folk.txt` (calendar contacts without a folk record), `congress/` (data cache),
  `photos/` (manual photos named `<folk person id>.jpg`).
- Log: `~/Library/Logs/folk-to-anki.log` (launchd's own stdout in `folk-to-anki.launchd.log`).
- Installed agent: `~/Library/LaunchAgents/com.elityre.folk-to-anki.plist`.

## Common tasks

```bash
cd ~/Documents/agent-coding/folk-to-anki
cp config.env.example config.env            # then fill in FOLK_API_KEY and CALENDAR_ICS_URLS
./folk_to_anki.py setup                     # list folk groups, show which custom fields are missing
./folk_to_anki.py setup --create-fields     # create them on FOLK_GROUP
./folk_to_anki.py flag --dry-run            # what the calendar would change in folk
./folk_to_anki.py cards --dry-run           # print the cards that would be made
./folk_to_anki.py run                       # do both for real
./folk_to_anki.py test                      # end to end with fixtures into deck "Folk to Anki test"
./folk_to_anki.py test --cleanup            # delete that test deck
./install.sh                                # install / restart the launchd agent
launchctl bootout gui/$(id -u)/com.elityre.folk-to-anki   # stop it
tail -f ~/Library/Logs/folk-to-anki.log
```

Hand-picking people: set **Anki card** in folk to `Congressperson`, `DC staffer` or `Networking`
to card someone the calendar never saw, or `Skip` to never card them. Add **Photo URL** (any
direct image link) for staffers and networking contacts, since folk's API exposes no profile
pictures.

## Settings that matter

- Anki must be open with the AnkiConnect add-on (port 8765); the Roam import relies on the same.
- folk API access needs the Premium plan; the key is made under Settings → API keys. The key
  acts as Eli in the shared Palisade workspace, so field writes are visible to colleagues.
- Google Calendar → Settings → the calendar → "Secret address in iCal format" for each feed.

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
- `/usr/bin/python3` under launchd cannot read `~/Documents`; the plist uses Homebrew's python.
