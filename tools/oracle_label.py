"""Semantic oracle labeler — INDEPENDENT of the production verifier.

Labels (kalshi_title, polymarket_title) pairs as TRUE/FALSE/ABSTAIN by direct
semantic rules about whether they resolve on the same real-world event. This is
deliberately NOT the verifier's logic — it's a second opinion to build ground
truth. Conservative: ABSTAIN when genuinely unsure (those get manual review).
"""
import json, re, sys

# Normalize for comparison: lowercase, drop punctuation, collapse space.
def norm(t):
    t = t.lower().replace("-", " ").replace(".", "")
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()

US_STATES = {"alabama","alaska","arizona","arkansas","california","colorado","connecticut",
 "delaware","florida","georgia","hawaii","idaho","illinois","indiana","iowa","kansas",
 "kentucky","louisiana","maine","maryland","massachusetts","michigan","minnesota",
 "mississippi","missouri","montana","nebraska","nevada","ohio","oklahoma","oregon",
 "pennsylvania","tennessee","texas","utah","vermont","virginia","washington","wisconsin",
 "wyoming","new hampshire","new mexico","new jersey","new york","north carolina",
 "north dakota","south carolina","south dakota","west virginia","rhode island"}
COUNTRIES={"israel","qatar","lebanon","iran","saudi arabia","ukraine","russia","china",
 "taiwan","india","pakistan","brazil","mexico","canada","france","germany","japan",
 "north korea","south korea","united kingdom","venezuela","egypt","turkey"}

def find_places(t, vocab):
    found=set()
    for v in vocab:
        if re.search(r"\b"+re.escape(v)+r"\b", t):
            found.add(v)
    # drop substrings of a longer match (york within new york handled by \b)
    return found

YEAR=re.compile(r"\b(20\d\d)\b")
DISTRICT=re.compile(r"\b([a-z]{2})\s?(al|\d{1,2})\b")  # ca 41, vt al

def label(k, p):
    nk, npp = norm(k), norm(p)
    if nk == npp:
        return "true", "identical"
    # Year tokens in title (authoritative event date).
    yk, yp = set(YEAR.findall(nk)), set(YEAR.findall(npp))
    if yk and yp and not (yk & yp):
        return "false", f"year {yk}!={yp}"
    # Geography
    sk = find_places(nk, US_STATES) | find_places(nk, COUNTRIES)
    sp = find_places(npp, US_STATES) | find_places(npp, COUNTRIES)
    if sk != sp:
        return "false", f"geo {sk}!={sp}"
    # Congressional district scope
    dk = set(DISTRICT.findall(nk)); dp = set(DISTRICT.findall(npp))
    # only treat as district if "house"/"seat"/"district" context present
    if ("house" in nk or "district" in nk or "house" in npp or "district" in npp):
        if dk != dp and (dk or dp):
            return "false", f"district {dk}!={dp}"
    # Categorical vs specific
    kc = nk.split()[0] in {"who","which"} if nk else False
    pc = npp.split()[0] in {"who","which"} if npp else False
    if kc != pc:
        return "false", "categorical_vs_binary"
    # Person/subject: capitalized proper-noun sequences in ORIGINAL case.
    def proper_nouns(orig):
        # sequences of Capitalized words not at sentence start template
        words=orig.split()
        names=set()
        for w in words:
            wl=w.strip("?.,").lower()
            if w[:1].isupper() and wl not in {"will","the","a","an","who","which","what","when"} and wl.isalpha() and len(wl)>2:
                names.add(wl)
        return names
    npk, nnp = proper_nouns(k), proper_nouns(p)
    # Remove shared template proper nouns that aren't subjects (rough): keep all
    if npk and nnp:
        # if neither set's names overlap at all (after prefix tolerance), different subject
        def matches(a, bset):
            return any(a==b or (len(a)>=4 and len(b)>=4 and (a[:4]==b[:4] or a in b or b in a)) for b in bset)
        shared=any(matches(a,nnp) for a in npk)
        if not shared:
            return "false", f"names {sorted(npk)}!={sorted(nnp)}"
    # Strong lexical identity after norm with small edit → true
    ka=set(nk.split()); pa=set(npp.split())
    jac=len(ka&pa)/len(ka|pa) if (ka|pa) else 0
    if jac>=0.75:
        return "true", f"jaccard={jac:.2f}"
    return "abstain", f"jaccard={jac:.2f}"

if __name__=="__main__":
    sample=json.load(open(sys.argv[1] if len(sys.argv)>1 else "/tmp/tolabel.json"))
    out=[]
    from collections import Counter
    c=Counter()
    for r in sample:
        lab,reason=label(r["kalshi_title"], r["polymarket_title"])
        r["oracle"]=lab; r["oracle_reason"]=reason
        c[lab]+=1
        out.append(r)
    json.dump(out, open("/tmp/labeled.json","w"))
    print("oracle labels:", dict(c))
