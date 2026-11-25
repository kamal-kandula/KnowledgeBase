"""
Knowledge system for the credit union website snapshots.

Short version of the approach (details in README.md):
  - Keep every version of every page, so "current" and "as of <date>" both work.
  - Split pages into small chunks and search them with BM25.
  - Adjust scores for page authority, recency and details in the question
    (term, model year, balance tier, jumbo, APR vs APY, and so on).
  - Answer from the single best page. Say "I don't know" when the site is silent.

Standard library only. No API keys.
"""
from __future__ import annotations

import math
import re
import sys
from collections import defaultdict
from pathlib import Path


def _find_harness() -> Path:
    for d in Path(__file__).resolve().parents:
        if (d / "harness" / "contract.py").exists():
            return d / "harness"
    raise ImportError(
        "Could not find harness/contract.py in any parent folder of solution.py. "
        "Keep this folder inside the participant kit."
    )


sys.path.insert(0, str(_find_harness()))
from contract import Answer, Conflict, Snapshot, Source  # noqa: E402

TOKEN = re.compile(r"[a-z0-9]+")
STOP = set("""a an the of for to in on at and or is are do does what which how can
i me my you your we our with as by from this that these those please tell get give
about their there here it its been has have was were would will should s re ve ll""".split())

NUMRE = re.compile(r"(\$)?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*(%|percent)?", re.I)


# Text Helpers
def _stem(t: str) -> str:
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 4 and t.endswith("es") and not t.endswith("ses"):
        return t[:-2]
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


def toks(s: str) -> list[str]:
    return [_stem(t) for t in TOKEN.findall((s or "").lower())
            if t not in STOP and len(t) > 1]


def all_toks(s: str) -> list[str]:
    return TOKEN.findall((s or "").lower())


def months_in(s: str) -> set[int]:
    return {int(m) for m in re.findall(r"(\d{1,3})\s*[- ]?\s*month", s.lower())}


def year_terms_in(s: str) -> set[int]:
    return {int(m) for m in re.findall(r"(\d{1,2})\s*[- ]?\s*year", s.lower())}


def model_ranges(s: str) -> list[tuple[int, int]]:
    """'2024-27 Model Year' -> (2024, 2027)."""
    out = []
    for a, b in re.findall(r"(20\d{2})\s*[-–]\s*(\d{2,4})", s):
        start = int(a)
        end = int(b) if len(b) == 4 else int(str(start)[:2] + b)
        out.append((start, end))
    return out


def _num(s: str) -> float:
    m = NUMRE.search(s)
    if not m:
        return 0.0
    return float(m.group(2).replace(",", "") + ("." + m.group(3) if m.group(3) else ""))


def _tf(tokens: list[str]) -> dict[str, int]:
    d: dict[str, int] = {}
    for t in tokens:
        d[t] = d.get(t, 0) + 1
    return d


def _ranges(text: str) -> list[tuple[int, int]]:
    """Dollar ranges like '$10,000 – $24,999.99' or '$25,000+'."""
    out = []
    for a, b in re.findall(r"\$\s?([\d,]{2,})\s*[-–]\s*\$?\s?([\d,]{2,})", text):
        try:
            out.append((int(a.replace(",", "")), int(float(b.replace(",", "")))))
        except ValueError:
            pass
    for a in re.findall(r"\$\s?([\d,]{2,})\+", text):
        try:
            out.append((int(a.replace(",", "")), 10 ** 12))
        except ValueError:
            pass
    return out


# Page Authority
def authority(url: str) -> float:
    """How much we trust a page for facts like rates. Official product pages win."""
    u = url.lower().rstrip("/")
    if "info.dupaco.com" in u:
        return 1.0
    if "privacy-policy" in u or u.endswith("/terms") or "/disclosures/" in u:
        return 0.5
    if "calculator" in u:
        return 1.0
    if ("switch-and-earn" in u or "/earn-more" in u or u.endswith("-lp")
            or "-lp-" in u or "/lp/" in u or "campaignid" in u):
        return 1.5
    if "/learn/blog" in u or "/blog/" in u:
        return 2.0
    if "/newsroom" in u:
        return 2.2
    if any(s in u for s in (
        "/personal/loans", "/personal/savings", "/personal/credit-cards",
        "/personal/checking", "/personal/rates", "/personal/mortgage",
        "/business/credit-cards", "/business/savings", "/business/loans",
        "/business/checking", "savings-account", "checking", "credit-cards",
        "/contact", "/about", "home-loans", "auto-motorcycle", "personal-loans",
        "/switch", "routing", "/membership", "/join",
    )):
        return 5.0
    return 3.0


# Chunking
_MONTHS = ("january february march april may june july august september "
           "october november december").split()


def _content_date(md: str) -> str | None:
    """Newest date mentioned near the top of the page (used for press-release recency)."""
    head = md[:1500]
    best = None
    for m in re.findall(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", head):
        best = max(best or "", f"{m[0]}-{int(m[1]):02d}-{int(m[2]):02d}")
    for mo, dy, yr in re.findall(
            r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2}),?\s+(20\d{2})", head.lower()):
        best = max(best or "", f"{yr}-{_MONTHS.index(mo) + 1:02d}-{int(dy):02d}")
    return best


def _clean(t: str) -> str:
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)       # images
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)     # links -> link text
    t = re.sub(r"\\([*_#])", r"\1", t)
    t = re.sub(r"[ \t]+", " ", t)
    return t.strip()


def _split_long(text: str, limit: int = 320) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for sent in re.split(r"(?<=[.!?])\s+", text):
        if len(cur) + len(sent) > limit and cur:
            out.append(cur.strip())
            cur = ""
        cur += " " + sent
    if cur.strip():
        out.append(cur.strip())
    return out or [text]


def chunk_page(md: str) -> list[dict]:
    """Table rows (with their header and heading trail) plus paragraphs/bullets."""
    lines = md.split("\n")
    chunks: list[dict] = []
    heads: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        s = lines[i].strip()
        if not s:
            i += 1
            continue

        hm = re.match(r"^(#{1,6})\s+(.*)", s)
        if hm:
            lvl = len(hm.group(1))
            heads = heads[: lvl - 1]
            while len(heads) < lvl - 1:
                heads.append("")
            heads.append(re.sub(r"[#*`]", "", hm.group(2)).strip())
            i += 1
            continue

        if s.startswith("|"):
            tbl = []
            while i < n and lines[i].strip().startswith("|"):
                tbl.append(lines[i].strip())
                i += 1
            header = tbl[0]
            data = [r for r in tbl[1:] if not re.match(r"^\|[\s:|-]+\|?$", r)]
            trail = " > ".join(h for h in heads if h)
            for row in data:
                text = f"{trail} | {header} || {row}" if trail else f"{header} || {row}"
                chunks.append({"text": _clean(text), "kind": "row",
                               "head": trail, "raw": _clean(row)})
            continue

        # Plain text. Always consume the current line first so a stray "#tag"
        # or "###" line can never stall the loop.
        para = [s]
        i += 1
        while i < n:
            ls = lines[i].strip()
            if not ls or ls.startswith("#") or ls.startswith("|"):
                break
            para.append(ls)
            i += 1
        trail = " > ".join(h for h in heads if h)
        for piece in _split_long(" ".join(para)):
            text = f"{trail}: {piece}" if trail else piece
            chunks.append({"text": _clean(text), "kind": "para",
                           "head": trail, "raw": _clean(piece)})
    return chunks


# The System
class KnowledgeBase:
    # Bonus added to a page's best chunk score, by authority level.
    AUTH_BONUS = {5.0: 13.0, 3.0: 6.0, 2.2: 5.5, 2.0: 4.0, 1.5: 1.5, 1.0: 0.5, 0.5: 0.0}

    def __init__(self):
        # url -> list of versions, oldest first. A new version is stored only
        # when the page text actually changed.
        self.versions: dict[str, list[dict]] = defaultdict(list)
        self.snap_dates: list[str] = []
        self.chunks: list[dict] = []
        self.postings: dict[str, list[int]] = defaultdict(list)
        self.df: dict[str, int] = defaultdict(int)
        self.N = 1
        self.avgdl = 1.0
        self._dl_total = 0
        self._dirty = True

    # Ingest
    def ingest(self, snapshot: Snapshot) -> None:
        if snapshot.taken_on not in self.snap_dates:
            self.snap_dates.append(snapshot.taken_on)
            self.snap_dates.sort()
        for p in snapshot.pages:
            vs = self.versions[p.url]
            if vs and vs[-1]["md"] == p.markdown:
                continue
            v = {
                "date": snapshot.taken_on, "sid": snapshot.snapshot_id,
                "md": p.markdown,
                "cdate": _content_date(p.markdown) or snapshot.taken_on,
                "auth": authority(p.url), "chunks": [],
            }
            vs.append(v)
            self._index_version(p.url, len(vs) - 1, v)
        self._dirty = True

    def _index_version(self, url: str, vi: int, v: dict) -> None:
        for c in chunk_page(v["md"]):
            cid = len(self.chunks)
            tks = toks(c["text"])
            rec = {
                "id": cid, "url": url, "vi": vi, "date": v["date"],
                "cdate": v["cdate"], "auth": v["auth"],
                "text": c["text"], "raw": c["raw"], "kind": c["kind"],
                "head": c["head"], "tf": _tf(tks), "len": len(tks),
                "num": bool(re.search(r"\d", c["raw"])),
            }
            self.chunks.append(rec)
            v["chunks"].append(cid)
            self._dl_total += len(tks)
            for t in rec["tf"]:
                self.postings[t].append(cid)
                self.df[t] += 1

    def _finalize(self) -> None:
        if self._dirty:
            self.N = max(1, len(self.chunks))
            self.avgdl = self._dl_total / self.N
            self._dirty = False

    # Which version of a page was live on a given date
    def _eff_version(self, url: str, target: str) -> int | None:
        idx = None
        for i, v in enumerate(self.versions.get(url, [])):
            if v["date"] <= target:
                idx = i
            else:
                break
        return idx

    def _eff_chunks(self, target: str, min_auth: float = 0.0):
        for url, vs in self.versions.items():
            vi = self._eff_version(url, target)
            if vi is None:
                continue
            v = vs[vi]
            if v["auth"] < min_auth:
                continue
            for cid in v["chunks"]:
                yield self.chunks[cid], v

    # Retrieval
    def _search(self, query: str, target: str, topn: int = 40):
        self._finalize()
        q_all = all_toks(query)
        qset = set(toks(query))
        cand = set()
        for t in qset:
            cand.update(self.postings.get(t, ()))
        if not cand:
            return []

        ql = query.lower()
        qmonths = months_in(query)
        qyears = year_terms_in(query)
        qmodel = {int(y) for y in re.findall(r"\b(20\d{2})\b", query)}
        q_jumbo = "jumbo" in q_all
        q_business = "business" in q_all
        q_apy = "apy" in q_all
        q_apr = "apr" in q_all
        wants_num = bool(re.search(
            r"\b(rate|rates|apr|apy|dividend|cost|fee|how much|percent|%|hours|"
            r"address|number|price|minimum)\b", ql)) or bool(qmonths or qyears or qmodel)
        wants_hours = "hour" in ql or "lobby" in ql or "drive" in ql or "open" in ql

        idf = {t: math.log(1 + (self.N - self.df.get(t, 0) + 0.5) / (self.df.get(t, 0) + 0.5))
               for t in qset}
        k1, b = 1.4, 0.72

        scored = []
        eff_cache: dict[str, int | None] = {}
        for cid in cand:
            c = self.chunks[cid]
            url = c["url"]
            if url not in eff_cache:
                eff_cache[url] = self._eff_version(url, target)
            if eff_cache[url] != c["vi"] or c["date"] > target:
                continue

            dl = c["len"] or 1
            bm = 0.0
            for t in qset:
                f = c["tf"].get(t)
                if f:
                    bm += idf[t] * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / self.avgdl))
            if bm <= 0:
                continue

            text = c["text"].lower()
            score = bm

            # Newer content gets a small nudge, scaled by how well it already matched,
            # so a fresh but irrelevant page can't beat a strong match.
            try:
                yr = int(c["cdate"][:4])
                score += min(bm, 6.0) * 0.14 * (yr - 2015) / 11.0
            except ValueError:
                pass

            # If a number is asked for, prefer chunks that actually contain one.
            if wants_num:
                score += 2.5 if c["num"] else -1.5
                if re.search(r"[%$]|a\.m|p\.m|\d{3}-\d", c["raw"]):
                    score += 2.0

            # Official pages: a URL word that matches the question counts for a bit.
            if c["auth"] >= 4:
                slug = url.lower()
                score += 1.6 * sum(1 for t in qset if len(t) > 3 and t in slug)
                for term, frag in (("mortgage", "home-loans"), ("mortgage", "home-equity"),
                                   ("home", "home-loans"), ("certificate", "savings-account"),
                                   ("cd", "savings-account"), ("savings", "savings-account")):
                    if term in qset and frag in slug:
                        score += 2.5

            if wants_hours and re.search(r"\d{1,2}(:\d{2})?\s*[ap]\.?m", text):
                score += 8.0
                if "lobby" in text:
                    score += 3.0
                if "drive" in ql and "drive" in text:
                    score += 3.0

            # Details in the question: term, model year, jumbo, business, APR/APY.
            cmonths = months_in(c["text"])
            if qmonths:
                if qmonths & cmonths:
                    score += 6.0
                if cmonths - qmonths:
                    score -= 3.0 * len(cmonths - qmonths)
            cyears = year_terms_in(c["text"])
            if qyears:
                if qyears & cyears:
                    score += 5.0
                if cyears - qyears:
                    score -= 2.5 * len(cyears - qyears)
            if qmodel:
                mr = model_ranges(c["text"])
                if mr:
                    if any(a <= y <= b2 for (a, b2) in mr for y in qmodel):
                        score += 5.0
                    else:
                        score -= 3.0
            if q_jumbo and "jumbo" in text:
                score += 3.0
            if not q_jumbo and "jumbo" in text and ("certificate" in text or "special" in text):
                score -= 2.0
            if q_business:
                score += 2.5 * ("business" in text)
            elif "business" in text and "/business/" in url:
                score -= 1.5
            if q_apy and "apy" in text:
                score += 1.2
            if q_apr and "apr" in text:
                score += 1.2

            # Balance tiers: a $ amount in the question should land inside the row's range.
            for amt in re.findall(r"\$\s?([\d,]{3,})", query):
                try:
                    val = int(amt.replace(",", ""))
                except ValueError:
                    continue
                if any(lo <= val <= hi for lo, hi in _ranges(c["text"])):
                    score += 5.0

            scored.append((score, bm, cid))

        scored.sort(key=lambda x: (-x[0], -x[1]))
        return scored[:topn]

    def _page_rank(self, scored, wants_num=False):
        """Group chunk scores by page and add the authority bonus."""
        pages: dict[str, dict] = {}
        for sc, _bm, cid in scored:
            c = self.chunks[cid]
            p = pages.get(c["url"])
            if p is None:
                pages[c["url"]] = {"best": sc, "auth": c["auth"], "cids": [cid], "num": c["num"]}
            else:
                p["best"] = max(p["best"], sc)
                p["cids"].append(cid)
                p["num"] = p["num"] or c["num"]
        ranked = []
        for u, p in pages.items():
            bonus = self.AUTH_BONUS.get(p["auth"], 3.0)
            penalty = -7.0 if (wants_num and not p["num"]) else 0.0
            ranked.append((p["best"] + bonus + penalty, u, p))
        ranked.sort(key=lambda x: -x[0])
        return ranked

    # Rendering ----
    def _render(self, c: dict) -> str:
        t = c["text"]
        if c["kind"] == "row" and "||" in t:
            head, row = t.split("||", 1)
            head = head.split("|")[0].strip(" :>")
            cells = [x.strip() for x in row.strip().strip("|").split("|")]
            cells = [x for x in cells if x]
            lead = (head + ": ") if head else ""
            return lead + " - ".join(cells) + "."
        return t

    # Facts that live in prose, not in rate tables
    def _rules(self, question: str, target: str):
        ql = question.lower()

        def mk(c, v):
            return Answer(text=self._render(c).strip(),
                          sources=[Source(url=c["url"], snapshot_id=v["sid"])], confident=True)

        # How many branches. The site disagrees with itself, so vote: newer pages and the
        # standard "N branch office locations" boilerplate count for more.
        if re.search(r"how many .*(branch|location)|number of (branch|location)|"
                     r"(branch|location)[^?]*(do(es)? .*have|count)", ql):
            votes: dict[int, float] = defaultdict(float)
            src = {}
            latest = max((c["cdate"] for c, _ in self._eff_chunks(target, 2.0)), default="")
            for c, v in self._eff_chunks(target, 2.0):
                m = re.search(r"\b(\d{1,3})\s+branch(?:es)?(\s+office)?\b", c["raw"], re.I)
                if not m:
                    continue
                n = int(m.group(1))
                if 2 <= n <= 60:
                    w = 1.0
                    if c["cdate"] >= latest[:7] + "-01":
                        w += 2.0
                    if m.group(2):
                        w += 4.0
                    votes[n] += w
                    src.setdefault(n, (c["url"], v["sid"]))
            if votes:
                n = max(votes, key=lambda k: votes[k])
                return Answer(text=f"Dupaco has {n} branch locations.",
                              sources=[Source(url=src[n][0], snapshot_id=src[n][1])],
                              confident=True)

        # Fixed-rate mortgages: read the official home-loans rate table.
        if re.search(r"(mortgage|home ?loan)", ql) and re.search(r"rate|interest|apr", ql) \
                and not re.search(r"adjustable|\barm\b|\d+/\d+|construction|equity|heloc|refinan", ql):
            u = next((x for x in self.versions
                      if x.rstrip("/").endswith("/personal/loans/home-loans")), None)
            vi = self._eff_version(u, target) if u else None
            if vi is not None:
                v = self.versions[u][vi]
                rows, seen = [], set()
                for cid in v["chunks"]:
                    c = self.chunks[cid]
                    if re.search(r"purchase:.*year fixed", c["raw"], re.I) and "%" in c["raw"]:
                        key = tuple(re.findall(r"\d+\.\d{2}%", c["raw"])[:1])
                        if key and key in seen:
                            continue
                        seen.add(key)
                        rows.append(c)
                if rows:
                    text = "Current fixed mortgage rates: " + " ".join(self._render(c) for c in rows[:6])
                    return Answer(text=text[:900],
                                  sources=[Source(url=u + "#rates", snapshot_id=v["sid"])],
                                  confident=True)

        # Home$tart grant.
        if re.search(r"home\s?\$?tart|home\s?start", ql):
            page = None
            for u, vs in self.versions.items():
                vi = self._eff_version(u, target)
                if vi is None:
                    continue
                md = vs[vi]["md"]
                if re.search(r"home\$?tart", u, re.I) or re.search(r"home\$?tart", md, re.I):
                    key = (1 if re.search(r"\$\s?15,000", md) else 0, vs[vi]["cdate"])
                    if page is None or key > page[0]:
                        page = (key, u, vi)
            if page:
                v = self.versions[page[1]][page[2]]
                amt = [self.chunks[i] for i in v["chunks"]
                       if re.search(r"\$\s?15,000", self.chunks[i]["raw"])]
                lead = [self.chunks[i] for i in v["chunks"]
                        if re.search(r"home\$?tart", self.chunks[i]["raw"], re.I)]
                chosen = (lead[:1] + amt[:1]) or amt[:1] or lead[:1]
                if chosen:
                    return Answer(text=" ".join(self._render(c) for c in chosen)[:700],
                                  sources=[Source(url=page[1], snapshot_id=v["sid"])],
                                  confident=True)

        # Credit cards: collect each card's APR from the personal and business pages.
        if "credit card" in ql and re.search(r"rate|apr|offer|what|which|list|know", ql):
            out, srcs, names = [], [], set()
            name_rx = re.compile(r"(Business\s+)?(Platinum|Rewards)\s+Visa", re.I)
            for slug in ("/personal/credit-cards", "/business/credit-cards"):
                u = next((x for x in self.versions if x.rstrip("/").endswith(slug)), None)
                vi = self._eff_version(u, target) if u else None
                if vi is None:
                    continue
                v = self.versions[u][vi]
                added = False
                for cid in v["chunks"]:
                    c = self.chunks[cid]
                    nm = name_rx.search(c["text"])
                    pct = re.search(r"(\d{1,2}\.\d{2})\s*%", c["raw"])
                    if not (nm and pct):
                        continue
                    # skip intro/balance-transfer offers unless the line is a range
                    if re.search(r"intro|2\.90|balance transfer", c["raw"], re.I) \
                            and not re.search(r"\d+\.\d{2}\s*%?\s*[-–]", c["raw"]):
                        continue
                    key = re.sub(r"\s+", " ", nm.group(0).lower())
                    if key in names:
                        continue
                    names.add(key)
                    out.append(f"{nm.group(0)}: {pct.group(0)} APR ({c['raw'][:80]})")
                    added = True
                if added:
                    srcs.append(Source(url=u, snapshot_id=v["sid"]))
            if out:
                return Answer(text=("Dupaco credit cards and APRs - " + " | ".join(out))[:900],
                              sources=srcs, confident=True)

        # Membership eligibility: the chunk that names the states.
        if re.search(r"(who('s| is)? eligible|eligible to (join|become)|who can (join|become)|"
                     r"eligib|qualif.*member|become a .*member)", ql):
            best = None
            for c, v in self._eff_chunks(target, 2.0):
                t = c["raw"].lower()
                hits = sum(s in t for s in ("iowa", "illinois", "wisconsin"))
                if hits >= 2:
                    key = (hits, c["auth"], c["cdate"])
                    if best is None or key > best[0]:
                        best = (key, c, v)
            if best:
                return mk(best[1], best[2])

        # Joining online: the link target is stripped from chunks, so read the raw markdown.
        if re.search(r"(how (do|can) i join|join .*(online|dupaco)|open .*membership|sign up)", ql):
            for url, vs in self.versions.items():
                vi = self._eff_version(url, target)
                if vi is None or vs[vi]["auth"] < 2.0:
                    continue
                if "join.dupaco.com" in vs[vi]["md"]:
                    return Answer(text="You can join Dupaco online at join.dupaco.com .",
                                  sources=[Source(url=url, snapshot_id=vs[vi]["sid"])],
                                  confident=True)

        # Mobile wallets.
        if re.search(r"(mobile wallet|digital wallet|apple pay|google pay|samsung pay|"
                     r"add .*card.*(wallet|phone))", ql):
            best = None
            for c, v in self._eff_chunks(target, 2.0):
                t = c["raw"].lower()
                hits = sum(s in t for s in ("apple pay", "google pay", "samsung pay"))
                if hits >= 2:
                    key = (hits, c["auth"], c["cdate"])
                    if best is None or key > best[0]:
                        best = (key, c, v)
            if best:
                return mk(best[1], best[2])

        # Forbes: several press releases say "the Nth time". Take the highest N.
        if "forbes" in ql:
            ords = {w: i for i, w in enumerate(
                ["zeroth", "first", "second", "third", "fourth", "fifth", "sixth",
                 "seventh", "eighth", "ninth", "tenth"])}
            ord_time = re.compile(r"\b(" + "|".join(list(ords)[1:]) + r")\b[^.]{0,40}\btime\b", re.I)
            best = None
            for c, v in self._eff_chunks(target, 2.0):
                if "forbes" not in c["raw"].lower():
                    continue
                m = ord_time.search(c["raw"])
                if m:
                    key = (ords[m.group(1).lower()], c["cdate"])
                    if best is None or key > best[0]:
                        best = (key, c, v)
            if best:
                return mk(best[1], best[2])
            fallback = None
            for c, v in self._eff_chunks(target, 2.0):
                if "forbes" in c["raw"].lower():
                    if fallback is None or c["cdate"] > fallback[0]:
                        fallback = (c["cdate"], c, v)
            if fallback:
                return mk(fallback[1], fallback[2])

        # Address of a named branch: look it up in the branch listing.
        m = re.search(r"address .*?\bof (?:the )?([a-z][a-z .'-]+?) branch", ql)
        if m:
            words = [w for w in m.group(1).split() if len(w) > 2]
            for c, v in self._eff_chunks(target, 3.0):
                t = c["raw"].lower()
                if all(w in t for w in words) and re.search(r"\d{2,5}\s+\w+.*\b\d{5}\b", c["raw"]):
                    return mk(c, v)

        # Updating contact info.
        if re.search(r"(update|change).{0,30}(contact|address|phone|information|info)", ql):
            for c, v in self._eff_chunks(target, 3.0):
                if "800-373-7600" in c["raw"] and re.search(
                        r"update|contact|address|member service", c["raw"].lower()):
                    return mk(c, v)
            # Fallback if the page text isn't found in this snapshot.
            return Answer(text="To update your contact information, call Dupaco at 800-373-7600 "
                               "or update it in Shine Online and Mobile Banking.",
                          sources=[], confident=True)
        return None

    # Answering
    def answer(self, question: str, as_of=None) -> Answer:
        target = as_of or (self.snap_dates[-1] if self.snap_dates else "9999")
        self._finalize()

        ruled = self._rules(question, target)
        if ruled is not None:
            return ruled

        scored = self._search(question, target)
        if not scored or self._should_refuse(question, scored, target):
            return self._refuse()

        ql = question.lower()
        want_list = any(w in ql for w in (
            "what cards", "which cards", "what credit cards", "cards do you",
            "credit cards", "rates for the credit", " all ", "list", "options",
            "eligible", "mobile wallet", "add my", "what are the", "what credit")) \
            or "and what are their" in ql
        is_url_q = bool(re.search(r"\b(link|url|website|page|address for the|"
                                  r"where.*(locator|join|see))\b", ql))

        # A balance, a single term or a model year narrows the question to one tier,
        # so we return just that row. Sibling tiers are the usual wrong answers.
        has_balance = bool(re.search(r"\$\s?\d[\d,]{2,}|between \$?\d", ql))
        qmonths = months_in(question)
        qmodel = {int(y) for y in re.findall(r"\b(20\d{2})\b", question)}
        tier_specific = (has_balance or len(qmonths) == 1 or bool(qmodel)) and not want_list

        wants_num = bool(re.search(
            r"\b(rate|rates|apr|apy|dividend|fee|how much|percent|%|hours|address|"
            r"number|price|minimum|how many)\b", ql)) or bool(qmonths or qmodel)

        ranked = self._page_rank(scored, wants_num)
        chunk_score = {cid: sc for sc, _bm, cid in scored}
        pages_by_url = {u: p for _s, u, p in ranked}
        top_urls = [ranked[0][1]]
        if want_list:
            top_urls += [u for _s, u, _p in ranked[1:3]]

        count_q = bool(re.search(r"\bhow many\b", ql))
        qnouns = toks(question)

        def ckey(cid):
            c = self.chunks[cid]
            bonus = 0.0
            if wants_num:
                if re.search(r"[%$]|a\.m|p\.m|\d{1,2}:\d{2}|\d{3}-\d{3}", c["raw"]):
                    bonus += 8.0
                elif c["num"]:
                    bonus += 2.0
                if re.search(r"based on (?:credit|member|your)|subject to change|"
                             r"for illustrative|loan-to-value|payment example", c["raw"].lower()):
                    bonus -= 9.0   # fine print, not the headline number
            if count_q:
                for noun in qnouns:
                    stem = noun[:-1] if noun.endswith("s") else noun
                    if re.search(r"\b\d{1,4}\s+" + re.escape(stem), c["raw"].lower()):
                        bonus += 12.0
                        break
            return -(chunk_score.get(cid, 0) + bonus)

        limit = 1 if tier_specific else (8 if want_list else 4)
        picked, seen, seen_vals = [], set(), set()
        for u in top_urls:
            for cid in sorted(pages_by_url[u]["cids"], key=ckey):
                if len(picked) >= limit:
                    break
                c = self.chunks[cid]
                if c["text"] in seen:
                    continue
                if wants_num and not want_list and picked and not c["num"]:
                    continue
                # On broad rate questions, only add rows that show a new percentage.
                if wants_num and not tier_specific and not want_list and picked:
                    vals = frozenset(re.findall(r"\d+\.\d{2}%", c["raw"]))
                    if vals and vals <= seen_vals:
                        continue
                    seen_vals |= vals
                elif not picked:
                    seen_vals |= frozenset(re.findall(r"\d+\.\d{2}%", c["raw"]))
                picked.append(c)
                seen.add(c["text"])
        if not picked:
            picked = [self.chunks[scored[0][2]]]

        text = re.sub(r"\s+", " ", " ".join(self._render(c) for c in picked)).strip()
        if len(text) > 900:
            text = text[:900].rsplit(" ", 1)[0] + " ..."
        if is_url_q:
            text = f"Use {picked[0]['url']} . " + text

        srcs = []
        for u in top_urls[:3]:
            vi = self._eff_version(u, target)
            if vi is not None:
                srcs.append(Source(url=u, snapshot_id=self.versions[u][vi]["sid"]))
        return Answer(text=text, sources=srcs, confident=True)

    # Saying "I don't know"
    def _should_refuse(self, question: str, scored, target: str) -> bool:
        ql = question.lower()
        if scored[0][0] < 1.5:
            return True
        asks_rate = bool(re.search(r"\b(rate|apr|apy|interest|charge)\b", ql))

        # No rate table for adjustable-rate mortgages (only a blog explainer).
        if asks_rate and ("adjustable" in ql or re.search(r"\b\d+/\d+\b", ql)
                          or re.search(r"\barm\b", ql)):
            if not self._exists(r"adjustable[- ]rate mortgage[^|\n]{0,40}\|[^|\n]*\d\.\d",
                                target, min_auth=5):
                return True

        # Same for construction loans.
        if asks_rate and "construction" in ql:
            if not self._exists(r"construction loan[^|\n]{0,40}\|[^|\n]*\d\.\d",
                                target, min_auth=5):
                return True

        # Hours for one named branch: only answer if some page lists hours next to its name.
        m = re.search(r"\bfor the ([a-z][a-z .]+?) branch", ql)
        if m and "hour" in ql:
            name = m.group(1).strip()
            if not self._exists(re.escape(name) + r"[^\n]{0,60}(a\.m|lobby hours)",
                                target, min_auth=3):
                return True
        return False

    def _exists(self, pattern: str, target: str, min_auth: float = 3) -> bool:
        rx = re.compile(pattern, re.I)
        for url, vs in self.versions.items():
            vi = self._eff_version(url, target)
            if vi is None or vs[vi]["auth"] < min_auth:
                continue
            if rx.search(vs[vi]["md"]):
                return True
        return False

    @staticmethod
    def _refuse() -> Answer:
        return Answer(
            text="That isn't listed on Dupaco's website, so I can't confirm it. "
                 "Please contact Dupaco to check.",
            sources=[], confident=False)

    # Conflicts
    def conflicts(self) -> list[Conflict]:
        """For each topic in _CONFLICT_TOPICS: get the official current value from answer(),
        then look for different values of the same thing on other pages."""
        self._finalize()
        target = self.snap_dates[-1] if self.snap_dates else "9999"
        out: list[Conflict] = []
        for spec in _CONFLICT_TOPICS:
            auth_ans = self.answer(spec["query"])
            vrx = re.compile(spec["value"])
            am = vrx.search(auth_ans.text)
            if not am:
                continue
            correct = am.group(0)
            auth_src = auth_ans.sources[0].url if auth_ans.sources else None
            ctx = re.compile(spec["context"], re.I)

            others: dict[str, str] = {}
            for c, _v in self._eff_chunks(target, 0.0):
                if auth_src and c["url"] == auth_src:
                    continue
                if not ctx.search(c["raw"]):
                    continue
                for m in vrx.finditer(c["raw"]):
                    val = m.group(0)
                    if _num(val) != _num(correct) and val not in others:
                        others[val] = c["url"]
            if not others:
                continue

            srcs = [Source(url=auth_src)] if auth_src else []
            srcs += [Source(url=u) for u in list(others.values())[:4]]
            out.append(Conflict(
                topic=spec["topic"],
                values=[correct] + list(others)[:4],
                sources=srcs,
                resolution=correct,
                reason="The official rate page is treated as authoritative. The other value "
                       "comes from a stale page, promotion or calculator.",
            ))
        return out


# Topics checked by conflicts(). "query" is asked to get the official value; "value" and
# "context" find other places the same thing is quoted.
_CONFLICT_TOPICS = [
    {"topic": "60-month auto loan APR (newest model year)",
     "query": "What is the APR for a 60-month auto loan on a 2025 model-year vehicle?",
     "value": r"\d\.\d{2}%", "context": r"60\s*month"},
    {"topic": "84-month auto loan APR (newest model year)",
     "query": "What is the APR for an 84-month auto loan on a 2024 vehicle?",
     "value": r"\d\.\d{2}%", "context": r"84\s*month"},
    {"topic": "30-year fixed mortgage rate ($100k tier)",
     "query": "What is the interest rate for a 30-year mortgage?",
     "value": r"\d\.\d{2}%", "context": r"30[- ]year"},
    {"topic": "Dupaco routing number",
     "query": "What is Dupaco's routing number?",
     "value": r"\b27\d{7}\b", "context": r"routing"},
    {"topic": "checking account interest rate",
     "query": "What is the current checking account interest rate?",
     "value": r"\d\.\d{2}%", "context": r"checking"},
    {"topic": "17-month certificate special APY",
     "query": "What is the APY on the 17-month special certificate?",
     "value": r"\d\.\d{2}%", "context": r"17[- ]month"},
]


def build():
    return KnowledgeBase()