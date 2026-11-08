"""Facts about current members of Congress from the public @unitedstates datasets:
name matching, state, party, chamber, full-committee chair/ranking member, official photo.
The three JSON files are cached in Application Support for a day."""
import json
import logging
import re
import time
import unicodedata
import urllib.request
from pathlib import Path

log = logging.getLogger("congress")
SRC = "https://unitedstates.github.io/congress-legislators/"
FILES = ["legislators-current.json", "committees-current.json", "committee-membership-current.json"]
PHOTO = "https://unitedstates.github.io/images/congress/225x275/{bioguide}.jpg"
STATES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
          "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
          "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
          "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
          "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
          "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
          "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
          "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
          "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
          "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia", "PR": "Puerto Rico", "GU": "Guam",
          "VI": "U.S. Virgin Islands", "AS": "American Samoa", "MP": "Northern Mariana Islands"}


def norm(name):
    name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    name = re.sub(r"\b(sen|senator|rep|representative|congressman|congresswoman|hon|dr|mr|mrs|ms)\.?\s+", "",
                  name, flags=re.I)
    name = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", name, flags=re.I)
    name = re.sub(r"[^a-z ]", " ", name.lower())
    return " ".join(name.split())


class Congress:
    def __init__(self, cache_dir, max_age=86400):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        data = {f: self._load(f, max_age) for f in FILES}
        self.legislators = data[FILES[0]]
        self.committees = {c["thomas_id"]: c for c in data[FILES[1]]}
        self.membership = data[FILES[2]]
        self.by_name = {}
        for leg in self.legislators:
            n = leg["name"]
            keys = {norm(f"{n['first']} {n['last']}"), norm(n.get("official_full", ""))}
            if n.get("nickname"):
                keys.add(norm(f"{n['nickname']} {n['last']}"))
            for k in keys - {""}:
                self.by_name.setdefault(k, leg)

    def _load(self, fname, max_age):
        p = self.cache_dir / fname
        if not p.exists() or time.time() - p.stat().st_mtime > max_age:
            try:
                req = urllib.request.Request(SRC + fname, headers={"User-Agent": "folk-to-anki"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    p.write_bytes(r.read())
            except OSError as e:
                if not p.exists():
                    raise
                log.warning("could not refresh %s (%s); using cached copy", fname, e)
        return json.loads(p.read_text())

    def match(self, full_name):
        return self.by_name.get(norm(full_name))

    def facts(self, leg):
        term = leg["terms"][-1]
        bioguide = leg["id"]["bioguide"]
        chamber = "Senator" if term["type"] == "sen" else "Representative"
        party = term["party"]
        state = STATES.get(term["state"], term["state"])
        if term["type"] == "rep" and term.get("district") not in (None, 0):
            seat = f"{term['state']}-{term['district']}"
        else:
            seat = term["state"]
        leadership = []
        for cid, members in self.membership.items():
            if cid not in self.committees:            # subcommittee keys are not in committees-current
                continue
            for m in members:
                if m.get("bioguide") == bioguide and m.get("title"):
                    t = m["title"].lower()
                    if t.startswith("chair") or t == "ranking member":
                        leadership.append({"committee_id": cid, "committee": self.committees[cid]["name"],
                                           "title": t})
        return {"name": leg["name"].get("official_full") or f"{leg['name']['first']} {leg['name']['last']}",
                "bioguide": bioguide, "chamber": chamber, "party": party, "state": state, "seat": seat,
                "photo_url": PHOTO.format(bioguide=bioguide), "leadership": leadership}
