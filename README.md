# auto-link-suggest

Suggests internal links for a page. Give it a page URL and your sitemap URL; an AI model
(Claude, OpenAI, NVIDIA, or any OpenAI-compatible API) picks relevant sitemap pages and proposes up to 2 anchor texts per
target URL, taken from body paragraphs only (never headings). Irrelevant URLs are skipped.

Each suggestion is either:
- `existing_text` - a phrase already in the paragraph that can be linked
- `add_text` - a short sentence to add, containing the anchor to link

## Project layout

```
auto-link-suggest/
├── app.py              # web front end (Flask): start/stop, results table, CSV/XLSX/Word download
├── internal_links.py   # core logic + command-line tool
├── docx_export.py      # builds the Word copy of the page with links placed
├── requirements.txt
├── .env.example        # copy to .env and add your API key(s)
└── README.md
```

## Setup

```bash
cd auto-internal-link-suggester
py -m venv .venv
source .venv/bin/activate # Windows: source .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env # then edit .env and add your key(s). Remember that .env.example is just an example file copy it as .env in project root folder to add Secret Keys
```

Add to .gitignore file to avoid db or other files being pushed to github 
.env*
.linkcache.json
suggestions.json
__pycache__/
.venv/
.sitemap_cache.json
data/*

## Run the web app

```bash
python app.py
```
Open http://127.0.0.1:5000, enter the Page URL and Sitemap URL, choose a provider use a **large model** or rate limits cut off requests, Test Connection and press **Start**. If there is error change model and base URL
**Stop** takes effect when the current step finishes. When results appear, download them as CSV, XLSX, or **Word** (the full page content with the links placed in).

## If you want to Run it from the command line

```bash
python internal_links.py --page https://example.com/blog/post \
                         --sitemap https://example.com/sitemap.xml \
                         --provider anthropic        # or openai
```
Saves `suggestions.json`. Optional: `--model`, `--max-candidates 40`, `--out file.json`,
`--docx page.docx` (also writes the Word copy).

## CSV / XLSX columns

Target URL, Reason, Type, Paragraph #, Anchor text, Suggested sentence, Paragraph excerpt.

## Large sitemaps (tens of thousands of URLs)

Only the *page* is sent to the AI, never the whole sitemap. Steps and typical time:

1. Sitemap download (child sitemaps in parallel) - cached 6 hours, so repeat runs skip it.
2. Local relevance ranking of every URL slug against the page (about 1 second for 50,000 URLs).
3. Titles/descriptions fetched for only the top N candidates (default 30; untick the box in the app or use `--no-titles` to skip).
4. The AI checks the page in parts of 15 paragraphs, 3 parts at once, with automatic retries.

If your provider rate-limits you (error 429), set `LLM_CONCURRENCY=1` in `.env`. Failures are reported with a reason
(bad key, wrong model, rate limit, timeout, unreadable output); other parts still produce results.
Note: ranking is keyword-based on URL slugs, so pages with very non-descriptive URLs (e.g. `/p/1234`) can't be matched.

## Troubleshooting: "Request timed out"

The AI provider did not answer in time (default 60 s per request; change with `LLM_TIMEOUT` in `.env`).

1. Click **Test AI connection** in the app. It sends a tiny request and shows the response time, or the exact reason
   (rejected key, wrong model name with a list of available models, rate limit, timeout).
2. If the test also times out: the provider/model is overloaded or unreachable from your network. Try a smaller model
   (NVIDIA default is `meta/llama-3.1-8b-instruct`), another provider, turn off any VPN/proxy, or try again later.
3. If the test is fast but real runs time out: lower `LLM_CONCURRENCY` to 1 and/or the candidate count.

When the first parallel requests all fail, the run stops early with the reason instead of waiting on the rest.

## Using any AI provider / API key

API keys are **never entered in the web app**. Put them in your `.env` file (see `.env.example`) and restart.
In the web app pick a provider and adjust the Base URL and Model if needed; the page tells you whether a key
was found for the selected provider.

- Built-in: Claude, OpenAI, NVIDIA, OpenRouter, Groq, Google Gemini, Together AI, DeepSeek, Mistral, Ollama (local)
- **Custom**: any service with an OpenAI-compatible chat API. Set `LLM_API_KEY` in `.env`, then enter its Base URL
  (usually ending in `/v1`) and a model name in the app.
- Default model names are only suggestions and providers rename models often; edit the Model field if one is rejected.

Command line:
```bash
export LLM_API_KEY=your_key
python internal_links.py --page URL --sitemap URL --provider custom \
       --base-url https://api.example.com/v1 --model model-name
```

## Using NVIDIA's free API

1. Create a free key at https://build.nvidia.com (Get API Key; it starts with `nvapi-`).
2. Put it in `.env` as `NVIDIA_API_KEY`. Optionally set `NVIDIA_MODEL` (default `meta/llama-3.3-70b-instruct`).
3. Choose **NVIDIA (free API)** in the web app, or run with `--provider nvidia`.

The free tier is rate-limited (shown on your build.nvidia.com dashboard), which is fine for this tool.

## Word output

The .docx reproduces the page's headings, paragraphs and list items in order, with the suggestions placed in:

- blue underlined: links that already exist on the page
- green bold underlined: new suggested link on existing text
- yellow highlight: new sentence to add (appended to its paragraph, with the anchor linked)

A summary table at the end lists every anchor, target URL, and whether it could be placed. Headings are never linked.
Images, tables and other page elements are not reproduced.

## Notes

- Reads server-rendered HTML only; JavaScript-rendered pages need a headless browser added.
- Candidate page titles/descriptions are cached in `.linkcache.json` to speed up repeat runs.
- One job at a time; results are held in memory until the next run.
- The app binds to `127.0.0.1` only. Add authentication before exposing it on a network.
