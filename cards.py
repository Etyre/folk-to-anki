"""Turns one person into the list of Anki notes the templates call for.

Each note is {"deck", "model", "fields": {...}, "tags", "key", "photo_url"} where key is
"<folk person id>|<kind>" and is what makes reruns update instead of duplicate.
Templates (Eli's Roam networking templates, 2026-11):
  Congressperson: photo -> Name/Role; state; party; chair/ranking member cloze + reverse.
  DC staffer:     photo -> Name/Role(office); office; party.
  Networking:     photo -> Name/Deal.
"""
import html

BASIC, CLOZE = "Folk Basic", "Folk Cloze"
PARTY_ADJ = {"Democrat": "Democratic", "Republican": "Republican", "Independent": "Independent"}


def esc(s):
    return html.escape(str(s or ""), quote=False)


def img(person_id):
    return f'<img src="folk-{person_id}.jpg">'


def photo_front(person_id):
    return f'<div class="front-side">{img(person_id)}</div>'


def listing(pairs):
    items = "".join(f"<li>{esc(k)}: {esc(v)}</li>" for k, v in pairs if v)
    return f'<div class="back-side list"><ul>{items}</ul></div>'


def build(p, decks):
    """p: a plain dict from folk_to_anki.gather(); decks: {"dc":..., "networking":...}."""
    pid, name, kind = p["id"], p["name"], p["kind"]
    notes = []
    base_tags = ["folk-to-anki", kind.replace(" ", "_")]

    def note(k, model, fields, deck, extra_tags=(), photo=False):
        notes.append({"key": f"{pid}|{k}", "model": model, "deck": deck,
                      "fields": dict(fields, Key=f"{pid}|{k}"),
                      "tags": base_tags + list(extra_tags), "photo_url": p.get("photo_url") if photo else None})

    if kind == "Congressperson":
        deck = decks["dc"]
        role = f"{PARTY_ADJ.get(p['party'], p['party'])} {p['chamber']}"
        if p.get("photo_url"):
            note("photo", BASIC, {"Front": photo_front(pid), "Back": listing([("Name", name), ("Role", role)])},
                 deck, ["names_and_faces"], photo=True)
        note("state", BASIC, {"Front": f"{esc(name)}'s state?", "Back": esc(p["state"])}, deck)
        note("party", BASIC, {"Front": f"{esc(name)}'s party?", "Back": esc(p["party"])}, deck)
        for lead in p.get("leadership", []):
            cid, title, committee = lead["committee_id"], lead["title"], lead["committee"]
            note(f"committee:{cid}", CLOZE,
                 {"Text": f"{{{{c1::{esc(name)}}}}} is the {esc(title)} of the {{{{c2::{esc(committee)}}}}}.",
                  "Back Extra": ""}, deck)
            back = esc(name) + (f"<br>{img(pid)}" if p.get("photo_url") else "")
            note(f"committee_rev:{cid}", BASIC,
                 {"Front": f"Who is the {esc(title)} of the {esc(committee)}?", "Back": back}, deck, photo=True)

    elif kind == "DC staffer":
        deck = decks["dc"]
        if p.get("photo_url"):
            note("photo", BASIC, {"Front": photo_front(pid), "Back": listing([("Name", name), ("Role", p["office"])])},
                 deck, ["names_and_faces"], photo=True)
        if p.get("office"):
            note("office", BASIC, {"Front": f"{esc(name)}'s office?", "Back": esc(p["office"])}, deck)
        if p.get("party"):
            note("party", BASIC, {"Front": f"{esc(name)}'s party?", "Back": esc(p["party"])}, deck)

    else:  # Networking
        deck = decks["networking"]
        if p.get("photo_url"):
            note("photo", BASIC, {"Front": photo_front(pid), "Back": listing([("Name", name), ("Deal", p["deal"])])},
                 deck, ["names_and_faces"], photo=True)
    return notes
