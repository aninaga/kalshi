# Labeling task: do these two prediction markets resolve on the SAME event?

You are given pairs of market titles — one from **Kalshi**, one from **Polymarket**.
For each pair, decide whether they resolve to YES/NO based on the **exact same
real-world outcome**, such that a YES on one is economically the same bet as a
YES (or NO) on the other.

## Output: for each pair, a label
- **true**  — same resolving event (a trader could hedge one against the other).
- **false** — different events (even if titles look similar).
- **abstain** — genuinely cannot tell from the titles/rules given.

Also give a **polarity** for `true` pairs:
- **aligned**  — Kalshi-YES means the same as Polymarket-YES.
- **inverted** — Kalshi-YES means the same as Polymarket-NO (one title is negated,
  e.g. "Will X win" vs "Will X NOT win", or "growth" vs "negative growth").

## Decision rules (apply in order)

1. **Same subject/entity.** The person, team, company, or topic must be the same.
   - "Kash Patel arrested" vs "Joe Biden arrested" → **false** (different person).
   - "Letitia James arrested" vs "James Clapper arrested" → **false** (a shared
     surname is NOT the same person).
   - Tolerate spelling/format variants of the SAME entity: "Lee Jae Myung" ==
     "Lee Jae-myung", "U.S." == "US", "Team Falcons" == "Falcons", "Pro Baseball"
     == "MLB", "CPI Core" == "Core CPI". These are **true**.

2. **Same scope / geography.** National vs a single state/district/region differ.
   - "Win the U.S. Senate" vs "Win the Montana Senate race" → **false**.
   - "Win the House in 2028" vs "Win the VT-AL House seat" → **false**.
   - "CA-41 House seat" vs "CA-47 House seat" → **false** (different district).
   - "MA-6" vs "MA-06" → **true** (same district, zero-pad only).
   - "Israel and Qatar normalize" vs "Israel and Saudi Arabia normalize" → **false**.

3. **Same office/contest.** Different elected office = different event.
   - "Attorney General race in Michigan" vs "Michigan governor race" → **false**.
   - "Win the House" vs "Win the Senate" → **false**.
   - "governor" == "governorship" → same office.

4. **Same time period / year.** A different year or period = different event.
   - "2026 election" vs "2028 election" → **false**.
   - But the two venues often list different *close/resolution dates* for the
     SAME event (one uses election day, the other a later certification date,
     sometimes in the next calendar year). **Ignore the close_time fields** —
     judge by the YEAR and event named IN THE TITLE, not the close date.
   - "hottest year" vs "second-hottest year" → **false** (different outcome).
   - "2026 year" vs "June 2026" → **false** (different period).

5. **Same proposition / threshold / outcome.**
   - "On the ballot" vs "Win the election" → **false** (ballot access ≠ winning).
   - "Finish 2nd" vs "Finish in third place" → **false** (different rank).
   - "Above 3%" vs "Below 3%" → **false** outcome, but it IS the inversion →
     label **true / inverted** ONLY if the threshold is identical and merely the
     direction flips; otherwise **false**.
   - "Points 8+" vs "Points O/U 4.5" → **false** (different line).
   - "Who will win X?" (multi-candidate) vs "Will [one person] win X?" →
     **false** (a categorical market is not the same contract as one outcome).

6. **Use the rules text when present.** `kalshi_rules` / `polymarket_rules` give
   the resolution criteria. If they describe different conditions, **false**.

## When unsure
Prefer **abstain** over guessing. A few abstains are fine; a wrong confident
label is worse. If two titles are word-for-word identical, it's **true/aligned**.

## Output format
Return STRICT JSON: a list of objects, one per input pair, each:
`{"id": <int>, "label": "true|false|abstain", "polarity": "aligned|inverted|na", "reason": "<short>"}`
Use `"na"` polarity for false/abstain. Include EVERY id from your batch. Output
ONLY the JSON array, nothing else.
