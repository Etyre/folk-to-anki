"""Reads Google Calendar secret iCal feeds and answers: which external people has Eli met
(past events) and which is he about to meet (upcoming events)?

Minimal VEVENT parser on purpose: we only need DTSTART, SUMMARY, STATUS, ATTENDEE and
ORGANIZER. Recurring events count from their first DTSTART; that is enough to say "met".
Lines are unfolded first (iCal wraps at 75 chars, which otherwise splits email domains).
"""
import datetime as dt
import logging
import re
import urllib.request

log = logging.getLogger("calendar")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "folk-to-anki"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def parse_events(text):
    text = re.sub(r"\r?\n[ \t]", "", text)           # unfold continuation lines
    events = []
    for block in re.findall(r"BEGIN:VEVENT\r?\n(.*?)\r?\nEND:VEVENT", text, re.S):
        ev = {"attendees": [], "organizer": None, "status": "", "summary": "", "start": None}
        for line in block.splitlines():
            if ":" not in line:
                continue
            head, value = line.split(":", 1)
            name, _, params = head.partition(";")
            name = name.upper()
            if name == "DTSTART":
                ev["start"] = _parse_dt(value, params)
            elif name == "SUMMARY":
                ev["summary"] = value.replace("\\,", ",")
            elif name == "STATUS":
                ev["status"] = value.upper()
            elif name in ("ATTENDEE", "ORGANIZER"):
                m = re.search(r"CN=((?:\"[^\"]*\")|[^;:]*)", params)
                cn = (m.group(1).strip('"') if m else "").strip()
                email = value.lower().replace("mailto:", "").strip()
                pm = re.search(r"PARTSTAT=([A-Z-]+)", params)
                person = {"name": cn if cn and "@" not in cn else "", "email": email,
                          "partstat": pm.group(1) if pm else ""}
                if name == "ATTENDEE":
                    ev["attendees"].append(person)
                else:
                    ev["organizer"] = person
        if ev["start"]:
            events.append(ev)
    return events


def _parse_dt(value, params):
    value = value.strip()
    try:
        if "VALUE=DATE" in params or len(value) == 8:
            return dt.datetime.strptime(value, "%Y%m%d").replace(tzinfo=dt.timezone.utc)
        if value.endswith("Z"):
            return dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
        local = dt.datetime.strptime(value, "%Y%m%dT%H%M%S")
        return local.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    except ValueError:
        return None


def contacts(events, my_emails, ignore_domains, max_attendees, upcoming_days, past_days, now=None):
    """-> {email: {"name", "email", "last_met": date|None, "next_meeting": date|None,
                   "events": [summary...]}} for external people on real meetings."""
    now = now or dt.datetime.now(dt.timezone.utc)
    horizon = now + dt.timedelta(days=upcoming_days)
    floor = now - dt.timedelta(days=past_days) if past_days else None
    my = {e.lower() for e in my_emails}
    ignore = tuple(d.lower().lstrip("@") for d in ignore_domains)
    out = {}
    for ev in events:
        if ev["status"] == "CANCELLED" or not ev["attendees"]:
            continue
        if len(ev["attendees"]) > max_attendees:
            continue
        if any(a["email"] in my and a["partstat"] == "DECLINED" for a in ev["attendees"]):
            continue
        start = ev["start"]
        if start > horizon or (floor and start < floor):
            continue
        people = ev["attendees"] + ([ev["organizer"]] if ev["organizer"] else [])
        for a in people:
            email = a["email"]
            if not email or email in my or email.endswith(ignore) or a["partstat"] == "DECLINED":
                continue
            if email.endswith("calendar.google.com"):          # rooms and shared calendars, not people
                continue
            c = out.setdefault(email, {"name": a["name"], "email": email, "last_met": None,
                                       "next_meeting": None, "events": []})
            if a["name"] and not c["name"]:
                c["name"] = a["name"]
            day = start.date()
            if start <= now:
                if not c["last_met"] or day > c["last_met"]:
                    c["last_met"] = day
            elif not c["next_meeting"] or day < c["next_meeting"]:
                c["next_meeting"] = day
            c["events"].append(f"{day} {ev['summary']}")
    return out
