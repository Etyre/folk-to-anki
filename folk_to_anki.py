#!/Users/elityre/Documents/agent-coding/folk-to-anki/.venv/bin/python
"""folk-to-anki: flag in folk (Palisade's CRM) the people Eli has met or is about to meet,
read from Google Calendar, then build Anki cards for them from his networking templates.

    folk_to_anki.py setup [--create-fields]   list folk groups, check/create the custom fields
    folk_to_anki.py flag  [--dry-run]         calendar -> folk "Meeting status" / dates
    folk_to_anki.py cards [--dry-run]         folk -> Anki notes
    folk_to_anki.py run   [--dry-run] [--daily]   flag then cards (--daily: once per day)
    folk_to_anki.py test  [--cleanup]         end to end with fixtures, into a test deck

Config: config.env next to this file (see config.env.example). State and caches:
~/Library/Application Support/FolkToAnki/. Log: ~/Library/Logs/folk-to-anki.log.
"""
import argparse
import datetime as dt
import json
import logging
import sys
import urllib.request
from pathlib import Path

import anki as anki_mod
import calendar_feed
import cards
import config
import congress as congress_mod
import folk_api
import photo_finder

log = logging.getLogger("folk-to-anki")
STATE = config.APP_SUPPORT / "state.json"
REPORT = config.APP_SUPPORT / "not-in-folk.txt"
TEST_DECK = "Folk to Anki test"
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


def flag(cfg, folk, dry_run):
    contacts = calendar_contacts(cfg)
    log.info("calendar: %d external people on meetings", len(contacts))
    group = folk.group_by_name(cfg["FOLK_GROUP"])
    missing = folk.missing_fields(group["id"])
    if missing:
        raise SystemExit("folk group %r lacks fields %s; run: folk_to_anki.py setup --create-fields"
                         % (group["name"], [f["name"] for f in missing]))
    people = folk.all_people()
    by_email, by_name = {}, {}
    for p in people:
        for e in p.get("emails") or []:
            by_email.setdefault(e.lower(), p)
        n = congress_mod.norm(p.get("fullName", ""))
        if n:
            by_name.setdefault(n, p)
    changed, unmatched = 0, []
    for c in contacts.values():
        p = by_email.get(c["email"]) or (by_name.get(congress_mod.norm(c["name"])) if c["name"] else None)
        if not p:
            unmatched.append(c)
            continue
        status = "Planning to meet" if c["next_meeting"] and not c["last_met"] else "Met"
        want = {"Meeting status": status,
                "Last met": c["last_met"].isoformat() if c["last_met"] else None,
                "Next meeting": c["next_meeting"].isoformat() if c["next_meeting"] else None}
        have = folk_api.field_values(p, group["id"])
        in_group = any(g["id"] == group["id"] for g in p.get("groups", []))
        diff = {k: v for k, v in want.items() if (have.get(k) or None) != v}
        if in_group and not diff:
            continue
        changed += 1
        log.info("%s %s -> %s%s", "would set" if dry_run else "set", p["fullName"], diff or want,
                 "" if in_group else f" (adding to group {group['name']})")
        if not dry_run:
            folk.update_person(p, group["id"], want)
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
    state = state if state is not None else {}
    """-> list of plain person dicts that cards.build understands."""
    group = folk.group_by_name(cfg["FOLK_GROUP"])
    dc_group = folk.group_by_name(cfg["FOLK_DC_GROUP"]) if cfg["FOLK_DC_GROUP"] else None
    out = []
    for p in folk.people_in_group(group["id"]):
        f = folk_api.field_values(p, group["id"])
        card = f.get("Anki card")
        if card == "Skip" or (not card and f.get("Meeting status") not in ("Met", "Planning to meet")):
            continue
        name = p.get("fullName") or f"{p.get('firstName', '')} {p.get('lastName', '')}".strip()
        emails = [e.lower() for e in p.get("emails") or []]
        leg = congress.match(name)
        kind = card
        if not kind:
            if leg:
                kind = "Congressperson"
            elif (dc_group and any(g["id"] == dc_group["id"] for g in p.get("groups", []))) \
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
        if kind == "Congressperson":
            if not leg:
                log.warning("%s is marked Congressperson but is not in legislators-current; skipping", name)
                continue
            facts = congress.facts(leg)
            rec.update(name=facts["name"], party=facts["party"], state=facts["state"], chamber=facts["chamber"],
                       leadership=facts["leadership"], photo_url=rec["photo_url"] or facts["photo_url"])
        if not rec["photo_url"]:
            local = next((x for x in Path(cfg["PHOTOS_DIR"]).glob(p["id"] + ".*")), None)
            rec["photo_url"] = local.as_uri() if local else folk.participant_picture(p)
        if not rec["photo_url"] and finder is not None:
            rec["photo_url"] = search_photo(cfg, folk, finder, state, group["id"], rec)
        if not rec["photo_url"]:
            log.warning("no photo for %s (%s): add a Photo URL in folk or %s/%s.jpg; skipping the face card",
                        name, kind, cfg["PHOTOS_DIR"], p["id"])
        out.append(rec)
    return out


def search_photo(cfg, folk, finder, state, group_id, rec):
    """Ask the photo agent once per person (re-asking after PHOTO_SEARCH_RETRY_DAYS when it found
    nothing). A verified hit is written to folk's Photo URL field so it shows in the CRM and can be
    overridden there. Raises photo_finder.ContactRequested if the model wants to talk to Eli."""
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
    res = finder.find(rec["name"], context)
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
        try:
            folk.update_person(p, group_id, {"Photo URL": url})
        except folk_api.FolkError as e:
            log.error("could not write Photo URL for %s to folk: %s", rec["name"], e)
    else:
        log.info("photo search: nothing usable for %s (%s)", rec["name"], (res or {}).get("reason"))
    return url


def fetch_photo(url):
    if url.startswith("file://"):
        return Path(urllib.request.url2pathname(url[7:])).read_bytes()
    req = urllib.request.Request(url, headers={"User-Agent": "folk-to-anki"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def make_cards(cfg, folk, congress, anki, decks, dry_run, state, finder=None):
    people = gather(cfg, folk, congress, state, finder)
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

    def group_by_name(self, name):
        return self.GROUP

    def missing_fields(self, group_id):
        return []

    def all_people(self):
        return self.people

    def people_in_group(self, group_id):
        return self.people

    def update_person(self, person, group_id, values):
        self.writes.append((person["fullName"], values))
        person.setdefault("customFieldValues", {}).setdefault(group_id, {}).update(values)

    def participant_picture(self, person):
        return None


def run_test(cfg, anki, cleanup):
    if cleanup:
        log.info("deleting deck %r and its cards", TEST_DECK)
        anki.call("deleteDecks", decks=[TEST_DECK], cardsToo=True)
        return
    folk = FakeFolk()
    fixtures = config.PROJECT_DIR / "tests" / "fixtures"
    events = calendar_feed.parse_events((fixtures / "calendar.ics").read_text())
    now = dt.datetime(2026, 11, 7, 12, tzinfo=dt.timezone.utc)
    contacts = calendar_feed.contacts(events, ["eli@example.org"], ["example.org"], 10, 90, 0, now=now)
    print("calendar contacts:", json.dumps(contacts, default=str, indent=1))
    assert "blumenthal@senate.example" in contacts and contacts["blumenthal@senate.example"]["last_met"]
    assert contacts["staffer@senate.example"]["next_meeting"] and not contacts["staffer@senate.example"]["last_met"]
    assert "bigtalk@elsewhere.example" not in contacts, "events over MAX_ATTENDEES must not count"
    assert "colleague@example.org" not in contacts, "ignored domain leaked"
    congress = congress_mod.Congress(config.APP_SUPPORT / "congress")
    decks = {"dc": TEST_DECK + "::DC", "networking": TEST_DECK + "::Networking"}
    people = gather(cfg, folk, congress)
    kinds = {p["name"]: p["kind"] for p in people}
    print("kinds:", kinds)
    assert kinds.get("Richard Blumenthal") == "Congressperson" and kinds.get("Sam Staffer") == "DC staffer"
    assert kinds.get("Nina Networker") == "Networking" and "Skipped Person" not in kinds
    state = {}
    counts = make_cards(cfg, folk, congress, anki, decks, dry_run=False, state=state)
    assert counts["failed"] == 0 and counts["added"] + counts["unchanged"] >= 8, counts
    again = make_cards(cfg, folk, congress, anki, decks, dry_run=False, state=state)
    assert again["added"] == 0 and again["updated"] == 0, f"second run must be a no-op: {again}"
    print(f"\nTEST PASSED. Cards are in Anki deck {TEST_DECK!r}; remove with: folk_to_anki.py test --cleanup")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["setup", "flag", "cards", "run", "test"])
    ap.add_argument("--dry-run", action="store_true", help="print what would change; write nothing")
    ap.add_argument("--daily", action="store_true", help="exit at once if a run already succeeded today")
    ap.add_argument("--create-fields", action="store_true", help="setup: create the missing folk custom fields")
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
    if a.daily and state.get("last_success") == today:
        log.info("already ran today; nothing to do")
        return
    folk = folk_api.Folk(cfg["FOLK_API_KEY"])
    if a.command == "setup":
        groups = folk.groups()
        print("folk groups:", ", ".join(f"{g['name']} ({g['id']})" for g in groups))
        group = folk.group_by_name(cfg["FOLK_GROUP"])
        missing = folk.missing_fields(group["id"])
        print(f"group {group['name']!r}: missing fields: {[f['name'] for f in missing] or 'none'}")
        if missing and a.create_fields:
            for f in missing:
                print("creating", f["name"])
                folk.create_field(group["id"], f)
        return
    if a.command in ("flag", "run"):
        flag(cfg, folk, a.dry_run)
    if a.command in ("cards", "run"):
        congress = congress_mod.Congress(config.APP_SUPPORT / "congress")
        decks = {"dc": cfg["DECK_DC"], "networking": cfg["DECK_NETWORKING"]}
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
