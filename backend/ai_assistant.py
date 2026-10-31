"""
ai_assistant.py
===============
The AI layer of Parsely, backed by Google's Gemini API.

Division of labour in this project:

    * parser_engine.py  (heuristic / deterministic) -> DETECTS errors and WHERE
      they are, and produces a safe structural repair. This is reliable, offline,
      and gives the editor the exact line/column for the red underlines.

    * ai_assistant.py   (this file, AI-powered)     -> EXPLAINS each error in
      natural language, writes the beginner HINT, and performs the intelligent
      AUTOCORRECT. This is the part the project title ("AI-Based...") refers to.

Why this split? The rule engine is fast and never hallucinates a wrong location,
so it is trusted for detection. Gemini is far better at human-sounding
explanations and at guessing the programmer's *intent* when correcting, so it is
trusted for language and for the smart fix. If the key is absent or a call
fails, every function degrades gracefully to the deterministic templates in
templates_fallback.py, so the app always works.

Only the standard library + `requests` are used, so there is no heavy SDK to
version-match; the REST endpoint is stable.
"""

import json
import re

import requests

import templates_fallback
import config


_ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/models/"
             "{model}:generateContent")


# --------------------------------------------------------------------------- #
# Low-level Gemini call
# --------------------------------------------------------------------------- #
def _call_gemini(prompt, want_json=False, temperature=0.2, timeout=25):
    """Send one prompt to Gemini and return the text response, or raise."""
    key = config.GEMINI_API_KEY
    if not key:
        raise RuntimeError("No GEMINI_API_KEY configured")

    url = _ENDPOINT.format(model=config.GEMINI_MODEL)
    gen_cfg = {"temperature": temperature, "maxOutputTokens": 2048}
    if want_json:
        gen_cfg["responseMimeType"] = "application/json"

    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": gen_cfg,
    }
    resp = requests.post(url, params={"key": key},
                         headers={"Content-Type": "application/json"},
                         data=json.dumps(body), timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    # pull the first text part out of the first candidate
    cand = (data.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError("Empty response from Gemini")
    return text


def _strip_code_fence(text):
    """Remove a leading ```python / ``` fence and trailing ``` if present."""
    t = text.strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t


def is_ai_available():
    return bool(config.GEMINI_API_KEY)


# --------------------------------------------------------------------------- #
# 1) Natural-language explanations + hints for a batch of errors
# --------------------------------------------------------------------------- #
_EXPLAIN_SYSTEM = """You are Parsely, a friendly Python tutor for absolute beginners.
A rule-based parser has already found the exact syntax errors below and their line numbers.
Do NOT re-detect or re-locate errors. For EACH error given, write a short, warm, plain-English explanation.

Return ONLY a JSON array. Each element must correspond, in order, to each input error and have exactly these keys:
  "title":       3-5 word label for the error
  "explanation": 1-2 sentences saying what is wrong and why, in plain language a first-week student understands. No jargon.
  "hint":        1 sentence phrased as a guiding QUESTION that nudges the student to the fix without giving it away.
  "fix":         1 sentence telling them exactly what to change.
  "suggestion":  the single corrected line of code if obvious, else an empty string. No markdown, no backticks.
Keep it encouraging and concrete. Never invent errors that are not in the list."""


def explain_errors(code, errors, src_lines):
    """Return a list (parallel to `errors`) of dicts:
        {title, explanation, hint, fix, suggestion, severity}
    Uses Gemini when available; falls back to templates per-error on any failure.
    """
    if not errors:
        return []

    # always compute the deterministic fallback first (also our safety net)
    fallback = [dict(templates_fallback.describe(e, src_lines),
                     source="rules") for e in errors]

    if not is_ai_available():
        return fallback

    # build a compact description of each error for the model
    listed = []
    for idx, e in enumerate(errors):
        ln = e["line"]
        raw = src_lines[ln - 1] if 0 <= ln - 1 < len(src_lines) else ""
        listed.append({
            "index": idx,
            "rule": e["rule"],
            "line": ln,
            "severity": e.get("severity", "error"),
            "line_text": raw,
            "detail": {k: v for k, v in e.items()
                       if k in ("keyword", "ch", "needs", "expected", "word",
                                "suggest", "opener_line", "quote")},
        })

    prompt = (_EXPLAIN_SYSTEM
              + "\n\nFULL CODE:\n'''\n" + code + "\n'''\n\nERRORS (JSON):\n"
              + json.dumps(listed, indent=2)
              + "\n\nRemember: return a JSON array of exactly "
              + str(len(errors)) + " objects, in the same order.")

    try:
        text = _call_gemini(prompt, want_json=True, temperature=0.25)
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            raise ValueError("expected a JSON array")
        out = []
        for idx, e in enumerate(errors):
            if idx < len(parsed) and isinstance(parsed[idx], dict):
                a = parsed[idx]
                out.append({
                    "title": a.get("title") or fallback[idx]["title"],
                    "explanation": a.get("explanation") or fallback[idx]["explanation"],
                    "hint": a.get("hint") or fallback[idx]["hint"],
                    "fix": a.get("fix") or fallback[idx]["fix"],
                    "suggestion": a.get("suggestion", "") or fallback[idx].get("suggestion", ""),
                    "lesson": fallback[idx].get("lesson", ""),
                    "severity": e.get("severity", "error"),
                    "source": "ai",
                })
            else:
                out.append(fallback[idx])
        return out
    except Exception as exc:          # network error, bad JSON, quota, etc.
        config.log("explain_errors fell back to templates: {}".format(exc))
        return fallback


# --------------------------------------------------------------------------- #
# 2) AI-assisted autocorrect
# --------------------------------------------------------------------------- #
_AUTOCORRECT_SYSTEM = """You are an expert Python developer fixing a beginner's SYNTAX errors.
Rewrite the code so it parses as valid Python 3, changing as LITTLE as possible.
Rules:
- Fix only syntax problems (missing colons/brackets/quotes/commas, bad indentation, Python-2 print, = vs ==, misspelled keywords).
- Preserve the author's intent, variable names, structure, comments and line count wherever you can.
- Do NOT add new logic, imports, or explanatory comments.
- Return ONLY the corrected Python code. No markdown fences, no prose, no explanation."""


def autocorrect(code, deterministic_recovered=None):
    """Return a dict:
        {"corrected": "<code>", "source": "ai" | "rules"}
    Prefers Gemini's intent-aware correction; falls back to the deterministic
    recovered code from the rule engine.
    """
    if not is_ai_available():
        return {"corrected": deterministic_recovered or code, "source": "rules"}

    prompt = _AUTOCORRECT_SYSTEM + "\n\nCODE TO FIX:\n'''\n" + code + "\n'''"
    try:
        text = _call_gemini(prompt, want_json=False, temperature=0.1)
        fixed = _strip_code_fence(text)
        if not fixed.strip():
            raise RuntimeError("empty correction")
        return {"corrected": fixed, "source": "ai"}
    except Exception as exc:
        config.log("autocorrect fell back to rules: {}".format(exc))
        return {"corrected": deterministic_recovered or code, "source": "rules"}
