"""Turns one person into the list of Anki notes the templates call for.

Each note is {"deck", "model", "fields": {...}, "tags", "key", "photo_url"} where key is
"<folk person id>|<kind>" and is what makes reruns update instead of duplicate.
Templates (Eli's Roam networking templates, 2026-11):
  Congressperson: photo -> Name/Role; state; party; leadership role cloze; chair/ranking member cloze + reverse.
  DC staffer:     photo -> Name/Role(office); office; party.
  Networking:     photo -> Name/Deal.
Whenever the answer is a person's name, the answer side also shows that person's photo (Eli,
2026-11-17): the role and committee cards show the member, a staffer's office card shows the boss.
"""
import html

BASIC, CLOZE = "Folk Basic", "Folk Cloze"
PARTY_ADJ = {"Democrat": "Democratic", "Republican": "Republican", "Independent": "Independent"}


def esc(s):
    return html.escape(str(s or ""), quote=False)


def img(person_id):
    return f'<img src="folk-{person_id}.jpg">'


def member_img(bioguide):
    return f'<img src="folk-member-{bioguide}.jpg">'


def photo_front(person_id):
    return f'<div class="front-side">{img(person_id)}</div>'


def listing(pairs):
    items = "".join(f"<li>{esc(k)}: {esc(v)}</li>" for k, v in pairs if v)
    return f'<div class="back-side list"><ul>{items}</ul></div>'


def q(field, *parts):
    """An Anki search for notes whose `field` contains the parts in order. Used to find a card Eli
    already made by hand (his Roam cards) so the tool does not add a second one."""
    clean = [str(x).replace('"', "").replace("*", "").replace("_", "?") for x in parts]
    return f'"{field}:*' + "*".join(clean) + '*"'


def staff_role(p):
    """A role is "x at y", never just the organisation (Eli, 2026-11-17): "Senior Policy Adviser for
    Jeff Merkley", "Staffer for Sara Jacobs", "Counsel at House Foreign Affairs Committee".
    With no job title it is "Staffer for <office>", the wording on Eli's own Roam cards
    ("Staffer for Blumenthal", "Staffer for the House China Committee"). -> (label, text)."""
    title, office = p.get("job_title") or "", p.get("office") or ""
    if office.startswith("PSM "):                  # folk shorthand for professional staff member
        title, office = title or "Professional Staff Member", office[4:]
    if p.get("office_bioguide"):
        return "Role", f"{title or 'Staffer'} for {office}"
    if title and office:
        return "Role", f"{title} at {office}"
    return "Role", (title or (f"Staffer for {office}" if office else ""))


def build(p, decks):
    """p: a plain dict from folk_to_anki.gather(); decks: {"dc":..., "networking":...}."""
    pid, name, kind = p["id"], p["name"], p["kind"]
    notes = []
    base_tags = ["folk-to-anki", kind.replace(" ", "_")]

    names = [name] + [a for a in p.get("aliases", []) if a != name]

    def note(k, model, fields, deck, extra_tags=(), photo=False, older=None):
        notes.append({"key": f"{pid}|{k}", "model": model, "deck": deck,
                      "older": [older(n) for n in names] if older else [],
                      "fields": dict(fields, Key=f"{pid}|{k}"),
                      "tags": base_tags + list(extra_tags), "photo_url": p.get("photo_url") if photo else None})

    if kind == "Congressperson":
        deck = decks["dc"]
        role = f"{PARTY_ADJ.get(p['party'], p['party'])} {p['chamber']}"
        if p.get("photo_url"):
            note("photo", BASIC, {"Front": photo_front(pid), "Back": listing([("Name", name), ("Role", role)])},
                 deck, ["names_and_faces"], photo=True, older=lambda n: q("Back", "Name: " + n + "<"))
        note("state", BASIC, {"Front": f"{esc(name)}'s state?", "Back": esc(p["state"])}, deck,
             older=lambda n: q("Front", n, "s state?"))
        note("party", BASIC, {"Front": f"{esc(name)}'s party?", "Back": esc(p["party"])}, deck,
             older=lambda n: q("Front", n, "s party?"))
        # Shown on the answer side of both cloze cards; a missing picture never drops these notes.
        face = img(pid) if p.get("photo_url") else ""
        for role in p.get("roles", []):
            note("role:" + role, CLOZE,
                 {"Text": f"{{{{c1::{esc(name)}}}}} is the {{{{c2::{esc(role)}}}}}.", "Back Extra": face}, deck,
                 older=lambda n, role=role: q("Text", n, role))
        for lead in p.get("leadership", []):
            cid, title, committee = lead["committee_id"], lead["title"], lead["committee"]
            note(f"committee:{cid}", CLOZE,
                 {"Text": f"{{{{c1::{esc(name)}}}}} is the {esc(title)} of the {{{{c2::{esc(committee)}}}}}.",
                  "Back Extra": face}, deck, older=lambda n, c=committee: q("Text", n, c))
            back = esc(name) + (f"<br>{img(pid)}" if p.get("photo_url") else "")
            note(f"committee_rev:{cid}", BASIC,
                 {"Front": f"Who is the {esc(title)} of the {esc(committee)}?", "Back": back}, deck, photo=True,
                 # only when his card names the same person: after an election his old card may be out of date
                 older=lambda n, t=title, c=committee: q("Front", t, c.removeprefix("House ").removeprefix("Senate "))
                 + " " + q("Back", n))

    elif kind == "DC staffer":
        deck = decks["dc"]
        if p.get("photo_url"):
            note("photo", BASIC, {"Front": photo_front(pid), "Back": listing([("Name", name), staff_role(p)])},
                 deck, ["names_and_faces"], photo=True, older=lambda n: q("Back", "Name: " + n + "<"))
        if p.get("office"):
            boss = f"<br>{member_img(p['office_bioguide'])}" if p.get("office_bioguide") else ""
            note("office", BASIC, {"Front": f"{esc(name)}'s office?", "Back": esc(p["office"]) + boss}, deck,
                 older=lambda n: q("Front", n, "s office?"))
        if p.get("party"):
            note("party", BASIC, {"Front": f"{esc(name)}'s party?", "Back": esc(p["party"])}, deck,
                 older=lambda n: q("Front", n, "s party?"))

    else:  # Networking
        deck = decks["networking"]
        if p.get("photo_url"):
            note("photo", BASIC, {"Front": photo_front(pid), "Back": listing([("Name", name), (p.get("deal_label") or "Deal", p["deal"])])},
                 deck, ["names_and_faces"], photo=True, older=lambda n: q("Back", "Name: " + n + "<"))
    return notes
