"""
Whole-site (bulk) mode for 100,000s of pages.

Pipeline (each stage is resumable; progress lives in a SQLite file, default data/bulk.db):
  1. sitemap : read every URL from the sitemap (index files and .gz supported)
  2. crawl   : download pages with a thread pool + rate limit; store title/H1/description/paragraphs
  3. match   : FREE, no AI. TF-IDF finds the most related pages for every page, then anchor phrases
               are found in body paragraphs (never headings), max 2 per target, with a cap on how many
               links any one target page receives.
  4. ai      : OPTIONAL. Sends only the top candidates + most relevant paragraphs of each page to an LLM
               (one call per page). Use it on a limited number of pages; it costs money / API quota.

CLI:  python bulk.py --sitemap https://example.com/sitemap.xml [--max-pages 500] [--ai --provider groq ...]
"""
import argparse
import csv
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

import internal_links as il

DB_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bulk.db")
SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".zip", ".mp4", ".mp3", ".css", ".js")
HEAD = ["Source page", "Target URL", "Type", "Paragraph #", "Anchor text", "Suggested sentence (add_text only)",
        "Reason", "Score", "Method"]
SCHEMA = """
CREATE TABLE IF NOT EXISTS pages(url TEXT PRIMARY KEY, status INTEGER DEFAULT 0, tries INTEGER DEFAULT 0,
  title TEXT, h1 TEXT, descr TEXT, paras TEXT, links TEXT, err TEXT, matched INTEGER DEFAULT 0, ai_done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS suggestions(id INTEGER PRIMARY KEY, source TEXT, target TEXT, type TEXT, para INTEGER,
  anchor TEXT, sentence TEXT, reason TEXT, score REAL, method TEXT);
CREATE TABLE IF NOT EXISTS cands(source TEXT, target TEXT, score REAL);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS idx_pages_status ON pages(status);
CREATE INDEX IF NOT EXISTS idx_sug_source ON suggestions(source);
CREATE INDEX IF NOT EXISTS idx_cands_source ON cands(source);
"""  # status: 0 pending, 1 ok, 2 failed, 3 skipped (non-HTML / noindex)

SEP = re.compile(r"\s+[|\-\u2013\u2014:\u00b7\u2022\u00bb]\s+")
CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class RateLimiter:
    def __init__(self, per_sec):
        self.iv = 1.0 / per_sec if per_sec else 0
        self.lock, self.nxt = threading.Lock(), 0.0

    def wait(self):
        if not self.iv:
            return
        with self.lock:
            now = time.monotonic()
            t = max(now, self.nxt)
            self.nxt = t + self.iv
        if t > now:
            time.sleep(t - now)


_local = threading.local()


def fetch_one(url, rl, stop):
    """Download + parse one page. Returns (url, status, data) or None if stopped."""
    err = ""
    for attempt in range(3):
        if stop.is_set():
            return None
        rl.wait()
        try:
            sess = getattr(_local, "s", None) or requests.Session()
            _local.s = sess
            r = sess.get(url, headers=il.UA, timeout=20)
            if r.status_code in (429, 500, 502, 503, 504):
                err = f"HTTP {r.status_code}"
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code != 200:
                return url, 2, {"err": f"HTTP {r.status_code}"}
            if "html" not in r.headers.get("content-type", "html").lower():
                return url, 3, {"err": "not html"}
            soup = BeautifulSoup(r.text[:2_000_000], il.PARSER)
            rob = soup.find("meta", attrs={"name": re.compile("^robots$", re.I)})
            if rob and "noindex" in (rob.get("content") or "").lower():
                return url, 3, {"err": "noindex"}
            title = soup.title.get_text(strip=True) if soup.title else ""
            d = soup.find("meta", attrs={"name": "description"})
            h = soup.find("h1")
            h1 = h.get_text(" ", strip=True) if h else ""
            _, paras, linked, _ = il.extract_from_soup(soup, url, with_blocks=False)
            return url, 1, {"title": title, "h1": h1, "descr": d.get("content", "") if d else "",
                            "paras": json.dumps([[p["text"], p["linked"]] for p in paras[:80]]),
                            "links": json.dumps(sorted(linked))}
        except requests.RequestException as e:
            err = str(e)[:200]
            time.sleep(1 + attempt)
    return url, 2, {"err": err}


def target_phrases(title, h1, url):
    """Candidate anchor phrases for a target page, longest first (from H1/title/slug, 2-6 words)."""
    texts = [p.strip() for t in (h1, title) for p in SEP.split(t or "") if p.strip()]
    slug = re.sub(r"\.\w+$", "", urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]).replace("-", " ").replace("_", " ")
    if slug and not slug.isdigit():
        texts.append(slug)
    out = {}
    for t in texts:
        words = re.findall(r"[\w'\u2019]+(?:-[\w'\u2019]+)*", t)
        L = len(words)
        if L == 1 and len(words[0]) >= 6 and words[0].lower() not in il.STOP:
            out[words[0]] = 1
        for n in range(min(L, 6), 1, -1):
            for i in range(L - n + 1):
                g = words[i:i + n]
                if g[0].lower() in il.STOP or g[-1].lower() in il.STOP:
                    continue
                out[" ".join(g)] = 1
    return sorted(out, key=lambda p: (-len(p.split()), -len(p)))[:40]


def find_anchors(paras, lows, phrases, used, max_n=2):
    """Find up to max_n phrases that occur verbatim in body paragraphs and are not already linked."""
    out = []
    for ph in phrases:
        pl = ph.lower()
        if pl in used or any(pl in a.lower() or a.lower() in pl for _, a in out):
            continue
        for pi, low in enumerate(lows):
            pos = low.find(pl)
            while pos != -1:
                end = pos + len(pl)
                if ((pos == 0 or not low[pos - 1].isalnum()) and (end == len(low) or not low[end].isalnum())
                        and not any(pl in lt or lt in pl for lt in paras[pi][1])):
                    out.append((pi, paras[pi][0][pos:end]))
                    break
                pos = low.find(pl, pos + 1)
            else:
                continue
            break
        if len(out) >= max_n:
            break
    for _, a in out:
        used.add(a.lower())
    return out


class Bulk:
    def __init__(self, path=None):
        self.path = path or os.environ.get("BULK_DB") or DB_DEFAULT
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.stop, self.running, self.stage, self.error, self.logs = threading.Event(), False, "idle", None, []
        db = self._conn()
        db.executescript(SCHEMA)
        db.close()

    def _conn(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    def log(self, msg):
        self.logs.append(time.strftime("%H:%M:%S ") + msg)
        del self.logs[:-300]
        print(msg, flush=True)

    def reset(self):
        db = self._conn()
        db.executescript("DELETE FROM pages; DELETE FROM suggestions; DELETE FROM cands; DELETE FROM kv;")
        db.commit()
        db.close()
        self.logs, self.error, self.stage = [], None, "idle"

    # ---------- stage 1: sitemap ----------
    def load_sitemap(self, sitemap, max_pages=None):
        db = self._conn()
        old = db.execute("SELECT v FROM kv WHERE k='sitemap'").fetchone()
        if old and old[0] != sitemap:
            self.log("Different sitemap than the stored one: clearing previous data.")
            db.executescript("DELETE FROM pages; DELETE FROM suggestions; DELETE FROM cands;")
        db.execute("INSERT OR REPLACE INTO kv VALUES('sitemap', ?)", (sitemap,))
        db.commit()
        self.log("Reading sitemap...")
        urls = [u for u in dict.fromkeys(il.parse_sitemap(sitemap)) if urlparse(u).scheme in ("http", "https")]
        if max_pages:
            urls = urls[:max_pages]
        db.executemany("INSERT OR IGNORE INTO pages(url, status) VALUES (?, ?)",
                       [(u, 3 if u.lower().split("?")[0].endswith(SKIP_EXT) else 0) for u in urls])
        db.commit()
        db.close()
        self.log(f"Sitemap: {len(urls)} URLs.")

    # ---------- stage 2: crawl ----------
    def crawl(self, workers=10, rate=5.0):
        db = self._conn()
        pending = [r[0] for r in db.execute("SELECT url FROM pages WHERE status=0 OR (status=2 AND tries<3)")]
        if not pending:
            self.log("Crawl: nothing left to fetch.")
            return True
        self.log(f"Crawling {len(pending)} pages ({workers} workers, max {rate}/sec)...")
        rl, done = RateLimiter(rate), 0
        with ThreadPoolExecutor(workers) as ex:
            for i in range(0, len(pending), 400):
                if self.stop.is_set():
                    return False
                for res in ex.map(lambda u: fetch_one(u, rl, self.stop), pending[i:i + 400]):
                    if res is None:
                        continue
                    url, status, d = res
                    if status == 1:
                        db.execute("UPDATE pages SET status=1, title=?, h1=?, descr=?, paras=?, links=?, err=NULL WHERE url=?",
                                   (d["title"], d["h1"], d["descr"], d["paras"], d["links"], url))
                    elif status == 2:
                        db.execute("UPDATE pages SET status=2, tries=tries+1, err=? WHERE url=?", (d["err"], url))
                    else:
                        db.execute("UPDATE pages SET status=3, err=? WHERE url=?", (d["err"], url))
                    done += 1
                db.commit()
                if (i // 400) % 5 == 4 or i + 400 >= len(pending):
                    self.log(f"Crawled {done}/{len(pending)}")
        return not self.stop.is_set()

    # ---------- stage 3: match (free, no AI) ----------
    def match(self, top_k=5, min_score=0.03, max_inlinks=100):
        try:
            import numpy as np
            from sklearn.feature_extraction.text import TfidfVectorizer
        except ImportError:
            raise RuntimeError("Whole-site mode needs numpy and scikit-learn: pip install scikit-learn numpy")
        db, w = self._conn(), self._conn()
        rows = db.execute("SELECT url, title, h1, descr FROM pages WHERE status=1").fetchall()
        n = len(rows)
        if n < 2:
            raise RuntimeError("Not enough crawled pages to match yet.")
        urls = [r[0] for r in rows]
        titles, h1s = [r[1] or "" for r in rows], [r[2] or "" for r in rows]
        nidx = {il.norm(u): i for i, u in enumerate(urls)}
        slugs = [urlparse(u).path.replace("-", " ").replace("_", " ").replace("/", " ") for u in urls]
        profiles = [f"{titles[i]} {titles[i]} {titles[i]} {h1s[i]} {h1s[i]} {h1s[i]} {rows[i][3] or ''} {slugs[i]}"
                    for i in range(n)]
        vec = TfidfVectorizer(stop_words="english", sublinear_tf=True, max_df=0.5 if n > 20 else 1.0,
                              max_features=300000, ngram_range=(1, 2), dtype=np.float32)
        TT = vec.fit_transform(profiles).T.tocsr()
        cnt = np.zeros(n, dtype=np.int32)
        for t, c in db.execute("SELECT target, COUNT(*) FROM suggestions GROUP BY target"):
            if il.norm(t) in nidx:
                cnt[nidx[il.norm(t)]] += c

        @lru_cache(maxsize=60000)
        def phr(i):
            return target_phrases(titles[i], h1s[i], urls[i])

        total = db.execute("SELECT COUNT(*) FROM pages WHERE status=1 AND matched=0").fetchone()[0]
        self.log(f"Matching {total} pages against {n} candidate targets...")
        cur = db.execute("SELECT url, title, h1, paras, links FROM pages WHERE status=1 AND matched=0")
        done, bs, nb = 0, max(25, min(200, 20_000_000 // n)), 0  # keeps the dense similarity block ~80MB
        while True:
            batch = cur.fetchmany(bs)
            if not batch:
                break
            if self.stop.is_set():
                return False
            parsed, texts = [], []
            for url, title, h1, paras, links in batch:
                paras = json.loads(paras or "[]")
                parsed.append((url, paras, set(json.loads(links or "[]"))))
                texts.append(f"{title} {title} {title} {h1} {h1} " + " ".join(p[0] for p in paras)[:6000])
            sim = (vec.transform(texts) @ TT).toarray()
            sug, cand_rows = [], []
            for j, (url, paras, links) in enumerate(parsed):
                row = sim[j]
                me = nidx.get(il.norm(url))
                if me is not None:
                    row[me] = -1
                for l in links:
                    if l in nidx:
                        row[nidx[l]] = -1
                m = min(30, n - 1)
                top = np.argpartition(-row, m - 1)[:m]
                order = [int(t) for t in top[np.argsort(-row[top])] if row[t] >= min_score]
                cand_rows += [(url, urls[t], float(row[t])) for t in order[:10]]
                lows, used, accepted = [p[0].lower() for p in paras], set(), 0
                for t in order:
                    if cnt[t] >= max_inlinks:
                        continue
                    found = find_anchors(paras, lows, phr(t), used)
                    if not found:
                        continue
                    cnt[t] += 1
                    accepted += 1
                    sug += [(url, urls[t], "existing_text", pi, a, "", "Anchor phrase matches the target page's topic",
                             float(row[t]), "heuristic") for pi, a in found]
                    if accepted >= top_k:
                        break
            w.executemany("INSERT INTO suggestions(source,target,type,para,anchor,sentence,reason,score,method) "
                          "VALUES (?,?,?,?,?,?,?,?,?)", sug)
            w.executemany("INSERT INTO cands VALUES (?,?,?)", cand_rows)
            w.executemany("UPDATE pages SET matched=1 WHERE url=?", [(p[0],) for p in parsed])
            w.commit()
            done += len(batch)
            nb += 1
            if nb % 10 == 0 or done >= total:
                self.log(f"Matched {done}/{total}")
        return True

    # ---------- stage 4: AI refinement (optional) ----------
    def _ai_one(self, row, provider, model, base_url, rl, paras_n):
        url, title, paras, cands = row
        ctoks = il.tokens(" ".join(f"{c['title']} {c['h1']}" for c in cands))
        keep = sorted(sorted(range(len(paras)), key=lambda i: -len(il.tokens(paras[i][0]) & ctoks))[:paras_n])
        sub = [{"id": k, "text": paras[i][0], "linked": paras[i][1]} for k, i in enumerate(keep)]

        def call(*a):
            rl.wait()
            return il.call_llm(*a)

        res = il.suggest_links(title, sub, cands, provider, model, None, base_url, chunk_size=paras_n,
                               call=call, progress=lambda m: None)
        score = {c["url"]: c["score"] for c in cands}
        return url, [(url, s["target_url"], o["type"], keep[int(o["paragraph"])], o["anchor_text"],
                      o.get("suggested_sentence", ""), s.get("reason", ""), score.get(s["target_url"], 0.0), "ai")
                     for s in res for o in s["options"]]

    def ai_refine(self, provider, model=None, base_url=None, workers=3, rpm=30, max_pages=None, cands_n=8, paras_n=12):
        db, w = self._conn(), self._conn()
        todo = [r[0] for r in db.execute(
            "SELECT p.url FROM pages p WHERE p.status=1 AND p.matched=1 AND p.ai_done=0 "
            "ORDER BY (SELECT COUNT(*) FROM cands c WHERE c.source=p.url AND c.score>=0.1) DESC LIMIT ?",
            (max_pages or -1,))]
        if not todo:
            self.log("AI stage: nothing left to refine.")
            return True
        self.log(f"AI stage: {len(todo)} pages = about {len(todo)} API calls (max {rpm}/min, {workers} at a time).")
        rl, ex, done, consec = il_rate(rpm), ThreadPoolExecutor(workers), 0, 0
        try:
            for i in range(0, len(todo), workers * 4):
                if self.stop.is_set():
                    return False
                futs = {}
                for url in todo[i:i + workers * 4]:
                    r = db.execute("SELECT title, paras FROM pages WHERE url=?", (url,)).fetchone()
                    paras = json.loads(r[1] or "[]")
                    cs = [{"url": u, "title": t or "", "h1": h or "", "description": d or "", "score": s}
                          for u, t, h, d, s in db.execute(
                              "SELECT p.url, p.title, p.h1, p.descr, c.score FROM cands c JOIN pages p ON p.url=c.target "
                              "WHERE c.source=? ORDER BY c.score DESC LIMIT ?", (url, cands_n))]
                    if not paras or not cs:
                        w.execute("UPDATE pages SET ai_done=1 WHERE url=?", (url,))
                        continue
                    futs[ex.submit(self._ai_one, (url, r[0] or "", paras, cs), provider, model, base_url, rl, paras_n)] = url
                w.commit()
                for f in as_completed(futs):
                    try:
                        url, rows = f.result()
                    except Exception as e:
                        consec += 1
                        if consec >= 10:
                            raise RuntimeError(f"AI stage stopped after 10 failures in a row. Last error: {e}")
                        continue
                    consec = 0
                    w.execute("DELETE FROM suggestions WHERE source=?", (url,))
                    w.executemany("INSERT INTO suggestions(source,target,type,para,anchor,sentence,reason,score,method) "
                                  "VALUES (?,?,?,?,?,?,?,?,?)", rows)
                    w.execute("UPDATE pages SET ai_done=1 WHERE url=?", (url,))
                    w.commit()
                    done += 1
                    if self.stop.is_set():
                        return False
                self.log(f"AI refined {done}/{len(todo)}")
            return True
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

    # ---------- orchestration ----------
    def run(self, sitemap, o):
        self.stop.clear()
        self.error, self.running = None, True
        try:
            self.stage = "sitemap"
            self.load_sitemap(sitemap, o.get("max_pages"))
            self.stage = "crawl"
            if self.stop.is_set() or not self.crawl(o.get("workers", 10), o.get("rate", 5.0)):
                return
            self.stage = "match"
            if not self.match(o.get("top_k", 5), o.get("min_score", 0.03), o.get("max_inlinks", 100)):
                return
            if o.get("ai"):
                self.stage = "ai"
                if not self.ai_refine(o["provider"], o.get("model"), o.get("base_url"), o.get("ai_workers", 3),
                                      o.get("rpm", 30), o.get("ai_pages")):
                    return
            self.stage = "done"
            self.log("Finished.")
        except Exception as e:
            self.error, self.stage = str(e), "error"
            self.log(f"Error: {e}")
        finally:
            if self.stop.is_set() and self.stage not in ("error", "done"):
                self.stage = "stopped"
                self.log("Stopped. Press Start to resume where it left off.")
            self.running = False

    # ---------- reporting ----------
    def counts(self):
        db = self._conn()
        q = lambda s: db.execute(s).fetchone()[0]
        c = {"total": q("SELECT COUNT(*) FROM pages"), "ok": q("SELECT COUNT(*) FROM pages WHERE status=1"),
             "failed": q("SELECT COUNT(*) FROM pages WHERE status=2"), "skipped": q("SELECT COUNT(*) FROM pages WHERE status=3"),
             "pending": q("SELECT COUNT(*) FROM pages WHERE status=0"),
             "matched": q("SELECT COUNT(*) FROM pages WHERE matched=1"), "ai_done": q("SELECT COUNT(*) FROM pages WHERE ai_done=1"),
             "suggestions": q("SELECT COUNT(*) FROM suggestions")}
        db.close()
        return c

    def preview(self, n=25):
        db = self._conn()
        r = db.execute("SELECT source,target,anchor,method FROM suggestions ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        db.close()
        return r

    def iter_rows(self):
        db = self._conn()
        try:
            yield from db.execute("SELECT source,target,type,para+1,anchor,sentence,reason,round(score,4),method "
                                  "FROM suggestions ORDER BY source, score DESC, id")
        finally:
            db.close()

    def rows_for(self, url):
        db = self._conn()
        r = db.execute("SELECT target,type,para,anchor,sentence,reason FROM suggestions WHERE source IN (?,?,?)",
                       (url, url.rstrip("/"), url.rstrip("/") + "/")).fetchall()
        db.close()
        return r

    def export_xlsx(self, outdir, max_rows=500_000):
        from openpyxl import Workbook
        files, wb, ws, n, path = [], None, None, 0, None
        for r in self.iter_rows():
            if wb is None or n >= max_rows:
                if wb:
                    wb.save(path)
                path = os.path.join(outdir, f"internal_link_suggestions_part{len(files) + 1}.xlsx")
                files.append(path)
                wb = Workbook(write_only=True)
                ws = wb.create_sheet("Suggestions")
                ws.append(HEAD)
                n = 0
            ws.append([CTRL.sub("", x) if isinstance(x, str) else x for x in r])
            n += 1
        if wb:
            wb.save(path)
        return files


def il_rate(rpm):
    return RateLimiter(max(rpm, 1) / 60.0)


def main():
    ap = argparse.ArgumentParser(description="Whole-site internal link suggestions (resumable).")
    ap.add_argument("--sitemap", required=True)
    ap.add_argument("--max-pages", type=int)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--rate", type=float, default=5.0, help="max requests per second to your site")
    ap.add_argument("--top-k", type=int, default=5, help="max target pages suggested per page")
    ap.add_argument("--max-inlinks", type=int, default=100, help="max suggested links pointing at one target page")
    ap.add_argument("--ai", action="store_true", help="also refine with an LLM (costs API quota/money)")
    ap.add_argument("--provider", choices=il.PROVIDERS, default="anthropic")
    ap.add_argument("--model")
    ap.add_argument("--base-url")
    ap.add_argument("--ai-pages", type=int, default=200, help="how many pages to send to the LLM")
    ap.add_argument("--rpm", type=int, default=30, help="LLM requests per minute")
    ap.add_argument("--ai-workers", type=int, default=3)
    ap.add_argument("--csv", default="bulk_suggestions.csv")
    a = ap.parse_args()
    b = Bulk()
    b.run(a.sitemap, dict(max_pages=a.max_pages, workers=a.workers, rate=a.rate, top_k=a.top_k, max_inlinks=a.max_inlinks,
                          min_score=0.03, ai=a.ai, provider=a.provider, model=a.model, base_url=a.base_url,
                          ai_pages=a.ai_pages, rpm=a.rpm, ai_workers=a.ai_workers))
    if b.error:
        raise SystemExit(b.error)
    with open(a.csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(HEAD)
        w.writerows(b.iter_rows())
    print(f"Saved {b.counts()['suggestions']} suggestions to {a.csv}")


if __name__ == "__main__":
    main()
