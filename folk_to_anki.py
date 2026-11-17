#!/Users/elityre/Documents/agent-coding/folk-to-anki/.venv/bin/python
"""folk-to-anki: tag in folk (Palisade's CRM) the people Eli has met or is about to meet, read
from Google Calendar, in team-wide "Met with" / "Meeting planned with" fields, then build Anki
cards for those people from his networking templates.

    folk_to_anki.py setup [--create-fields] [--card-fields]   list groups, check/create the fields
    folk_to_anki.py flag  [--dry-run]         calendar -> folk "Met with" / "Meeting planned with" / dates
    folk_to_anki.py cards [--dry-run]         folk -> Anki notes
    folk_to_anki.py run   [--dry-run] [--daily]   flag then cards (--daily: once per day)
    folk_to_anki.py members                   the Senate and House rankings, with the date each member was released
    folk_to_anki.py roles                     ask the roles agent now who holds which post in the incoming Congress
    folk_to_anki.py test  [--cleanup]         end to end with fixtures, into a test deck

Config: config.env next to this file (see config.env.example). State and caches:
~/Library/Application Support/FolkToAnki/. Log: ~/Library/Logs/folk-to-anki.log.
"""
import argparse
import datetime as dt
import json
import logging
import sys
import tempfile
import urllib.request
from pathlib import Path

import anki as anki_mod
import calendar_feed
import cards
import config
import congress as congress_mod
import folk_api
import photo_finder
import roles_agent
import top_members

log = logging.getLogger("folk-to-anki")
STATE = config.APP_SUPPORT / "state.json"
REPORT = config.APP_SUPPORT / "not-in-folk.txt"
PRIORITY = config.APP_SUPPORT / "member-priority.txt"
NEXT_CONGRESS = config.PROJECT_DIR / "next_congress.json"
ROLES = config.APP_SUPPORT / "congress-roles.json"
TEST_DECK = "Folk to Anki test"
# chamber -> (state.json key, config prefix)
CHAMBER_KEYS = {"senate": ("senators", "SENATORS"), "house": ("reps", "REPS")}
STAFF_DOMAINS = (".senate.gov", ".house.gov", "mail.house.gov")


def load_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1, sort_keys=True))


# --- step 1: calendar -> folk ------------------------------------------------------------------
def calendar_contacts(cfg):
    urls = config.csv(cfg["CALENDAR_ICS_URLS"])
    if not urls:
        raise SystemExit("CALENDAR_ICS_URLS is empty in config.env")
    events = []
    for u in urls:
        try:
            evs = calendar_feed.parse_events(calendar_feed.fetch(u))
        except OSError as e:
            log.error("calendar feed failed (%s); continuing with the others", e)
            continue
        log.info("calendar feed: %d events", len(evs))
        events += evs
    return calendar_feed.contacts(events, config.csv(cfg["MY_EMAILS"]), config.csv(cfg["IGNORE_EMAIL_DOMAINS"]),
                                  int(cfg["MAX_ATTENDEES"]), int(cfg["UPCOMING_DAYS"]), int(cfg["PAST_DAYS"]))


def tracked_groups(cfg, folk):
    """-> {group id: {"name", "fields": set of custom field names}} for every group in FOLK_GROUPS."""
    by_name = {g["name"].strip().lower(): g for g in folk.groups()}
    out = {}
    for n in config.csv(cfg["FOLK_GROUPS"]):
        g = by_name.get(n.lower())
        if not g:
            raise SystemExit(f"folk group {n!r} (FOLK_GROUPS) not found; groups are: "
                             + ", ".join(x["name"] for x in by_name.values()))
        out[g["id"]] = {"name": g["name"], "fields": {f["name"] for f in folk.group_fields(g["id"])}}
    return out


def groups_of(person, groups):
    return [g["id"] for g in person.get("groups") or [] if g["id"] in groups]


def merged_fields(person, groups):
    """One view of a person's values across their tracked groups: lists are unioned, anything
    else is the first non-empty value."""
    out = {}
    for gid in groups_of(person, groups):
        for k, v in folk_api.field_values(person, gid).items():
            if isinstance(v, list):
                out[k] = sorted(set(out.get(k) or []) | {x for x in v if isinstance(x, str)})
            elif v not in (None, "") and not out.get(k):
                out[k] = v
    return out


def meeting_values(have, last_met, next_meeting, me, today):
    """The four shared fields after adding what my calendar knows to what is already in folk.
    Other people's names are never touched: the fields belong to the whole team, and other
    sources (colleagues, the Slack/email tracker) write to them too."""
    met = set(have.get(folk_api.MET_WITH) or [])
    planned = set(have.get(folk_api.PLANNED_WITH) or [])
    was_planned = me in planned
    if last_met:
        met.add(me)
    (planned.add if next_meeting else planned.discard)(me)
    last = max([d for d in (have.get(folk_api.LAST_MET), last_met) if d], default=None)
    nxt = have.get(folk_api.NEXT_MEETING) or None
    if nxt and nxt < today:
        nxt = None                                  # that meeting has happened
    if next_meeting:
        nxt = min(nxt, next_meeting) if nxt else next_meeting
    elif was_planned and not planned:
        nxt = None                                  # mine was the only planned meeting and it is gone
    return {folk_api.MET_WITH: sorted(met), folk_api.PLANNED_WITH: sorted(planned),
            folk_api.LAST_MET: last, folk_api.NEXT_MEETING: nxt}


def flag(cfg, folk, dry_run):
    contacts = calendar_contacts(cfg)
    log.info("calendar: %d external people on meetings", len(contacts))
    groups = tracked_groups(cfg, folk)
    need = [f["name"] for f in folk_api.shared_fields([])]
    lacking = {g["name"]: [n for n in need if n not in g["fields"]] for g in groups.values()}
    lacking = {k: v for k, v in lacking.items() if v}
    if lacking and dry_run:
        log.warning("shared fields not created yet on %s; showing what would be set once they are", list(lacking))
    elif lacking:
        raise SystemExit(f"folk groups lack shared fields {lacking}; run: folk_to_anki.py setup --create-fields")
    me, today = cfg["MY_NAME"], dt.date.today().isoformat()
    people = folk.all_people()
    by_email, by_name = {}, {}
    for p in people:
        for e in p.get("emails") or []:
            by_email.setdefault(e.lower(), p)
        n = congress_mod.norm(p.get("fullName", ""))
        if n:
            by_name.setdefault(n, p)
    changed, unmatched, outside = 0, [], []
    # One folk person can be several calendar contacts (two email addresses), so combine them
    # first: otherwise each address overwrites the other's dates and every run changes something.
    matched = {}
    for c in contacts.values():
        p = by_email.get(c["email"]) or (by_name.get(congress_mod.norm(c["name"])) if c["name"] else None)
        if not p:
            unmatched.append(c)
            continue
        m = matched.setdefault(p["id"], {"p": p, "last": None, "next": None})
        m["last"] = max([d for d in (m["last"], c["last_met"]) if d], default=None)
        m["next"] = min([d for d in (m["next"], c["next_meeting"]) if d], default=None)
    for m in matched.values():
        p = m["p"]
        gids = groups_of(p, groups)
        if not gids:
            outside.append(p.get("fullName") or p["id"])
            continue
        patch = {}
        for gid in gids:
            have = folk_api.field_values(p, gid)
            want = meeting_values(have, m["last"].isoformat() if m["last"] else None,
                                  m["next"].isoformat() if m["next"] else None, me, today)
            diff = {k: v for k, v in want.items()
                    if (sorted(have.get(k) or []) if isinstance(v, list) else have.get(k) or None) != v}
            if diff:
                patch[gid] = diff
        if not patch:
            continue
        changed += 1
        log.info("%s %s -> %s", "would set" if dry_run else "set", p["fullName"],
                 {groups[g]["name"]: d for g, d in patch.items()})
        if not dry_run:
            folk.update_person(p, patch)
    if outside:
        log.info("flag: %d calendar people are in folk but in no tracked group (FOLK_GROUPS), left alone: %s",
                 len(outside), ", ".join(sorted(outside)))
    unmatched.sort(key=lambda c: (c["next_meeting"] or dt.date.min, c["last_met"] or dt.date.min), reverse=True)
    lines = [f"{c['name'] or '?'} <{c['email']}>  last met {c['last_met']}  next {c['next_meeting']}  e.g. {c['events'][-1]}"
             for c in unmatched]
    if not dry_run:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(f"Calendar contacts with no folk record, {dt.date.today()}\n\n" + "\n".join(lines) + "\n")
    log.info("flag: %d folk people %s, %d calendar contacts not in folk (list in %s)",
             changed, "would change" if dry_run else "changed", len(unmatched), REPORT)
    return changed, unmatched


# --- step 2: folk -> Anki ----------------------------------------------------------------------
def gather(cfg, folk, congress, state=None, finder=None):
    """-> list of plain person dicts that cards.build understands: everyone in a tracked group whom
    I have met or plan to meet, plus anyone whose Anki card field names a type."""
    state = state if state is not None else {}
    groups = tracked_groups(cfg, folk)
    dc_id = folk.group_by_name(cfg["FOLK_DC_GROUP"])["id"] if cfg["FOLK_DC_GROUP"] else None
    me = cfg["MY_NAME"]
    out = []
    for p in folk.all_people():
        if not groups_of(p, groups):
            continue
        f = merged_fields(p, groups)
        card = f.get("Anki card")
        mine = me in (f.get(folk_api.MET_WITH) or []) or me in (f.get(folk_api.PLANNED_WITH) or [])
        if card == "Skip" or not (card or mine):
            continue
        name = p.get("fullName") or f"{p.get('firstName', '')} {p.get('lastName', '')}".strip()
        emails = [e.lower() for e in p.get("emails") or []]
        leg = congress.match(name)
        kind = card
        if not kind:
            if leg:
                kind = "Congressperson"
            elif any(g["id"] == dc_id for g in p.get("groups") or []) \
                    or any(e.endswith(STAFF_DOMAINS) for e in emails):
                kind = "DC staffer"
            else:
                kind = "Networking"
        company = (p.get("companies") or [{}])[0].get("name", "")
        rec = {"id": p["id"], "name": name, "kind": kind, "party": f.get("Party") or "",
               "office": f.get("Office") or company, "state": "", "chamber": "", "leadership": [],
               "deal": f.get("Deal") or p.get("description") or
                       " at ".join(x for x in (p.get("jobTitle"), company) if x),
               "photo_url": f.get("Photo URL") or None, "folk": p}
        # "Job title at company" is a last resort; for independents it is just a title or nothing,
        # so say so in the log and let Eli fill in the Deal field (or description) in folk.
        if kind == "Networking" and not (f.get("Deal") or p.get("description")):
            log.warning("%s: no Deal or description in folk; card back says %r", name, rec["deal"] or "(nothing)")
        if kind == "Congressperson":
            if not leg:
                log.warning("%s is marked Congressperson but is not in legislators-current; skipping", name)
                continue
            facts = congress.facts(leg)
            rec.update(bioguide=facts["bioguide"], roles=facts["roles"], name=facts["name"], party=facts["party"], state=facts["state"], chamber=facts["chamber"],
                       leadership=facts["leadership"], photo_url=rec["photo_url"] or facts["photo_url"])
        if not rec["photo_url"]:
            local = next((x for x in Path(cfg["PHOTOS_DIR"]).glob(p["id"] + ".*")), None)
            rec["photo_url"] = local.as_uri() if local else folk.participant_picture(p)
        if not rec["photo_url"] and finder is not None:
            rec["photo_url"] = search_photo(cfg, folk, finder, state, groups, rec)
        elif not rec["photo_url"]:                 # agent off or dry run: reuse what it found before
            rec["photo_url"] = (state.get("photo_search", {}).get(p["id"]) or {}).get("url")
        if not rec["photo_url"]:
            log.warning("no photo for %s (%s): add a Photo URL in folk or %s/%s.jpg; skipping the face card",
                        name, kind, cfg["PHOTOS_DIR"], p["id"])
        out.append(rec)
    return out


def search_photo(cfg, folk, finder, state, groups, rec):
    """Ask the photo agent once per person (re-asking after PHOTO_SEARCH_RETRY_DAYS when it found
    nothing). A verified hit is kept in state.json and, where the person's group has a Photo URL
    field, written to folk so it shows in the CRM and can be overridden there. Raises photo_finder.ContactRequested if the model wants to talk to Eli."""
    searched = state.setdefault("photo_search", {})
    prev = searched.get(rec["id"])
    today = dt.date.today()
    if prev and (prev.get("url") or (today - dt.date.fromisoformat(prev["date"])).days < int(cfg["PHOTO_SEARCH_RETRY_DAYS"])):
        return prev.get("url")
    p = rec["folk"]
    context = "; ".join(x for x in (
        f"card type {rec['kind']}", f"job title {p.get('jobTitle')}" if p.get("jobTitle") else "",
        f"organisation {rec['office']}" if rec.get("office") else "",
        f"party {rec['party']}" if rec.get("party") else "",
        f"email domain {p['emails'][0].split('@')[1]}" if p.get("emails") else "",
        f"notes: {p.get('description')}" if p.get("description") else "") if x)
    log.info("photo search: %s (%s)", rec["name"], context)
    try:
        res = finder.find(rec["name"], context)
    except photo_finder.ContactRequested:
        raise
    except Exception as e:      # one broken session must not end the run; not recorded, so retried next run
        log.error("photo search for %s crashed, will retry next run: %s", rec["name"], str(e)[:300])
        return None
    url = (res or {}).get("url")
    if url and res.get("confidence") == "low":
        log.info("photo search: low confidence for %s, not using %s (%s)", rec["name"], url, res.get("reason"))
        url = None
    if url and not photo_finder.verify_image(url):
        url = None
    searched[rec["id"]] = {"date": today.isoformat(), "url": url, "result": res}
    save_state(state)
    if url:
        log.info("photo search: %s -> %s (%s)", rec["name"], url, res.get("reason"))
        patch = {g: {"Photo URL": url} for g in groups_of(p, groups) if "Photo URL" in groups[g]["fields"]}
        try:
            if patch:
                folk.update_person(p, patch)
        except folk_api.FolkError as e:
            log.error("could not write Photo URL for %s to folk: %s", rec["name"], e)
    else:
        log.info("photo search: nothing usable for %s (%s)", rec["name"], (res or {}).get("reason"))
    return url


def full_name(leg):
    return leg["name"].get("official_full") or f"{leg['name']['first']} {leg['name']['last']}"


def update_roles(cfg, congress, force=False):
    """While the incoming-Congress file applies, ask the roles agent every ROLES_CHECK_DAYS who now
    holds which post, and merge its answer into congress-roles.json. Entries are kept per person
    and per committee post, so something the agent leaves out one day is not forgotten; a post
    that moved to someone else is taken off the previous holder."""
    nxt = top_members.load_next(NEXT_CONGRESS, congress, ROLES)
    if not nxt or not nxt.get("verified"):
        return
    try:
        found = json.loads(ROLES.read_text())
    except (OSError, ValueError):
        found = {}
    today = dt.date.today()
    if not force and (cfg["ROLES_AGENT"].lower() not in ("on", "true", "yes", "1") or (
            found.get("checked") and (today - dt.date.fromisoformat(found["checked"])).days < int(cfg["ROLES_CHECK_DAYS"]))):
        return
    on_file = [f"{ch}: {name}: {', '.join(titles)}" for ch in top_members.CHAMBERS
               for name, titles in ((nxt.get(ch) or {}).get("roles") or {}).items()]
    on_file += [f"{cid} {title}: {post['name']} ({post['status']})"
                for cid, posts in nxt["committees"].items() for title, post in posts.items()]
    wanted = [(cid, congress.committees[cid]["name"]) for ch in top_members.CHAMBERS.values()
              for cid in sorted(ch["A"] | ch["B"]) if cid in congress.committees]
    log.info("roles: asking the agent about the %sth Congress", nxt["congress"])
    res = roles_agent.research(nxt["congress"], nxt["starts"], "\n".join(on_file), wanted,
                               budget_usd=float(cfg["ROLES_AGENT_BUDGET_USD"]))
    if res is None:
        return
    changes = []
    lead = found.setdefault("leadership", {})
    for chamber in top_members.CHAMBERS:
        mine, by_name = lead.setdefault(chamber, {}), {}
        for post in res[chamber + "_leadership"]:
            if not congress.match(post["name"]):
                log.info("roles: %s (%s) is not in the Congress data yet; left out", post["name"], post["title"])
                continue
            by_name.setdefault(full_name(congress.match(post["name"])), []).append(
                {k: post[k] for k in ("title", "status", "source")})
        for name, items in by_name.items():
            titles = {i["title"] for i in items}
            for other in [o for o in mine if o != name]:          # a post that moved leaves its old holder
                mine[other] = [i for i in mine[other] if i["title"] not in titles]
            if mine.get(name) != items:
                changes.append(f"{name}: " + "; ".join(f"{i['title']} ({i['status']})" for i in items))
            mine[name] = items
        for empty in [o for o in mine if not mine[o]]:
            del mine[empty]
    coms = found.setdefault("committees", {})
    for post in res["committees"]:
        cid, leg = post["committee_id"], congress.match(post["name"])
        if cid not in congress.committees or not leg:
            log.info("roles: skipping %s %s for %s (unknown committee or person not in the data)",
                     cid, post["title"], post["name"])
            continue
        new = {"name": full_name(leg), "status": post["status"], "source": post["source"]}
        if coms.setdefault(cid, {}).get(post["title"]) != new:
            changes.append(f"{congress.committees[cid]['name']}: {post['title']} {new['name']} ({new['status']})")
        coms[cid][post["title"]] = new
    found.update(checked=today.isoformat(), notes=res.get("notes", ""))
    ROLES.parent.mkdir(parents=True, exist_ok=True)
    ROLES.write_text(json.dumps(found, indent=1, sort_keys=True))
    for c in changes:
        log.info("roles: %s", c)
    log.info("roles: %d changes; agent notes: %s", len(changes), res.get("notes", ""))


def member_ranking(congress, chamber, nxt=None):
    names = [x.strip() for x in PRIORITY.read_text().splitlines() if x.strip() and not x.startswith("#")] \
        if PRIORITY.exists() else []
    return top_members.rank(congress, chamber, names, nxt)


def member_people(cfg, congress, state, dry_run, skip=()):
    """The senators and representatives released so far (plus today's, at most <CHAMBER>_PER_DAY
    each) as card people. Members who left drop out of the data and are simply no longer updated.
    `skip` holds the bioguide ids already carded through folk, so nobody gets two sets of cards."""
    nxt = top_members.load_next(NEXT_CONGRESS, congress, ROLES)
    out = []
    for chamber, (key, conf) in CHAMBER_KEYS.items():
        total, per_day = int(cfg[conf + "_TOTAL"]), int(cfg[conf + "_PER_DAY"])
        if total <= 0:
            continue
        if nxt and not nxt.get("verified"):
            # Carding the sitting Congress weeks before it changes would teach people who are leaving.
            log.warning("%s: held until next_congress.json is filled in for the %sth Congress and marked verified",
                        key, nxt["congress"])
            continue
        ranked = member_ranking(congress, chamber, nxt)
        released = dict(state.get(key) or {}) if dry_run else state.setdefault(key, {})
        new = top_members.release([leg["id"]["bioguide"] for leg, _, _ in ranked], released,
                                  dt.date.today().isoformat(), per_day, total)
        by_id = {leg["id"]["bioguide"]: leg for leg, _, _ in ranked}
        if new:
            log.info("%s: %s %s (%d of %d so far)", key, "would release" if dry_run else "releasing",
                     ", ".join(full_name(by_id[b]) for b in new), len(released), total)
            if not dry_run:
                save_state(state)
        out += [top_members.person(congress, by_id[b], chamber, nxt) for b in released if b in by_id and b not in skip]
    return out


def fetch_photo(url):
    if url.startswith("file://"):
        return Path(urllib.request.url2pathname(url[7:])).read_bytes()
    req = urllib.request.Request(url, headers={"User-Agent": "folk-to-anki"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def make_cards(cfg, folk, congress, anki, decks, dry_run, state, finder=None):
    people = gather(cfg, folk, congress, state, finder)
    people += member_people(cfg, congress, state, dry_run, skip={p.get("bioguide") for p in people})
    log.info("cards: %d people to card", len(people))
    counts = {"added": 0, "updated": 0, "unchanged": 0, "failed": 0}
    if not dry_run:
        anki.ensure_setup(list(decks.values()))
    uploaded = state.setdefault("photos", {})
    for p in people:
        notes = cards.build(p, decks)
        if dry_run:
            print(f"\n== {p['name']} [{p['kind']}] photo={'yes' if p.get('photo_url') else 'NO'}")
            for n in notes:
                front = n["fields"].get("Front") or n["fields"].get("Text")
                back = n["fields"].get("Back") or n["fields"].get("Back Extra") or ""
                print(f"  [{n['deck']}] {front}  ->  {back}")
            continue
        try:
            if p.get("photo_url") and uploaded.get(p["id"]) != p["photo_url"]:
                anki.store_photo(f"folk-{p['id']}.jpg", fetch_photo(p["photo_url"]))
                uploaded[p["id"]] = p["photo_url"]
                save_state(state)
        except (OSError, anki_mod.AnkiError) as e:
            log.error("photo for %s failed: %s", p["name"], e)
            notes = [n for n in notes if not n["photo_url"]]
        for n in notes:
            try:
                counts[anki.upsert(n)] += 1
            except anki_mod.AnkiError as e:
                counts["failed"] += 1
                log.error("note %s failed: %s", n["key"], e)
    log.info("cards: %s", ", ".join(f"{v} {k}" for k, v in counts.items()))
    return counts


# --- test fixtures: stand-in for folk ------------------------------------------------------------
class FakeFolk:
    """Serves tests/fixtures/people.json and records writes instead of making them."""
    GROUP = {"id": "grp_test0000000000000000000000000000000", "name": "Networking"}

    def __init__(self):
        self.people = json.loads((config.PROJECT_DIR / "tests" / "fixtures" / "people.json").read_text())
        self.writes = []

    def groups(self):
        return [self.GROUP]

    def group_by_name(self, name):
        return self.GROUP

    def group_fields(self, group_id):
        return [{"name": f["name"]} for f in folk_api.shared_fields([]) + folk_api.CARD_FIELDS]

    def all_people(self):
        return self.people

    def update_person(self, person, values_by_group):
        self.writes.append((person["fullName"], values_by_group))
        for gid, values in values_by_group.items():
            person.setdefault("customFieldValues", {}).setdefault(gid, {}).update(values)

    def participant_picture(self, person):
        return None


def run_test(cfg, anki, cleanup):
    if cleanup:
        log.info("deleting deck %r and its cards", TEST_DECK)
        anki.call("deleteDecks", decks=[TEST_DECK], cardsToo=True)
        return
    global STATE
    STATE = Path(tempfile.mkdtemp(prefix="folk-to-anki-test-")) / "state.json"   # never the real state file
    folk = FakeFolk()
    cfg = dict(cfg, FOLK_GROUPS=FakeFolk.GROUP["name"], FOLK_DC_GROUP="", MY_NAME="Eli")
    fixtures = config.PROJECT_DIR / "tests" / "fixtures"
    events = calendar_feed.parse_events((fixtures / "calendar.ics").read_text())
    now = dt.datetime(2026, 11, 7, 12, tzinfo=dt.timezone.utc)
    contacts = calendar_feed.contacts(events, ["eli@example.org"], ["example.org"], 10, 90, 0, now=now)
    print("calendar contacts:", json.dumps(contacts, default=str, indent=1))
    assert "blumenthal@senate.example" in contacts and contacts["blumenthal@senate.example"]["last_met"]
    assert contacts["staffer@senate.example"]["next_meeting"] and not contacts["staffer@senate.example"]["last_met"]
    assert "bigtalk@elsewhere.example" not in contacts, "events over MAX_ATTENDEES must not count"
    assert "colleague@example.org" not in contacts, "ignored domain leaked"
    # shared fields: my name is added and removed, colleagues' names and later dates are kept
    M, P, L, N = folk_api.MET_WITH, folk_api.PLANNED_WITH, folk_api.LAST_MET, folk_api.NEXT_MEETING
    v = meeting_values({M: ["Dave"], L: "2026-12-01"}, "2026-11-01", "2026-11-20", "Eli", "2026-11-07")
    assert v == {M: ["Dave", "Eli"], P: ["Eli"], L: "2026-12-01", N: "2026-11-20"}, v
    v = meeting_values({P: ["Dave", "Eli"], N: "2026-11-20"}, None, None, "Eli", "2026-11-07")
    assert v == {M: [], P: ["Dave"], L: None, N: "2026-11-20"}, v
    v = meeting_values({P: ["Eli"], N: "2026-11-20"}, "2026-11-05", None, "Eli", "2026-11-07")
    assert v == {M: ["Eli"], P: [], L: "2026-11-05", N: None}, v
    congress = congress_mod.Congress(config.APP_SUPPORT / "congress")
    for chamber, least in (("senate", 50), ("house", 25)):
        ranked = member_ranking(congress, chamber)
        ids = [leg["id"]["bioguide"] for leg, _, _ in ranked]
        assert len(ids) == len(set(ids)) >= least and ranked[0][1] >= ranked[least - 1][1] > 0, chamber + " ranking"
        top = full_name(ranked[0][0])
        nxt = {"departing": [top], "from_house": [], chamber: {"flipped": True, "roles": {}}}
        incoming = top_members.rank(congress, chamber, (), nxt)
        assert top not in [full_name(l) for l, _, _ in incoming], "departing members are dropped"
        assert not top_members.person(congress, incoming[0][0], chamber, nxt)["leadership"], \
            "no chair cards before the new Congress"
    rep = full_name(member_ranking(congress, "house")[0][0])
    moved = top_members.rank(congress, "senate", (), {"from_house": [rep]})
    assert rep in [full_name(l) for l, _, _ in moved], "a House member moving up is a senator"
    assert rep not in [full_name(l) for l, _, _ in top_members.rank(congress, "house", (), {"from_house": [rep]})]
    assert top_members.load_next(NEXT_CONGRESS, congress, None, "2027-06-01") is None, "the file stops applying"
    nxt = {"departing": [], "committees": {"SSCM": {"chair": {"name": rep, "status": "expected"}}}}
    assert not top_members.committee_posts(congress, nxt)["SSCM"][congress.match(rep)["id"]["bioguide"]][1]
    rel = {}
    assert top_members.release(ids, rel, "2026-11-17", 2, 5) == ids[:2]
    assert top_members.release(ids, rel, "2026-11-17", 2, 5) == [], "at most two per day"
    assert top_members.release(ids, rel, "2026-11-18", 2, 5) == ids[2:4]
    assert top_members.release(ids, rel, "2026-11-19", 2, 5) == ids[4:5], "stops at the total"
    assert top_members.release(ids, rel, "2026-11-20", 2, 5) == []
    # the real release waits on next_congress.json; its logic is tested above
    cfg = dict(cfg, SENATORS_TOTAL="0", REPS_TOTAL="0")
    decks = {"dc": TEST_DECK + "::DC", "networking": TEST_DECK + "::Networking"}
    people = gather(cfg, folk, congress)
    kinds = {p["name"]: p["kind"] for p in people}
    print("kinds:", kinds)
    assert kinds.get("Richard Blumenthal") == "Congressperson" and kinds.get("Sam Staffer") == "DC staffer"
    assert kinds.get("Nina Networker") == "Networking" and "Skipped Person" not in kinds
    assert "Never Met" not in kinds and "Daves Contact" not in kinds, "only my own meetings get cards"
    state = {}
    counts = make_cards(cfg, folk, congress, anki, decks, dry_run=False, state=state)
    assert counts["failed"] == 0 and counts["added"] + counts["unchanged"] >= 8, counts
    again = make_cards(cfg, folk, congress, anki, decks, dry_run=False, state=state)
    assert again["added"] == 0 and again["updated"] == 0, f"second run must be a no-op: {again}"
    print(f"\nTEST PASSED. Cards are in Anki deck {TEST_DECK!r}; remove with: folk_to_anki.py test --cleanup")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["setup", "flag", "cards", "run", "members", "roles", "test"])
    ap.add_argument("--dry-run", action="store_true", help="print what would change; write nothing")
    ap.add_argument("--daily", action="store_true", help="exit at once if a run already succeeded today")
    ap.add_argument("--create-fields", action="store_true", help="setup: create the missing shared fields on every tracked group")
    ap.add_argument("--card-fields", action="store_true",
                    help="setup: also create the optional card fields (Anki card, Party, Office, Deal, Photo URL)")
    ap.add_argument("--cleanup", action="store_true", help="test: delete the test deck")
    ap.add_argument("--config", help="path to config.env")
    a = ap.parse_args(argv)

    config.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler(config.LOG_FILE)])
    cfg = config.load(a.config)
    anki = anki_mod.Anki(cfg["ANKICONNECT_URL"])
    state = load_state()
    today = dt.date.today().isoformat()

    if a.command == "test":
        return run_test(cfg, anki, a.cleanup)
    if a.command == "roles":
        return update_roles(cfg, congress_mod.Congress(config.APP_SUPPORT / "congress"), force=True)
    if a.command == "members":
        congress = congress_mod.Congress(config.APP_SUPPORT / "congress")
        nxt = top_members.load_next(NEXT_CONGRESS, congress, ROLES)
        if nxt:
            print(f"Incoming {nxt['congress']}th Congress (from {nxt['starts']}); next_congress.json verified: "
                  f"{nxt.get('verified') or 'NO, cards are held'}; departing listed: {len(nxt.get('departing') or [])}")
        for chamber, (key, conf) in CHAMBER_KEYS.items():
            released = state.get(key) or {}
            print(f"\n== {chamber.title()}: top {cfg[conf + '_TOTAL']}, {cfg[conf + '_PER_DAY']} per day")
            for i, (leg, score, why) in enumerate(member_ranking(congress, chamber, nxt)[:int(cfg[conf + "_TOTAL"])], 1):
                t = leg["terms"][-1]
                print(f"{i:2}. {full_name(leg)} ({t['party'][0]}-{t['state']})  score {score}  "
                      f"{released.get(leg['id']['bioguide'], '')}\n      {'; '.join(why)}")
        return
    if a.daily and state.get("last_success") == today:
        log.info("already ran today; nothing to do")
        return
    folk = folk_api.Folk(cfg["FOLK_API_KEY"])
    if a.command == "setup":
        groups = folk.groups()
        print("folk groups:", ", ".join(f"{g['name']} ({g['id']})" for g in groups))
        names = [cfg["MY_NAME"]] + [u["fullName"].split()[0] for u in folk.users()]
        names = [n for i, n in enumerate(names) if n not in names[:i] and n != "Claude"]
        for gid, g in tracked_groups(cfg, folk).items():
            shared = [f for f in folk_api.shared_fields(names) if f["name"] not in g["fields"]]
            card = [f for f in folk_api.CARD_FIELDS if f["name"] not in g["fields"]]
            print(f"group {g['name']!r}: missing shared fields {[f['name'] for f in shared] or 'none'}; "
                  f"missing optional card fields {[f['name'] for f in card] or 'none'}")
            for f in (shared if a.create_fields else []) + (card if a.card_fields else []):
                print("  creating", f["name"])
                folk.create_field(gid, f)
        return
    if a.command in ("flag", "run"):
        flag(cfg, folk, a.dry_run)
    if a.command in ("cards", "run"):
        congress = congress_mod.Congress(config.APP_SUPPORT / "congress")
        decks = {"dc": cfg["DECK_DC"], "networking": cfg["DECK_NETWORKING"]}
        if not a.dry_run:
            update_roles(cfg, congress)
        finder = None
        if cfg["PHOTO_SEARCH"].lower() in ("on", "true", "yes", "1") and not a.dry_run:
            finder = photo_finder.Finder(budget_usd=float(cfg["PHOTO_SEARCH_BUDGET_USD"]))
        counts = make_cards(cfg, folk, congress, anki, decks, a.dry_run, state, finder)
        if not a.dry_run and counts["failed"] == 0 and a.command == "run":
            state["last_success"] = today
            save_state(state)


if __name__ == "__main__":
    try:
        main()
    except photo_finder.ContactRequested as e:
        log.error("the photo agent asked to talk to Eli; stopping this run (see llm-inbox): %s", e)
        sys.exit(2)
    except (folk_api.FolkError, anki_mod.AnkiError, SystemExit) as e:
        if str(e):
            log.error("%s", e)
        sys.exit(1)
