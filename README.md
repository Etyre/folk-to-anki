# folk-to-anki

Tags, in Palisade's folk CRM, the people Eli has met or is about to meet (read from his Google
Calendar), and builds Anki cards for them from his networking templates: faces, states, parties,
offices, committee chairs. Built 2026-11-07 to replace hand-making these cards in Roam. The
existing Roam-made cards in the two networking decks are left exactly as they are; this tool only
touches notes it created itself.

Since 2026-11-16 the meeting fields are team-wide rather than Eli's own: **Met with** and
**Meeting planned with** list which Palisade people have met or will meet a contact, so the whole
team can track coverage and other sources (colleagues by hand, the Slack/email tracker) can fill
in the same fields. This tool only ever adds or removes `MY_NAME`.

## How it works

1. **Calendar → folk (`flag`).** Reads the Google Calendar "secret iCal" feeds (the same URLs the
   executive assistant uses), unfolds the wrapped lines, and collects every attendee or organizer
   on a non-cancelled event with at most `MAX_ATTENDEES` people, skipping Eli's own addresses,
   colleague domains, declined invites and room/shared-calendar addresses. Each external person
   gets a *last met* date (latest past event) and *next meeting* date (earliest event within
   `UPCOMING_DAYS`). People are matched to folk by email, then by normalized full name.
   folk has no workspace-wide columns: custom fields belong to a group. So the four shared fields
   exist under the same names on every group in `FOLK_GROUPS`, and a match is written in whichever
   of those groups the person is already in (people are never moved or added to groups):
   - **Met with** / **Meeting planned with** (multi-select of first names): `MY_NAME` is added when
     the calendar has a past / upcoming meeting, and removed from Meeting planned with when there
     is no upcoming one. Other names are never touched.
   - **Removing yourself by hand sticks.** An invite is not proof of a meeting. If you remove
     `MY_NAME` from Met with in folk, the tool sees that it had already tagged that meeting
     (`met_written` in `state.json`) and does not add you back; a later meeting tags again.
     The person's cards already in Anki are not deleted for you.
   - **Last met** only moves forward. **Next meeting** takes the earlier of the existing future
     date and mine, and is cleared once it is in the past (`meeting_values` in `folk_to_anki.py`).

   Matches that are in folk but in no tracked group are logged and left alone. Calendar people
   with no folk record are listed in `not-in-folk.txt` for Eli to add by hand; the tool never
   creates folk people.
2. **folk → Anki (`cards`).** Takes every person in a tracked group who has `MY_NAME` in Met with
   or Meeting planned with (both get cards), or whose **Anki card** field names a type, unless
   Anki card is `Skip`. Colleagues' contacts are not carded. Card type is the Anki card
   field, else inferred: name matches a current member of Congress → Congressperson; member of
   `FOLK_DC_GROUP` or a `senate.gov`/`house.gov` email → DC staffer; otherwise Networking.
   Anki card, Party, Office, Deal and Photo URL are optional fields: used on groups that have
   them (`setup --card-fields` creates them), ignored elsewhere.
3. Facts come from folk (fields **Party**, **Office** falling back to the primary company,
   **Deal** falling back to description then "job title at company"; a Networking person with neither Deal nor description is warned about in the log, since the fallback is thin for independents, **Photo URL**) and, for
   Congress, from the public `@unitedstates` datasets: state, party, chamber, chair / ranking
   member of full committees, and the official portrait. Photo order: Congress portrait, folk
   Photo URL, `PHOTOS_DIR/<folk id>.jpg`, the picture folk attached to the person in a past
   calendar or email interaction, and finally a web search by a Claude agent (next point). No
   photo means no face card, with a warning in the log.
4. **Photo agent** (`photo_finder.py`, Claude Agent SDK, model `claude-opus-5-5`). It runs through
   the Claude Code login on this Mac, so there is no API key to manage. The face card is the card
   that matters most, so the agent is told to search thoroughly and may spend up to
   `PHOTO_SEARCH_BUDGET_USD` ($5) and 120 turns per person. The agent has WebSearch, WebFetch,
   and four in-process tools: `page_images` (lists a page's images with alt text), `image_search`
   (Bing image results: image URL, title, source page), `view_image`
   (shows it the picture so it can check the face is the right person) and the shared
   `contact_eli`. It answers with a structured `{url, source_page, confidence, reason}`; low
   confidence is discarded, the URL is downloaded to prove it is an image, and a hit is kept in
   `state.json` and, where the person's group has a **Photo URL** field, written to folk so it is
   visible and overridable in the CRM.
   Each person is searched once; a miss is retried after `PHOTO_SEARCH_RETRY_DAYS`, or at once
   with `cards --retry-photos`. A folk record with no name is identified by the agent from the
   organisation and the start of the email address; the name is kept in `state.json` and used on
   the cards (folk itself is not changed). A nameless person who cannot be identified gets no cards. The system
   prompt starts with the shared preamble; a `contact_eli` call or `@ELI:` line is recorded in
   the LLM inbox and stops the run (exit code 2).
5. `cards.py` turns a person into notes per template (see the docstring there). Every note has a
   `Key` field `<folk person id>|<kind>`; `anki.py` searches Anki for that key and adds, updates
   or leaves the note alone. Photos go into Anki's media folder as `folk-<id>.jpg`. Note types
   `Folk Basic` and `Folk Cloze` and the decks are created on first run. Nothing is ever deleted.
   **Eli's own cards win** (2026-11-17): before adding a note, `anki.py` searches the whole
   collection for a note of any other type that already asks the same thing ("Name: X" on a
   back, "X's state?", "X's office?", the committee in a chair question, also under the member's
   short name such as "Don Beyer"). If one exists the note is counted as `skipped`: his card
   keeps its review history and is never touched or doubled. A chair question is only skipped
   when his card names the same person, since his may predate an election.
   **Face first** (2026-11-17): a person's other cards (party, state, office, roles, committees)
   are suspended and tagged `folk-waiting-for-face` until their names-and-faces card, the tool's
   or Eli's own, has graduated to a review card; the next run then unsuspends them. Only cards
   never studied are held, and only cards the tool held are released. Someone with no face card
   at all gets their other cards at once. The release happens on a run, so it needs the daily
   schedule (or a manual `cards`).
   **Posts** (2026-11-17, Eli's card style): each leadership post or committee chair / ranking
   member makes two notes. A cloze that blanks only the post, at the end of the sentence ("Maria
   Cantwell is the chair of [Senate Commerce]."), and a basic card asking the name ("Chair of
   Senate Commerce:" -> name and photo). Committees go by their spoken short name (`SHORT` in
   `cards.py`). A person with two posts of one kind has the other spelled out on each card.
   **No prompt may have two right answers** (Eli's rule, 2026-11-17). That is why a second post
   is spelled out, why leadership titles must carry chamber and party (the roles agent is told
   so), and why a placeholder title containing "unconfirmed" gets no card. Asking the same fact
   in two formats is fine and intended.
   **What Claude writes to folk is flagged** (2026-11-17). Besides the meeting fields, the tool
   writes two things folk lacked: the name of a nameless record the photo agent identified with
   high confidence, and a missing job title looked up by `title_finder.py` (one agent session for
   the whole batch; high or medium confidence only; each person asked once, again after 60 days;
   `TITLES_AGENT=off` stops it). Every such write adds a note on the person's folk page that
   starts "Added by Eli's Claude (folk-to-anki) on <date>:" and gives the source, so the team can
   tell it from what a person entered. What was written is in `state.json` under `folk_writes`
   and `title_search`.
   **A name answer comes with a face** (2026-11-17): role and committee cards show the member's
   photo on the answer side, and a staffer's office card shows the boss (`folk-member-<bioguide>.jpg`).
   A role is "x at y", never just the organisation: "Role: <job title or Staffer> for <member>"
   for a member's office, "Role: <job title> at <organisation>" elsewhere, and "Staffer for
   <office>" for a Hill or White House staffer with no job title, as on Eli's own Roam cards. A
   Networking person with no job title, Deal or description in folk gets "Organization: <company>";
   fill in the job title in folk to turn that into a role.
6. **Top members of Congress** (`top_members.py`, added 2026-11-17). Independently of folk, the
   top `SENATORS_TOTAL` (50) senators and `REPS_TOTAL` (25) House members get Congressperson cards,
   `SENATORS_PER_DAY` / `REPS_PER_DAY` (2 each) new ones per day in order of importance; a missed
   day is not caught up in a burst. "Importance" is importance to Palisade's AI policy work, scored
   from the same public data: floor leadership (speaker 150, leaders 100, whips 60, president pro
   tem 30, other posts 20), then chair / ranking member / member of the committees AI runs through
   (50 / 40 / 8: Senate Commerce, Armed Services, Intelligence, Judiciary, Appropriations; House
   Energy and Commerce, Science, Armed Services, Intelligence, Judiciary, Appropriations, China
   select), of the second tier (30 / 25 / 3; see `CHAMBERS` in the file), and chair / ranking
   member of any other committee (12 / 10). Names in `member-priority.txt` go first, in the order
   written. Who has been released, and when, is in `state.json` under `senators` and `reps`;
   released members are refreshed on every run, so a new post updates the cards. Note keys are
   `sen-<bioguide>|<kind>` and `rep-<bioguide>|<kind>`. A member already carded through folk is
   not carded twice.

   **Before a new Congress convenes the cards are for the incoming one.** The public data only
   describes the sitting Congress, so `next_congress.json` (in this folder, filled in by hand from
   election results) lists who is departing, which House members move up to the Senate, whether a
   chamber flipped, and the leadership posts known when it was written. While it applies:
   departing members are never released; a flipped chamber ranks today's ranking members as the
   likely chairs; sitting-Congress committee chairs are **not carded**, because chairs are only
   settled as the new Congress organizes; a post ending in ` (expected)` counts for the ranking but
   gets no card until it is confirmed. Nothing is released while `verified` is empty.

   **Roles update on their own** (`roles_agent.py`, Claude Agent SDK, model `claude-opus-5-5`).
   While the file applies, `run` asks a second agent every `ROLES_CHECK_DAYS` (2) to search the
   news for who now holds each leadership post and who chairs or is ranking member of the
   AI-relevant committees in the incoming Congress. Each post comes back as *confirmed* (the
   vote or official announcement has happened) or *expected*, with a source URL, and is merged
   into `congress-roles.json`, which is laid over `next_congress.json`. Confirmed posts become
   cards (including chair cards, before January); expected ones only move the ranking. Every
   change is logged with a `roles:` prefix. The file stops applying once the new Congress has
   convened **and** the public data has caught up (no departed member left in it or on a
   committee roster), at most 120 days after the start; from then on the cards follow the data,
   which refreshes daily, so roles keep updating without the agent.

   People who were never in Congress before (most senators-elect)
   cannot be carded until the data includes them in January.
7. `run --daily` does all of this and records a success stamp so the later launchd slots are no-ops.

## Files

| File | Purpose |
|------|---------|
| `folk_to_anki.py` | CLI and orchestration: `setup`, `flag`, `cards`, `run`, `test` |
| `folk_api.py` | folk REST client; `shared_fields` and `CARD_FIELDS` define the custom fields the tool creates |
| `calendar_feed.py` | iCal fetch, minimal VEVENT parse, met / upcoming contact extraction |
| `congress.py` | Congress name matching, facts and photo from `@unitedstates` data (cached daily) |
| `cards.py` | the templates as code: person → notes |
| `top_members.py` | Senate and House importance rankings and the N-per-day release |
| `title_finder.py` | Claude agent that looks up missing job titles for the whole batch in one session |
| `roles_agent.py` | Claude agent that finds the incoming Congress's leadership and committee leaders |
| `next_congress.json` | who sits in the incoming Congress and its leadership, until it convenes |
| `photo_finder.py` | Claude agent that finds a headshot; `photo_finder.py "Name" "context"` tries it by hand |
| `anki.py` | AnkiConnect: note types, decks, media, add-or-update by key |
| `config.py` | reads `config.env` |
| `.venv/` | Python with `anthropic` and `claude-agent-sdk` (untracked; see Common tasks) |
| `tests/fixtures/` | fake folk people and a fake calendar for `test` |
| `com.elityre.folk-to-anki.plist`, `install.sh` | launchd agent (07:30, 12:30, 18:30) and installer |

## Runtime locations

- Config: `config.env` in this folder (untracked; template `config.env.example`).
- State: `~/Library/Application Support/FolkToAnki/state.json` (uploaded photos, photo searches, last success),
  `not-in-folk.txt` (calendar contacts without a folk record), `congress-roles.json` (what the roles agent found, with sources),
  `member-priority.txt` (optional,
  senators and representatives to put first, one name per line), `congress/` (data cache),
  `photos/` (manual photos named `<folk person id>.jpg`).
- Log: `~/Library/Logs/folk-to-anki.log` (launchd's own stdout in `folk-to-anki.launchd.log`).
- Installed agent: `~/Library/LaunchAgents/com.elityre.folk-to-anki.plist`.

## Common tasks

```bash
cd ~/Documents/agent-coding/folk-to-anki
/opt/homebrew/bin/python3 -m venv .venv && .venv/bin/pip install claude-agent-sdk   # once
cp config.env.example config.env            # then fill in FOLK_API_KEY and CALENDAR_ICS_URLS
./folk_to_anki.py setup                     # list folk groups, show which fields each tracked group lacks
./folk_to_anki.py setup --create-fields     # create the four shared fields on every group in FOLK_GROUPS
./folk_to_anki.py setup --card-fields       # also create the optional card fields there
./folk_to_anki.py flag --dry-run            # what the calendar would change in folk (works before the fields exist)
./folk_to_anki.py cards --dry-run           # print the cards that would be made
./folk_to_anki.py members                   # the Senate and House rankings with reasons and release dates
./folk_to_anki.py roles                     # ask the roles agent now (normally every ROLES_CHECK_DAYS inside run)
./folk_to_anki.py cards --retry-photos      # search again now for everyone without a photo, then update the cards
./folk_to_anki.py run                       # do it all for real
./folk_to_anki.py test                      # end to end with fixtures into deck "Folk to Anki test"
./folk_to_anki.py test --cleanup            # delete that test deck
.venv/bin/python photo_finder.py "Jane Doe" "legislative assistant, Office of Sen. X"   # try the photo agent
./install.sh                                # install / restart the launchd agent
launchctl bootout gui/$(id -u)/com.elityre.folk-to-anki   # stop it
tail -f ~/Library/Logs/folk-to-anki.log
```

`PHOTO_SEARCH=off` in `config.env` disables the agent; `cards --dry-run` never runs it.

Hand-picking people: add your name to **Met with** in folk, or (on a group that has the optional
field) set **Anki card** to `Congressperson`, `DC staffer` or `Networking`
to card someone the calendar never saw, or `Skip` to never card them. **Photo URL** (any
direct image link) overrides the photo, since folk's API exposes no profile pictures.

## Settings that matter

- Anki must be open with the AnkiConnect add-on (port 8765); the Roam import relies on the same.
- folk API access needs the Premium plan; the key is made under Settings → API keys. The key
  acts as Eli in the shared Palisade workspace, so field writes are visible to colleagues.
- Google Calendar → Settings → the calendar → "Secret address in iCal format" for each feed.
- The photo agent needs the `claude` CLI (`~/.local/bin/claude`) logged in; its usage is billed to
  that Claude account. The launchd plist puts `~/.local/bin` on PATH for this reason.

## Gotchas learned

- **A replaced photo needs a new filename.** Anki keeps showing its cached copy of an image for
  as long as the filename is the same, even after the media file is overwritten (seen
  2026-11-19: a 792 px file was in the media folder and the card still showed the 200 px one).
  So the first photo is `folk-<id>.jpg` and any replacement is `folk-<id>-<hash>.jpg`
  (`photo_files` in `state.json`); editing a local override in `photos/` counts as a replacement.
- The folk person object has no picture field (checked 2026-11-07 against the API reference);
  only interaction participants carry a `picture`. Hence the Photo URL field and photos folder.
- Google's iCal feed folds lines at 75 characters, so a naive regex sees email domains such as
  `palisaderesearch.o`. Unfold (`\r?\n` followed by a space) before parsing.
- Many Google attendees have no `CN` name in the feed; matching is by email first for that reason.
- folk custom fields are per group, not per workspace (checked 2026-11-16): a "general column" is
  the same field name created on each group. Values come back keyed by group id; multi-selects
  are lists of labels, dates are `YYYY-MM-DD` strings, user fields are lists of user objects.
- The people filter by group is `filter[groups][in][id]=<group id>`; `filter[groups][in]=<id>`
  returns HTTP 422. The tool now pages through everyone and filters in Python instead.
- folk's PATCH replaces the `groups` list if one is sent. The tool sends only
  `customFieldValues`, so it cannot change anyone's group membership.
- One folk person can appear in the calendar under two email addresses. `flag` combines those
  before writing; handling them one at a time made Last met flip on every run (2026-11-16).
- A PATCH with only `customFieldValues: {group id: {field: value}}` merges: other fields in the
  group, other groups' values and group membership are untouched (verified 2026-11-16).
- Role facts on cards come from an LLM reading the news while the incoming-Congress file
  applies. The guard is that only posts it marks confirmed, with a source, are carded; to correct
  one by hand, edit `congress-roles.json` (the agent's next answer for that person or committee
  post overwrites it). Departures and House-to-Senate moves in `next_congress.json` are not
  re-checked by the agent; late-called races (Alaska Senate, a few House seats) need a hand edit.
- The fixture test used to save its state over the real `state.json` (found 2026-11-17 when it
  marked two senators as released). It now points `STATE` at a temporary file.
- Between 00:00 and 06:00 nightblock cuts the network: folk, the photo agent and photo downloads all
  fail ("Connection refused" / "No route to host"), and `test` fails its card count because the
  fixture photos cannot be fetched. Not a bug; run it in the day.
- Without a photo a Networking person gets no cards at all and a DC staffer only office/party
  cards, so the photo search (or a Photo URL) decides most of the output.
- folk's `/interactions` needs `timeframe`; it answers, but the participants carried no pictures
  for anyone in the first real run, so that photo source is effectively empty.
- folk rate limit is 20 requests per 2 seconds; the client sleeps on 429. `all_people()` pages
  through the whole workspace once per `flag` run.
- `/usr/bin/python3` under launchd cannot read `~/Documents`; the plist uses the project `.venv`
  (built on Homebrew python), which can.
- In-process Agent SDK tools return MCP content blocks; an image is
  `{"type": "image", "data": <base64>, "mimeType": "image/jpeg"}` and the model really sees it.
  Keep images under about 3.5 MB (the API limit is 5 MB after decoding).
- The Agent SDK reads the CLI's output line by line with a 1 MB cap (`max_buffer_size`). A photo
  returned by `view_image` is bigger than that as base64 and raised `CLIJSONDecodeError` on the
  first real search (2026-11-17); the option is now 16 MB, and a crashed search is retried next run.
- First real batch (2026-11-17): 56 people searched for $7.42 in about 50 minutes, 38 photos
  found, 18 not (1 of those rejected as low confidence). The roles agent's first run recorded 53 posts.
- In the first batch every miss stopped by choice after at most 26 turns and $0.38, far under the
  budget: 6 were folk records with no name, the other 12 mostly Hill staffers whose only photos are
  on LinkedIn or behind LegiStorm's paywall. Hence the more persistent instructions and
  `image_search` (2026-11-17). `image_search` parses Bing's HTML (`class="iusc"` anchors with a JSON
  `m` attribute); if it starts returning "no image results" for everything, that markup changed.
- Retry with the stronger agent (2026-11-17): 11 of the 18 found, about $9.80 in all. Five of the
  six nameless folk records were identified from their email and organisation (the sixth already
  shows its email as its name in folk and was matched by initials). Still missing: 7 Hill and
  White House staffers whose only photos are on LinkedIn or behind LegiStorm's paywall.
- A Claude session sometimes does not start within the SDK's 60 s ("Control request timeout:
  initialize"); on 2026-11-17 this hit 16 searches in a row for unknown reasons, while a test
  session an hour later started in 1.3 s. `Finder.find` now waits and starts again, up to three
  more times.
- A long `cards` run can outlast the Mac staying awake, and Anki may be closed by the time it
  writes. Run long manual batches under `caffeinate -i` and keep Anki open.
- The photo agent's pick goes on the card uncropped. Early picks included an over-the-shoulder
  graduation shot and a two-person announcement graphic it meant to have cropped (2026-11-17);
  the instructions now ask for a face toward the camera and rule out pictures with a second
  person. To replace a bad photo by hand, put a file named `<folk person id>.jpg` in the photos
  folder: it wins over the agent's pick (but not over a Photo URL set in folk).
- The fixture test's people can match Eli's real cards (Don Beyer), so some of its notes come back
  `skipped`; the test counts those as present.
- LinkedIn batch by hand (2026-11-17, through Eli's logged-in Chrome with Claude in Chrome): of the
  7 still missing, 2 had a usable profile photo (saved as `<folk person id>.jpg` in the photos
  folder), 2 have a profile with no photo, 3 have no findable profile. To get the full square
  picture rather than the circle LinkedIn displays, open the image itself (it is 400x400) and save
  that. Chrome allows one script-started download per site and silently blocks the next ones.
- `max_budget_usd` on the agent is an estimate that can overshoot slightly; the default $1 per
  person is generous, the trial cost $0.14.
- The shared confirm step for `contact_eli` (`llm_contact.with_confirm`, see `../shared/README.md`) is not used
  here (checked 2026-11-10). It needs a second request to the same model with a forced tool choice, and the photo
  agent runs inside a Claude Agent SDK session: `ClaudeAgentOptions` has no `tool_choice`, the session's message
  list is not available to replay, and there is no API key for a direct call. A `contact_eli` call is therefore
  always sent to Eli at once, which is the safe default.
