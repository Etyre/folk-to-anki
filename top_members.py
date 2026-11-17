"""Ranks members of the Senate and of the House by importance and releases a few per day for carding.

"Importance" here means importance to Palisade's AI policy work, scored from the public
@unitedstates data (see congress.py): floor leadership first, then chairs and ranking members of
the committees that handle AI, then seats on those committees. It is a starting order, not a
truth: names in member-priority.txt (Application Support, one per line, optional) go first in
their chamber, in the order written, so the list can be corrected by hand without touching code.

Before a new Congress convenes the cards are for the incoming one, not the sitting one. The data
only knows the sitting one, so next_congress.json (filled in from election results) says who is
leaving, which House members are moving up to the Senate, whether control of a chamber flipped
and, once the parties have voted, the new leadership posts. Committee chairs are not settled until
the new Congress organizes, so chair cards are left out until the data itself describes it.
"""
import datetime as dt
import json

import congress as congress_mod

# Per chamber: the term type in the data, the committee type, the committees by how much AI policy
# runs through them, and the id prefix of the Anki note keys.
CHAMBERS = {
    "senate": {"type": "sen", "title": "Senator", "prefix": "sen-",
               # Commerce, Armed Services, Intelligence, Judiciary, Appropriations
               "A": {"SSCM", "SSAS", "SLIN", "SSJU", "SSAP"},
               # Homeland Security, Foreign Relations, Banking, HELP, Energy, Finance
               "B": {"SSGA", "SSFR", "SSBK", "SSHR", "SSEG", "SSFI"}},
    "house": {"type": "rep", "title": "Representative", "prefix": "rep-",
              # Energy and Commerce, Science, Armed Services, Intelligence, Judiciary, Appropriations, China select
              "A": {"HSIF", "HSSY", "HSAS", "HLIG", "HSJU", "HSAP", "HSZS"},
              # Homeland Security, Foreign Affairs, Financial Services, Oversight, Education and Workforce, Ways and Means
              "B": {"HSHM", "HSFA", "HSBA", "HSGO", "HSED", "HSWM"}},
}
POINTS = {"A": (50, 40, 8), "B": (30, 25, 3), "other": (12, 10, 0)}       # chair, ranking member, member
LEADERSHIP = (("speaker", 120), ("majority leader", 100), ("minority leader", 100), ("whip", 60),
              ("president pro tempore", 30))
OTHER_LEADERSHIP = 20


def load_next(path, today=None):
    """-> the next_congress.json dict while that Congress has not convened yet, else None."""
    try:
        nxt = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return nxt if (today or dt.date.today().isoformat()) < nxt["starts"] else None


def _keys(leg):
    n = leg["name"]
    return {congress_mod.norm(f"{n['first']} {n['last']}"), congress_mod.norm(n.get("official_full", ""))} - {""}


def _names(items):
    return {congress_mod.norm(x) for x in items or []}


def roles(congress, leg, chamber, nxt):
    """Leadership posts in the Congress being carded: from next_congress.json once it lists that
    chamber's (an unlisted member then has none), otherwise the sitting ones."""
    listed = ((nxt or {}).get(chamber) or {}).get("roles")
    if listed is None:
        return congress.roles(leg)
    mine = {congress_mod.norm(k): v for k, v in listed.items()}
    return next((mine[k] for k in _keys(leg) if k in mine), [])


def rank(congress, chamber, priority_names=(), nxt=None):
    """-> [(legislator, score, [reasons])] for the chamber, most important first. With `nxt` (see
    load_next) that is the incoming chamber: departing members are dropped, House members moving
    up count as senators, and if control flipped today's ranking members count as chairs."""
    ch = CHAMBERS[chamber]
    departing, moving_up = _names(nxt and nxt.get("departing")), _names(nxt and nxt.get("from_house"))
    flipped = bool(((nxt or {}).get(chamber) or {}).get("flipped"))
    scored = {}
    for leg in congress.legislators:
        up = bool(_keys(leg) & moving_up) and leg["terms"][-1]["type"] == "rep"
        here = up if chamber == "senate" and leg["terms"][-1]["type"] == "rep" else \
            leg["terms"][-1]["type"] == ch["type"] and not up
        if not here or _keys(leg) & departing:
            continue
        score, why = 0, (["moving up from the House"] if up else [])
        for role in roles(congress, leg, chamber, nxt):
            minor = any(w in role.lower() for w in ("assistant", "deputy", "vice"))
            score += OTHER_LEADERSHIP if minor else next((p for key, p in LEADERSHIP if key in role.lower()),
                                                         OTHER_LEADERSHIP)
            why.append(role)
        scored[leg["id"]["bioguide"]] = [leg, score, why]
    for cid, members in congress.membership.items():
        com = congress.committees.get(cid)                # full committees only
        if not com or com.get("type") != chamber:
            continue
        chair, ranking, member = POINTS["A" if cid in ch["A"] else "B" if cid in ch["B"] else "other"]
        short = com["name"]
        for lead in ("Senate Committee on ", "Senate Select Committee on ", "House Committee on ",
                     "House Permanent Select Committee on ", "House Select Committee on "):
            short = short.replace(lead, "")
        for m in members:
            s = scored.get(m.get("bioguide"))
            if not s or s[2][:1] == ["moving up from the House"]:
                continue
            title = (m.get("title") or "").lower()
            is_chair, is_ranking = title.startswith("chair"), title == "ranking member"
            if flipped:
                is_chair, is_ranking = is_ranking, is_chair
            likely, sitting = ("likely ", " (sitting Congress)") if nxt else ("", "")
            if is_chair:
                s[1] += chair
                s[2].append(f"{likely}chair, {short}{sitting}")
            elif is_ranking:
                s[1] += ranking
                s[2].append(f"{likely}ranking member, {short}{sitting}")
            elif member:
                s[1] += member
                s[2].append(short)
    out = sorted(scored.values(), key=lambda s: (-s[1], s[0]["name"]["last"]))
    first = []
    for name in priority_names:
        leg = congress.match(name)
        hit = next((s for s in out if leg and s[0] is leg), None)
        if hit and hit not in first:
            hit[2].insert(0, "your priority list")
            first.append(hit)
    return [tuple(s) for s in first + [s for s in out if s not in first]]


def release(ranked_ids, released, today, per_day, total):
    """Adds to `released` ({bioguide: ISO date}) the next members due today, in rank order: at most
    `per_day` per calendar day and `total` overall. Missed days are not caught up in a burst.
    -> the ids newly released."""
    room = min(per_day - sum(1 for d in released.values() if d == today), total - len(released))
    new = [b for b in ranked_ids[:total] if b not in released][:max(room, 0)]
    for b in new:
        released[b] = today
    return new


def person(congress, leg, chamber, nxt=None):
    """A member as the plain dict cards.build understands (kind Congressperson). For an incoming
    Congress, committee posts are left out (not settled yet)."""
    f = congress.facts(leg)
    return {"id": CHAMBERS[chamber]["prefix"] + f["bioguide"], "bioguide": f["bioguide"], "name": f["name"],
            "kind": "Congressperson", "party": f["party"], "state": f["state"],
            "chamber": CHAMBERS[chamber]["title"], "leadership": [] if nxt else f["leadership"],
            "roles": roles(congress, leg, chamber, nxt), "office": "", "deal": "", "photo_url": f["photo_url"]}
