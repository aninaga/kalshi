"""Canonical NBA team tricodes.

Kalshi tickers and Polymarket slugs both use the standard NBA tricodes
(SAS, OKC, ...). ESPN uses its own abbreviations for a handful of teams
(SA, GS, NY, NO, UTAH, WSH), so we resolve ESPN team objects back to the
canonical tricode here.
"""

# canonical tricode -> (espn abbreviation, location, nickname)
TRICODES = {
    "ATL": ("ATL", "Atlanta", "Hawks"),
    "BOS": ("BOS", "Boston", "Celtics"),
    "BKN": ("BKN", "Brooklyn", "Nets"),
    "CHA": ("CHA", "Charlotte", "Hornets"),
    "CHI": ("CHI", "Chicago", "Bulls"),
    "CLE": ("CLE", "Cleveland", "Cavaliers"),
    "DAL": ("DAL", "Dallas", "Mavericks"),
    "DEN": ("DEN", "Denver", "Nuggets"),
    "DET": ("DET", "Detroit", "Pistons"),
    "GSW": ("GS", "Golden State", "Warriors"),
    "HOU": ("HOU", "Houston", "Rockets"),
    "IND": ("IND", "Indiana", "Pacers"),
    "LAC": ("LAC", "LA", "Clippers"),
    "LAL": ("LAL", "Los Angeles", "Lakers"),
    "MEM": ("MEM", "Memphis", "Grizzlies"),
    "MIA": ("MIA", "Miami", "Heat"),
    "MIL": ("MIL", "Milwaukee", "Bucks"),
    "MIN": ("MIN", "Minnesota", "Timberwolves"),
    "NOP": ("NO", "New Orleans", "Pelicans"),
    "NYK": ("NY", "New York", "Knicks"),
    "OKC": ("OKC", "Oklahoma City", "Thunder"),
    "ORL": ("ORL", "Orlando", "Magic"),
    "PHI": ("PHI", "Philadelphia", "76ers"),
    "PHX": ("PHX", "Phoenix", "Suns"),
    "POR": ("POR", "Portland", "Trail Blazers"),
    "SAC": ("SAC", "Sacramento", "Kings"),
    "SAS": ("SA", "San Antonio", "Spurs"),
    "TOR": ("TOR", "Toronto", "Raptors"),
    "UTA": ("UTAH", "Utah", "Jazz"),
    "WAS": ("WSH", "Washington", "Wizards"),
}

_ESPN_ABBR = {espn.upper(): tri for tri, (espn, _loc, _nick) in TRICODES.items()}
# also accept a few common alternates
_ESPN_ABBR.update({"PHO": "PHX", "UTA": "UTA", "NOR": "NOP", "WSH": "WAS"})


def from_espn(team: dict) -> str:
    """Resolve an ESPN competitor ``team`` dict to a canonical tricode."""
    abbr = (team.get("abbreviation") or "").upper()
    if abbr in _ESPN_ABBR:
        return _ESPN_ABBR[abbr]
    if abbr in TRICODES:
        return abbr
    loc = (team.get("location") or team.get("displayName") or "").lower()
    for tri, (_espn, location, nick) in TRICODES.items():
        if location.lower() in loc or nick.lower() in loc:
            return tri
    raise ValueError(f"Unrecognized ESPN team: {team!r}")


def location(tri: str) -> str:
    return TRICODES[tri][1]


def nickname(tri: str) -> str:
    return TRICODES[tri][2]
