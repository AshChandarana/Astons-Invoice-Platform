"""
Raiser attribution (SPEC section 3).

Priority order:
  1. BrightManager record match — not available yet (BM API access TBC),
     so skipped for now.
  2. Parse the Xero invoice Reference for raiser initials, matching the
     current spreadsheet convention: 'LG/BT' for shared credit, 'DP' for
     sole. Only initials in the raiser registry match — random capitals
     in a reference never mis-attribute.
  3. Fallback: mandatory "raised by" picker at review; approval is
     blocked until set. Guarantees 100% attribution.

Shared credit: 50/50 on net by default; per-pair overrides via
xero_split_overrides. Both the computed split amounts and the raw pair
are stored so reporting can show either view.
"""

import json
import re

import db

_PAIR_RE = re.compile(r"\b([A-Z]{2,3})\s*/\s*([A-Z]{2,3})\b")
_TOKEN_RE = re.compile(r"\b[A-Z]{2,3}\b")


def known_initials() -> set:
    return {r["initials"] for r in db.xero_raisers_all(active_only=True)}


def parse_reference(reference: str, known: set = None) -> list:
    """Extract up to two raiser initials from an invoice reference.
    A registered pair like 'LG/BT' wins; otherwise any registered
    initials tokens in order of appearance. Unregistered initials never
    match."""
    if not reference:
        return []
    if known is None:
        known = known_initials()
    ref = reference.upper()

    for m in _PAIR_RE.finditer(ref):
        a, b = m.group(1), m.group(2)
        if a in known and b in known and a != b:
            return [a, b]

    hits = []
    for tok in _TOKEN_RE.findall(ref):
        if tok in known and tok not in hits:
            hits.append(tok)
    return hits[:2]


def unregistered_pair_hint(reference: str, known: set = None) -> str:
    """If the reference contains an initials-pair pattern that is not in
    the registry, return it so the UI can suggest adding it."""
    if not reference:
        return None
    if known is None:
        known = known_initials()
    m = _PAIR_RE.search(reference.upper())
    if m and not (m.group(1) in known and m.group(2) in known):
        return f"{m.group(1)}/{m.group(2)}"
    return None


def compute_split(raisers: list, net) -> str:
    """JSON split of the net fee across one or two raisers. Pairs split
    50/50 unless an override exists for 'A/B' (first-named's share).
    Amounts always sum exactly to net (second person gets the
    remainder)."""
    net = round(float(net or 0), 2)
    if not raisers:
        return json.dumps([])
    if len(raisers) == 1:
        return json.dumps([{"initials": raisers[0], "share": 1.0, "net": net}])

    pair = "/".join(raisers).upper()
    first_share = 0.5
    for o in db.xero_split_overrides_all():
        if o["pair"] == pair:
            first_share = float(o["first_share"])
            break
        # override stored the other way round applies inverted
        if o["pair"] == "/".join(reversed(raisers)).upper():
            first_share = 1.0 - float(o["first_share"])
            break
    first_net = round(net * first_share, 2)
    return json.dumps([
        {"initials": raisers[0], "share": first_share, "net": first_net},
        {"initials": raisers[1], "share": round(1.0 - first_share, 4),
         "net": round(net - first_net, 2)},
    ])


def raiser_emails(raisers: list) -> list:
    """Email addresses for the given initials, where known."""
    by_initials = {r["initials"]: r for r in db.xero_raisers_all()}
    return [by_initials[i]["email"] for i in raisers
            if i in by_initials and by_initials[i].get("email")]


def describe(raisers: list) -> str:
    by_initials = {r["initials"]: r["name"] for r in db.xero_raisers_all()}
    return " / ".join(f"{i} ({by_initials.get(i, '?')})" for i in raisers)
