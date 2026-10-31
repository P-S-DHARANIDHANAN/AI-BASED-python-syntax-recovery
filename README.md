# Parsely — AI-Based Python Syntax Error Recovery System

A browser-based tool where you write Python and get its **syntax errors explained
in plain English**, with the error underlined in **red** in the editor and the
proposed fix shown in **green** right beside it. A rule-based engine detects and
locates errors and recovers the program structure; **Google's Gemini AI writes
the natural-language explanations, the beginner hints, and performs the
intent-aware autocorrect**.

---

## 1. What makes it "AI-based" (the honest split)

The project uses **two cooperating layers**, each doing what it is best at:

| Layer | Technology | Job | Why it owns that job |
|-------|-----------|-----|----------------------|
| **Detection + recovery** | Hand-written tokenizer + parser (Python, deterministic) | Find *what* the error is, *where* it is (line/column), and repair structure so parsing can continue | Fast, offline, and never hallucinates a wrong location — so it is trusted for the red underlines and the recovery loop |
| **Explanation + autocorrect** | **Gemini AI** | Write the plain-English explanation, the guiding hint, and the smart, intent-aware corrected code | An LLM is far better at human-sounding language and at guessing the programmer's *intent* when fixing |

If no Gemini key is configured, the AI layer **degrades gracefully** to built-in
template explanations, so the app always runs for a demo.

---

## 2. Project structure

```
parsely/
├── backend/
│   ├── app.py                 # Flask server: serves the UI + the JSON API
│   ├── parser_engine.py       # FROM-SCRATCH tokenizer + parser + recovery loop (no CPython, no AI)
│   ├── ai_assistant.py        # Gemini calls: explanations, hints, autocorrect (+ fallback)
│   ├── templates_fallback.py  # deterministic explanations used when AI is off/unreachable
│   ├── config.py              # loads GEMINI_API_KEY + model from .env (never hard-coded)
│   ├── requirements.txt
│   └── .env.example           # copy to .env and paste your key
└── frontend/
    ├── index.html             # the single-page editor UI
    ├── style.css              # styling, light/dark themes, the red/green overlay
    └── app.js                 # editor, live overlay, calls the backend API
```

---

## 3. Setup (about 2 minutes)

**Requirements:** Python 3.9+ and pip.

```bash
cd parsely/backend

# 1. install dependencies
pip install -r requirements.txt

# 2. add your Gemini API key
cp .env.example .env
#    then open .env and paste your key:
#    GEMINI_API_KEY=AIza....your_key_here
#    (get a free key at https://aistudio.google.com/apikey)

# 3. run
python app.py
```

Open **http://localhost:5000/** in your browser.

> No key yet? It still runs — you'll see `AI: off (rules)` in the top bar and the
> built-in explanations are used instead. Add the key and restart to switch the
> explanations and autocorrect to Gemini.

---

## 4. How it works — the flow from keystroke to explanation

```
        ┌─────────────────────────── BROWSER (frontend/) ───────────────────────────┐
        │                                                                             │
 you    │   <textarea>  ──type──▶  debounce 350ms  ──▶  POST /api/analyze {code}      │
 type ──┼──▶                                                   │                       │
        │   ◀── red underlines + green ghost suggestions + explanation cards ◀──┐     │
        └──────────────────────────────────────────────────────────────────────┼─────┘
                                                                                 │ JSON
        ┌─────────────────────────── SERVER (backend/) ──────────────────────────┼────┐
        │                                                                         │    │
        │   parser_engine.analyze(code)        [HEURISTIC, deterministic]         │    │
        │      1. tokenize()      → tokens                                        │    │
        │      2. first_hard_error() → earliest real error + its line/column      │    │
        │      3. apply a safe repair to a COPY, re-parse, repeat  ◀── recovery   │    │
        │         loop: finds MANY real errors in one pass, not just the first    │    │
        │      → errors[], recovered_code, changed_lines                          │    │
        │                                                                         │    │
        │   ai_assistant.explain_errors(code, errors)   [GEMINI AI]               │    │
        │      → for each error: natural-language explanation + hint + fix  ──────┘    │
        │        (falls back to templates_fallback.py if the key/network fails)        │
        └──────────────────────────────────────────────────────────────────────────┘
```

Step by step:

1. **You type** in the editor. Each keystroke restarts a ~350 ms timer
   (*debounce*) — the "real-time" feel is this timer, not threads. When you
   pause, the frontend sends the whole program to `POST /api/analyze`.
2. **Tokenizer (lexical analysis).** `parser_engine.tokenize()` scans the text
   character by character into *tokens* (keywords, names, numbers, strings,
   brackets, operators). Strings and comments become single tokens so quotes and
   `#` never confuse later stages.
3. **Parser (syntax analysis).** Tokens are grouped into logical lines and
   checked against syntax rules (missing colon, unclosed bracket, unterminated
   string, bad indentation, Python-2 `print`, `=` vs `==`, misspelled keyword,
   missing comma…). The earliest genuine error is returned **with its exact line
   and column**.
4. **Recovery loop (the key novelty).** Instead of stopping at the first error
   (what CPython does), the engine applies a safe structural repair to a *copy*,
   re-parses, finds the next genuine error, and repeats. One mistake therefore
   stops hiding the rest — several real errors surface in a single pass.
5. **AI explanation layer.** The list of located errors is handed to Gemini,
   which returns, for each one, a short plain-English explanation, a guiding
   hint (phrased as a question for "Hint only" mode), and a one-line fix. This is
   the natural-language generation the title refers to. If Gemini is
   unavailable, deterministic templates fill in instead.
6. **The browser renders two things at once:**
   - **In the editor** — a transparent overlay (a "mirror" layer behind the
     text, kept in sync as you scroll) underlines the wrong characters in **red**
     and places a **green** suggestion exactly where the fix goes (a green `:`,
     a closing bracket, or a chip like `➜ print(...)`). The gutter shows a
     red/amber dot per affected line.
   - **In the side panel** — one card per error with the AI explanation, the fix,
     and a "why this happens" note.
7. **Autocorrect** (`POST /api/autocorrect`) asks Gemini to rewrite the code to
   valid Python 3, changing as little as possible. With **Auto-fix** toggled on,
   this runs automatically after you pause — but never rewrites the line your
   cursor is on, so it won't fight you while you type. Fallback here is the
   deterministic recovered code from the rule engine.

---

## 5. Where the data is stored

Deliberately almost nowhere:

- While you work, your code lives **in memory** on the page (a JavaScript string)
  and is sent to the local backend only to be analyzed.
- The browser's **localStorage** keeps a convenience copy of your last code,
  theme, and the auto-fix setting, so a refresh doesn't lose your work. This
  stays in *your* browser.
- Your Gemini key lives only in the backend's **`.env`** file on your machine and
  is never sent to the browser or committed to the project.
- The only outbound network call is from **your backend to the Gemini API**, and
  only to generate explanations/corrections. Nothing is stored in a database.

---

## 6. API reference

| Method & path | Body | Returns |
|---|---|---|
| `GET /api/health` | — | `{ai_available, model}` |
| `POST /api/analyze` | `{code}` | `{findings[], recovered, changed_lines, error_count, warning_count, ai_available}` |
| `POST /api/autocorrect` | `{code}` | `{corrected, source}` where `source` is `"ai"` or `"rules"` |

Each item in `findings` carries the rule id, line/column (for the red/green
marks), severity, and the natural-language `title` / `explanation` / `hint` /
`fix` / `suggestion`.

---

## 7. Honest limitations (good to state in a review)

- It checks **syntax structure**, not runtime or logic errors (undefined names,
  type errors, wrong results).
- The detector is rule-based, so extremely unusual constructs could slip past;
  the structural repairs are labelled **best-guess** for that reason.
- AI explanations need network + a valid key and quota; the fallback keeps the
  tool usable without them.
