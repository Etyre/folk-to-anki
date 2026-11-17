"""Ranks sitting senators by importance and releases a few per day for carding.

"Importance" here means importance to Palisade's AI policy work, scored from the public
@unitedstates data (see congress.py): floor leadership first, then chairs and ranking members of
the committees that handle AI, then seats on those committees. It is a starting order, not a
truth: names in senator-priority.txt (Application Support, one per line, optional) go first, in
the order written, so the list can be corrected by hand without touching the code.
"""
# Committees by how much AI policy runs through them. (chair, ranking member, member) points.
TIER_A = {"SSCM", "SSAS", "SLIN", "SSJU", "SSAP"}                    # Commerce, Armed Services, Intel, Judiciary, Approps
TIER_B = {"SSGA", "SSFR", "SSBK", "SSHR", "SSEG", "SSFI"}            # Homeland, Foreign Rel., Banking, HELP, Energy, Finance
POINTS = {"A": (50, 40, 8), "B": (30, 25, 3), "other": (12, 10, 0)}
LEADERSHIP = (("majority leader", 100), ("minority leader", 100), ("whip", 60), ("president pro tempore", 30))
OTHER_LEADERSHIP = 20


def rank(congress, priority_names=()):
    """-> [(legislator, score, [reasons])] for every sitting senator, most important first."""
    scored = {}
    for leg in congress.legislators:
        if leg["terms"][-1]["type"] != "sen":
            continue
        score, why = 0, []
        for role in congress.roles(leg):
            pts = next((p for key, p in LEADERSHIP if key in role.lower()), OTHER_LEADERSHIP)
            score += pts
            why.append(role)
        scored[leg["id"]["bioguide"]] = [leg, score, why]
    for cid, members in congress.membership.items():
        com = congress.committees.get(cid)                # full committees only
        if not com or com.get("type") != "senate":
            continue
        chair, ranking, member = POINTS["A" if cid in TIER_A else "B" if cid in TIER_B else "other"]
        short = com["name"].replace("Senate Committee on ", "").replace("Senate Select Committee on ", "")
        for m in members:
            s = scored.get(m.get("bioguide"))
            if not s:
                continue
            title = (m.get("title") or "").lower()
            if title.startswith("chair"):
                s[1] += chair
                s[2].append(f"chair, {short}")
            elif title == "ranking member":
                s[1] += ranking
                s[2].append(f"ranking member, {short}")
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
    """Adds to `released` ({bioguide: ISO date}) the next senators due today, in rank order: at most
    `per_day` per calendar day and `total` overall. Missed days are not caught up in a burst.
    -> the ids newly released."""
    room = min(per_day - sum(1 for d in released.values() if d == today), total - len(released))
    new = [b for b in ranked_ids[:total] if b not in released][:max(room, 0)]
    for b in new:
        released[b] = today
    return new


def person(congress, leg):
    """A senator as the plain dict cards.build understands (kind Congressperson)."""
    f = congress.facts(leg)
    return {"id": "sen-" + f["bioguide"], "bioguide": f["bioguide"], "name": f["name"], "kind": "Congressperson",
            "party": f["party"], "state": f["state"], "chamber": f["chamber"], "leadership": f["leadership"],
            "roles": f["roles"], "office": "", "deal": "", "photo_url": f["photo_url"]}

