#!/usr/bin/env python3
"""
Web front end for internal_links.py  (keep both files in the same folder).

    pip install -r requirements.txt
    cp .env.example .env             # add your API key(s)
    python app.py                    # then open http://127.0.0.1:5000
"""
import csv
import io
import json
import os
import tempfile
import threading
import time
import zipfile
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, render_template_string, request, send_file
from openpyxl import Workbook

import internal_links as il
import bulk as bk
from docx_export import build_docx

try:  # optional: load API keys from .env
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
HEADERS = ["Target URL", "Reason", "Type", "Paragraph #", "Anchor text", "Suggested sentence (add_text only)", "Paragraph excerpt"]

job = {"state": "idle", "log": [], "rows": [], "error": None, "page": "", "title": "", "blocks": [], "result": []}
stop_event = threading.Event()


class Stopped(BaseException):  # BaseException so 'except Exception' never swallows a Stop
    pass


def check():
    if stop_event.is_set():
        raise Stopped()


def interruptible(fn, *args):
    """Run a slow call in a helper thread so Stop takes effect within ~0.2s instead of waiting for it."""
    box = {}

    def target():
        try:
            box["v"] = fn(*args)
        except BaseException as e:  # re-raised below in the job thread
            box["e"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    while t.is_alive():
        t.join(0.2)
        check()
    check()
    if "e" in box:
        raise box["e"]
    return box["v"]


def log(msg):
    job["log"].append(msg)


def run(page, sitemap, provider, max_candidates, api_key=None, base_url=None, model=None, fetch_titles=True, refresh=False):
    try:
        t0 = time.time()
        log("Reading page...")
        title, paragraphs, linked, blocks = interruptible(il.extract_page_full, page)
        if not paragraphs:
            raise RuntimeError("No paragraph content found on the page.")
        check()
        log(f"Found {len(paragraphs)} paragraphs. Loading sitemap...")
        all_urls, cached = interruptible(il.get_sitemap_urls, sitemap, refresh)
        me = il.norm(page)
        urls = [u for u in all_urls if il.norm(u) != me and il.norm(u) not in linked]
        log(f"Sitemap: {len(all_urls):,} URLs ({'from cache' if cached else 'downloaded'}); {len(urls):,} to compare.")
        check()
        cands = interruptible(il.build_candidates, il.ranking_text(title, blocks, paragraphs), urls, max_candidates, fetch_titles, log)
        check()
        if not cands:
            raise RuntimeError("No relevant candidate pages found in the sitemap.")
        result = il.suggest_links(title, paragraphs, cands, provider, model, api_key, base_url,
                                  call=lambda *a: interruptible(il.call_llm, *a), progress=log, check=check)
        check()
        rows = []
        for s in result:
            for o in s["options"]:
                p = paragraphs[int(o["paragraph"])]["text"]
                rows.append([s["target_url"], s["reason"], o["type"], int(o["paragraph"]) + 1,
                             o["anchor_text"], o.get("suggested_sentence", ""), p[:140]])
        job["rows"] = rows
        job.update(title=title, blocks=blocks, result=result)
        log(f"Done in {time.time() - t0:.0f}s: {len(rows)} suggestions for {len(result)} pages.")
        job["state"] = "done"
    except Stopped:
        log("Stopped by user.")
        job["state"] = "stopped"
    except Exception as e:
        msg = str(e).replace(api_key, "***") if api_key else str(e)  # never echo the key
        job["error"] = msg
        log(f"Error: {msg}")
        job["state"] = "error"


@app.post("/start")
def start():
    if job["state"] in ("running", "stopping"):
        return jsonify(error="A job is already running."), 409
    d = request.get_json(force=True)
    page, sitemap = (d.get("page") or "").strip(), (d.get("sitemap") or "").strip()
    for u in (page, sitemap):
        if urlparse(u).scheme not in ("http", "https"):
            return jsonify(error="Enter valid http(s) URLs."), 400
    provider = d.get("provider", "anthropic")
    base_url, model, api_key = (d.get("base_url") or "").strip(), (d.get("model") or "").strip(), None  # keys come from .env only
    if provider not in il.PROVIDERS:
        return jsonify(error="Unknown provider."), 400
    if base_url and urlparse(base_url).scheme not in ("http", "https"):
        return jsonify(error="Base URL must start with http:// or https://"), 400
    if provider == "custom" and not (base_url and model):
        return jsonify(error="Custom provider needs a Base URL and a Model name."), 400
    stop_event.clear()
    job.update(state="running", log=[], rows=[], error=None, page=page, title="", blocks=[], result=[])
    threading.Thread(target=run, args=(page, sitemap, provider, int(d.get("max_candidates") or 40),
                                       api_key or None, base_url or None, model or None,
                                       bool(d.get("fetch_titles", True)), bool(d.get("refresh"))), daemon=True).start()
    return jsonify(ok=True)


@app.post("/test")
def test():
    d = request.get_json(force=True)
    provider = d.get("provider", "anthropic")
    if provider not in il.PROVIDERS:
        return jsonify(ok=False, message="Unknown provider."), 400
    ok, msg = il.test_connection(provider, (d.get("model") or "").strip() or None, None, (d.get("base_url") or "").strip() or None)
    return jsonify(ok=ok, message=msg)


@app.post("/stop")
def stop():
    if job["state"] == "running":
        stop_event.set()
        job["state"] = "stopping"
        log("Stopping...")
    return jsonify(ok=True)


@app.get("/status")
def status():
    return jsonify(state=job["state"], log=job["log"], rows=job["rows"], headers=HEADERS, error=job["error"])


@app.get("/download/<fmt>")
def download(fmt):
    if not job["rows"]:
        return "No results yet.", 404
    if fmt == "docx":
        data = build_docx(job["title"], job["page"], job["blocks"], job["result"])
        return send_file(io.BytesIO(data), as_attachment=True, download_name="page_with_suggested_links.docx",
                         mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    if fmt == "xlsx":
        wb = Workbook()
        ws = wb.active
        ws.title = "Internal links"
        ws.append(HEADERS)
        for r in job["rows"]:
            ws.append(r)
        for col, w in zip("ABCDEFG", (45, 40, 14, 12, 28, 55, 60)):
            ws.column_dimensions[col].width = w
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return send_file(buf, as_attachment=True, download_name="internal_link_suggestions.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADERS)
    w.writerows(job["rows"])
    return send_file(io.BytesIO(buf.getvalue().encode("utf-8-sig")), as_attachment=True,
                     download_name="internal_link_suggestions.csv", mimetype="text/csv")


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Internal Link Suggester</title>
<style>
:root{--bg:#fff;--fg:#1c1c1e;--mut:#6b7280;--bd:#e5e7eb;--ac:#2563eb;--rd:#dc2626}
@media(prefers-color-scheme:dark){:root{--bg:#16181d;--fg:#e8e8ea;--mut:#9ca3af;--bd:#2d3139}}
body{font:15px system-ui,sans-serif;background:var(--bg);color:var(--fg);max-width:1000px;margin:0 auto;padding:24px}
label{display:block;font-weight:600;margin:14px 0 4px}
input,select{width:100%;padding:9px;border:1px solid var(--bd);border-radius:6px;background:transparent;color:inherit;box-sizing:border-box}
.row{display:flex;gap:12px}.row>div{flex:1}
button{padding:9px 18px;border:0;border-radius:6px;color:#fff;font-weight:600;cursor:pointer;margin:16px 8px 0 0}
button:disabled{opacity:.4;cursor:not-allowed}
#start{background:var(--ac)}#stop{background:var(--rd)}#csv,#xlsx,#docx{background:#059669}
#log{background:rgba(127,127,127,.1);border-radius:6px;padding:10px;margin-top:16px;min-height:40px;white-space:pre-wrap;font-size:13px}
.tw{overflow-x:auto;margin-top:16px}table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border:1px solid var(--bd);padding:6px 8px;text-align:left;vertical-align:top}
#st{color:var(--mut);margin-top:12px}
</style></head><body>
<h2>Internal Link Suggester</h2><p><a href="/bulk">Whole-site mode (100,000s of pages) &rarr;</a></p>
<label>Page URL</label><input id="page" placeholder="https://example.com/blog/my-post">
<label>Sitemap URL</label><input id="sitemap" placeholder="https://example.com/sitemap.xml">
<div class="row"><div><label>AI provider</label><select id="provider">
<option value="anthropic">Claude (Anthropic)</option><option value="openai">OpenAI</option>
<option value="nvidia">NVIDIA (free API)</option><option value="openrouter">OpenRouter</option>
<option value="groq">Groq</option><option value="gemini">Google Gemini</option>
<option value="together">Together AI</option><option value="deepseek">DeepSeek</option>
<option value="mistral">Mistral</option><option value="ollama">Ollama (local)</option>
<option value="custom">Custom (any OpenAI-compatible API)</option></select></div>
<div><label>Model</label><input id="model" placeholder="model name"></div></div>
<div class="row"><div id="bu-wrap"><label>Base URL</label><input id="bu" placeholder="https://api.example.com/v1"></div>
<div><label>Max candidate pages</label><input id="mc" type="number" value="30" min="5" max="100"></div></div>
<label style="font-weight:400;margin-top:10px"><input id="ft" type="checkbox" checked style="width:auto"> Fetch candidate page titles (more accurate, slightly slower)</label>
<label style="font-weight:400;margin-top:4px"><input id="rf" type="checkbox" style="width:auto"> Re-download sitemap (otherwise cached for 6 hours)</label>
<div id="testout" style="margin-top:8px;font-size:13px"></div>
<div id="keynote" style="color:var(--mut);font-size:12px;margin-top:6px"></div>
<button id="test" style="background:#6b7280">Test AI connection</button><button id="start">Start</button><button id="stop" disabled>Stop</button>
<button id="csv" disabled>Download CSV</button><button id="xlsx" disabled>Download XLSX</button><button id="docx" disabled>Download Word (page + links)</button>
<div id="st"></div><div id="log"></div>
<div class="tw"><table id="tbl" hidden><thead></thead><tbody></tbody></table></div>
<script>
const $=id=>document.getElementById(id);let timer=null;
const PRESETS=__PRESETS__;const KEYS=__KEYS__;
function fill(){const p=PRESETS[$('provider').value]||['',''];$('bu').value=p[0];$('model').value=p[1];$('bu-wrap').hidden=$('provider').value==='anthropic';const v=$('provider').value;$('keynote').textContent=KEYS[v]?'API key found in server .env / environment.':(v==='ollama'?'No key needed for local Ollama.':'No API key found for this provider. Add it to your .env file (see README) and restart the app.')}
$('provider').onchange=fill;fill();
async function post(u,b){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});return r.json()}
$('start').onclick=async()=>{
  const r=await post('/start',{page:$('page').value,sitemap:$('sitemap').value,provider:$('provider').value,base_url:$('bu').value,model:$('model').value,max_candidates:$('mc').value,fetch_titles:$('ft').checked,refresh:$('rf').checked});
  if(r.error){alert(r.error);return}
  clearInterval(timer);timer=setInterval(poll,1000);poll();
};
$('stop').onclick=()=>post('/stop');
$('test').onclick=async()=>{$('test').disabled=true;$('testout').textContent='Testing...';
  try{const r=await post('/test',{provider:$('provider').value,base_url:$('bu').value,model:$('model').value});
    $('testout').textContent=(r.ok?'OK: ':'Failed: ')+r.message;$('testout').style.color=r.ok?'#059669':'#dc2626'}
  catch(e){$('testout').textContent='Failed: '+e}$('test').disabled=false};
$('csv').onclick=()=>location='/download/csv';$('xlsx').onclick=()=>location='/download/xlsx';$('docx').onclick=()=>location='/download/docx';
async function poll(){
  const s=await (await fetch('/status')).json();
  const busy=s.state==='running'||s.state==='stopping';
  $('start').disabled=busy;$('stop').disabled=s.state!=='running';
  const has=s.rows.length>0;$('csv').disabled=$('xlsx').disabled=$('docx').disabled=!has;
  $('st').textContent='Status: '+s.state+(s.error?' - '+s.error:'');$('log').textContent=s.log.join('\\n');
  const t=$('tbl');t.hidden=!has;
  if(has){
    t.tHead.innerHTML='';const hr=t.tHead.insertRow();
    s.headers.forEach(h=>{const th=document.createElement('th');th.textContent=h;hr.appendChild(th)});
    t.tBodies[0].innerHTML='';
    s.rows.forEach(r=>{const tr=t.tBodies[0].insertRow();r.forEach(c=>{tr.insertCell().textContent=c})});
  }
  if(!busy)clearInterval(timer);
}
poll();
</script></body></html>"""


@app.get("/favicon.ico")
def favicon():
    return "", 204


@app.get("/")
def index():
    import json
    presets = {"anthropic": ["", "claude-sonnet-5-5"], "custom": ["", ""]}
    presets.update({k: [v[0], v[1]] for k, v in il.PRESETS.items()})
    env = os.environ.get
    keys = {"anthropic": bool(env("ANTHROPIC_API_KEY") or env("LLM_API_KEY")), "custom": bool(env("LLM_API_KEY")),
            "ollama": True}
    keys.update({k: bool(env(v[2]) or env("LLM_API_KEY")) for k, v in il.PRESETS.items() if v[2]})
    return PAGE.replace("__PRESETS__", json.dumps(presets)).replace("__KEYS__", json.dumps(keys))


# ---------------- whole-site (bulk) mode ----------------
bulk = bk.Bulk()

BULK_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Whole-site link suggestions</title>
__CSS__<style>progress{width:100%;height:14px}.bar{margin-top:10px;font-size:13px}#ai{display:none}
.chk{display:flex;gap:8px;align-items:flex-start;font-weight:400;margin-top:12px}.chk input{width:auto;margin-top:3px}
.warn{background:rgba(217,119,6,.12);border-radius:6px;padding:8px 10px;font-size:13px;margin-top:10px}</style></head><body>
<h2>Whole-site link suggestions</h2><p><a href="/">&larr; Single page mode</a></p>
<label>Sitemap URL</label><input id="sitemap" placeholder="https://example.com/sitemap.xml">
<div class="row"><div><label>Max pages (blank = all)</label><input id="maxp" type="number" min="1" placeholder="e.g. 500 for a trial"></div>
<div><label>Download workers</label><input id="workers" type="number" value="10" min="1" max="50"></div>
<div><label>Max requests/sec to your site</label><input id="rate" type="number" value="5" min="0.1" step="0.5"></div></div>
<div class="row"><div><label>Suggested targets per page</label><input id="topk" type="number" value="5" min="1" max="10"></div>
<div><label>Max suggested links to one target</label><input id="inl" type="number" value="100" min="1"></div>
<div><label>Mode</label><select id="mode"><option value="fast">Fast - no AI (free)</option><option value="ai">Fast + AI refinement (uses your API)</option></select></div></div>
<div id="ai"><div class="row"><div><label>AI provider</label><select id="provider">__OPTS__</select></div>
<div><label>Model</label><input id="model"></div><div id="buw"><label>Base URL</label><input id="bu"></div></div>
<div class="row"><div><label>Pages to send to AI (this run)</label><input id="aip" type="number" value="200" min="1"></div>
<div><label>AI requests/min</label><input id="rpm" type="number" value="30" min="1"></div>
<div><label>AI parallel calls</label><input id="aiw" type="number" value="3" min="1" max="20"></div></div>
<div id="keynote" style="color:var(--mut);font-size:12px;margin-top:6px"></div>
<div class="warn">AI mode makes about one API call per page (up to the number above). Free tiers are rate-limited and paid APIs charge per call, so keep this number modest and check your provider's pricing.</div>
<label class="chk"><input id="conf" type="checkbox"> I understand AI mode uses my API quota / may cost money.</label></div>
<button id="start">Start / Resume</button><button id="stop" disabled>Stop</button>
<button id="csv" disabled>Download CSV</button><button id="xlsx" disabled>Download XLSX</button>
<button id="reset" style="background:#6b7280">Clear data</button>
<div id="st" style="margin-top:12px;color:var(--mut)"></div>
<div class="bar">Pages downloaded <span id="l1"></span><progress id="p1" value="0" max="1"></progress></div>
<div class="bar">Pages matched <span id="l2"></span><progress id="p2" value="0" max="1"></progress></div>
<div class="bar" id="aibar">Pages refined by AI <span id="l3"></span></div>
<div id="log" style="max-height:220px;overflow:auto"></div>
<h3 style="margin-top:22px">Word copy of one page</h3>
<div class="row"><div style="flex:3"><input id="wurl" placeholder="https://example.com/page-from-your-sitemap"></div><div><button id="wdl" style="margin:0">Download Word</button></div></div>
<h3 style="margin-top:22px">Latest suggestions (preview of 25; download for all)</h3>
<div class="tw"><table id="tbl"><thead><tr><th>Source page</th><th>Target URL</th><th>Anchor</th><th>Method</th></tr></thead><tbody></tbody></table></div>
<script>
const $=id=>document.getElementById(id);let timer=null;const PRESETS=__PRESETS__,KEYS=__KEYS__;
const fmt=n=>n.toLocaleString();
function fill(){const v=$('provider').value,p=PRESETS[v]||['',''];$('bu').value=p[0];$('model').value=p[1];$('buw').hidden=v==='anthropic';
 $('keynote').textContent=KEYS[v]?'API key found in server .env / environment.':(v==='ollama'?'No key needed for local Ollama.':'No API key found for this provider. Add it to .env and restart the app.')}
$('provider').onchange=fill;fill();
$('mode').onchange=()=>{$('ai').style.display=$('mode').value==='ai'?'block':'none'};
async function post(u,b){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});return r.json()}
$('start').onclick=async()=>{const r=await post('/bulk/start',{sitemap:$('sitemap').value,max_pages:$('maxp').value,workers:$('workers').value,rate:$('rate').value,
 top_k:$('topk').value,max_inlinks:$('inl').value,ai:$('mode').value==='ai',provider:$('provider').value,model:$('model').value,base_url:$('bu').value,
 ai_pages:$('aip').value,rpm:$('rpm').value,ai_workers:$('aiw').value,confirm:$('conf').checked});
 if(r.error){alert(r.error);return}clearInterval(timer);timer=setInterval(poll,1500);poll()};
$('stop').onclick=()=>post('/bulk/stop');
$('csv').onclick=()=>location='/bulk/download/csv';$('xlsx').onclick=()=>location='/bulk/download/xlsx';
$('wdl').onclick=()=>{if($('wurl').value)location='/bulk/docx?url='+encodeURIComponent($('wurl').value)};
$('reset').onclick=async()=>{if(confirm('Delete all stored crawl data and suggestions?')){const r=await post('/bulk/reset');if(r.error)alert(r.error);poll()}};
async function poll(){const s=await (await fetch('/bulk/status')).json(),c=s.counts;
 $('start').disabled=s.running;$('stop').disabled=!s.running;$('csv').disabled=$('xlsx').disabled=c.suggestions===0;
 $('st').textContent='Stage: '+s.stage+(s.error?' - '+s.error:'')+'  |  suggestions so far: '+fmt(c.suggestions);
 const done=c.ok+c.failed+c.skipped;$('p1').max=Math.max(c.total,1);$('p1').value=done;
 $('l1').textContent=fmt(done)+' / '+fmt(c.total)+' (ok '+fmt(c.ok)+', failed '+fmt(c.failed)+', skipped '+fmt(c.skipped)+')';
 $('p2').max=Math.max(c.ok,1);$('p2').value=c.matched;$('l2').textContent=fmt(c.matched)+' / '+fmt(c.ok);
 $('l3').textContent=fmt(c.ai_done);$('log').textContent=s.log.join('\\n');
 const tb=$('tbl').tBodies[0];tb.innerHTML='';s.preview.forEach(r=>{const tr=tb.insertRow();r.forEach(v=>{tr.insertCell().textContent=v})});
 if(!s.running)clearInterval(timer)}
poll();
</script></body></html>"""


def _bulk_int(d, key, default, lo, hi):
    try:
        v = d.get(key)
        return default if v in (None, "") else max(lo, min(hi, int(float(v))))
    except (TypeError, ValueError):
        return default


@app.get("/bulk")
def bulk_page():
    css = PAGE[PAGE.index("<style>"):PAGE.index("</style>") + 8]
    presets = {"anthropic": ["", "claude-sonnet-5-5"], "custom": ["", ""]}
    presets.update({k: [v[0], v[1]] for k, v in il.PRESETS.items()})
    env = os.environ.get
    keys = {"anthropic": bool(env("ANTHROPIC_API_KEY") or env("LLM_API_KEY")), "custom": bool(env("LLM_API_KEY")), "ollama": True}
    keys.update({k: bool(env(v[2]) or env("LLM_API_KEY")) for k, v in il.PRESETS.items() if v[2]})
    labels = {"anthropic": "Claude (Anthropic)", "openai": "OpenAI", "nvidia": "NVIDIA (free API)", "openrouter": "OpenRouter",
              "groq": "Groq", "gemini": "Google Gemini", "together": "Together AI", "deepseek": "DeepSeek",
              "mistral": "Mistral", "ollama": "Ollama (local)", "custom": "Custom (OpenAI-compatible)"}
    opts = "".join(f'<option value="{p}">{labels[p]}</option>' for p in il.PROVIDERS)
    return (BULK_PAGE.replace("__CSS__", css).replace("__OPTS__", opts)
            .replace("__PRESETS__", json.dumps(presets)).replace("__KEYS__", json.dumps(keys)))


@app.post("/bulk/start")
def bulk_start():
    if bulk.running:
        return jsonify(error="A whole-site job is already running."), 409
    d = request.get_json(force=True)
    sitemap = (d.get("sitemap") or "").strip()
    if urlparse(sitemap).scheme not in ("http", "https"):
        return jsonify(error="Enter a valid sitemap URL."), 400
    provider, base_url, model = d.get("provider", "anthropic"), (d.get("base_url") or "").strip(), (d.get("model") or "").strip()
    ai = bool(d.get("ai"))
    if ai:
        if not d.get("confirm"):
            return jsonify(error="Tick the box confirming you understand AI mode uses API quota."), 400
        if provider not in il.PROVIDERS:
            return jsonify(error="Unknown provider."), 400
        if provider == "custom" and not (base_url and model):
            return jsonify(error="Custom provider needs a Base URL and a Model name."), 400
        if base_url and urlparse(base_url).scheme not in ("http", "https"):
            return jsonify(error="Base URL must start with http:// or https://"), 400
    opts = dict(max_pages=_bulk_int(d, "max_pages", None, 1, 10_000_000), workers=_bulk_int(d, "workers", 10, 1, 50),
                rate=float(d.get("rate") or 5), top_k=_bulk_int(d, "top_k", 5, 1, 10),
                max_inlinks=_bulk_int(d, "max_inlinks", 100, 1, 1_000_000), min_score=0.03, ai=ai, provider=provider,
                model=model or None, base_url=base_url or None, ai_pages=_bulk_int(d, "ai_pages", 200, 1, 10_000_000),
                rpm=_bulk_int(d, "rpm", 30, 1, 10_000), ai_workers=_bulk_int(d, "ai_workers", 3, 1, 20))
    bulk.running = True
    threading.Thread(target=bulk.run, args=(sitemap, opts), daemon=True).start()
    return jsonify(ok=True)


@app.post("/bulk/stop")
def bulk_stop():
    bulk.stop.set()
    return jsonify(ok=True)


@app.post("/bulk/reset")
def bulk_reset():
    if bulk.running:
        return jsonify(error="Stop the running job first."), 409
    bulk.reset()
    return jsonify(ok=True)


@app.get("/bulk/status")
def bulk_status():
    return jsonify(running=bulk.running, stage=bulk.stage, error=bulk.error, counts=bulk.counts(),
                   log=bulk.logs[-30:], preview=bulk.preview())


@app.get("/bulk/download/<fmt>")
def bulk_download(fmt):
    if bulk.counts()["suggestions"] == 0:
        return "No suggestions yet.", 404
    if fmt == "xlsx":
        d = tempfile.mkdtemp()
        files = bulk.export_xlsx(d)
        if len(files) == 1:
            return send_file(files[0], as_attachment=True, download_name="internal_link_suggestions.xlsx")
        z = os.path.join(d, "internal_link_suggestions_xlsx_parts.zip")
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                zf.write(f, os.path.basename(f))
        return send_file(z, as_attachment=True, download_name="internal_link_suggestions_xlsx_parts.zip")

    def gen():
        buf = io.StringIO()
        w = csv.writer(buf)
        buf.write("\ufeff")
        w.writerow(bk.HEAD)
        for i, r in enumerate(bulk.iter_rows(), 1):
            w.writerow(r)
            if i % 2000 == 0:
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()
        yield buf.getvalue()

    return Response(gen(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=internal_link_suggestions.csv"})


@app.get("/bulk/docx")
def bulk_docx():
    url = (request.args.get("url") or "").strip()
    rows = bulk.rows_for(url)
    if not rows:
        return "No suggestions stored for that URL (use the exact URL from your sitemap).", 404
    grouped = {}
    for t, ty, p, a, s, r in rows:
        g = grouped.setdefault(t, {"target_url": t, "reason": r, "options": []})
        g["options"].append({"type": ty, "paragraph": p, "anchor_text": a, "suggested_sentence": s})
    try:
        title, _, _, blocks = il.extract_page_full(url)
    except Exception as e:
        return f"Could not load the page: {e}", 502
    data = build_docx(title, url, blocks, list(grouped.values()))
    return send_file(io.BytesIO(data), as_attachment=True, download_name="page_with_suggested_links.docx",
                     mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
