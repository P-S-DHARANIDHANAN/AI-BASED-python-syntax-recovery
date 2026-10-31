"""
app.py
======
The Flask web server that ties Parsely together and serves the frontend.

Routes
------
GET  /                -> the single-page editor (frontend/index.html)
GET  /<static files>  -> style.css, app.js, etc.
GET  /api/health      -> {"ai_available": bool, "model": str}  (frontend reads this)
POST /api/analyze     -> body {"code": "..."}:
                          runs the heuristic engine, then asks the AI to explain
                          every error. Returns findings + recovered code.
POST /api/autocorrect -> body {"code": "..."}:
                          AI-assisted correction (falls back to rule recovery).

The server is deliberately simple so it runs with a single `python app.py`.
"""

import os

from flask import Flask, jsonify, request, send_from_directory

import config
import parser_engine
import ai_assistant

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "frontend")

app = Flask(__name__, static_folder=None)

# CORS is harmless here (frontend is same-origin) but handy if served separately.
try:
    from flask_cors import CORS
    CORS(app)
except Exception:
    pass


# --------------------------------------------------------------------------- #
# Static frontend
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(FRONTEND_DIR, filename)


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "ai_available": ai_assistant.is_ai_available(),
        "model": config.GEMINI_MODEL if ai_assistant.is_ai_available() else None,
    })


@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request.get_json(silent=True) or {}
    code = data.get("code", "")
    if not isinstance(code, str):
        return jsonify({"error": "code must be a string"}), 400

    src_lines = code.split("\n")

    # 1) deterministic detection + recovery (heuristic engine)
    result = parser_engine.analyze(code)

    # merge hard errors + advisory hints into one list, ordered by position
    diagnostics = result["errors"] + result["hints"]
    diagnostics.sort(key=lambda e: (e.get("line", 0), e.get("col", 0)))

    # 2) AI (or template fallback) natural-language layer
    nl = ai_assistant.explain_errors(code, diagnostics, src_lines)

    findings = []
    for e, text in zip(diagnostics, nl):
        findings.append({
            # location + machine facts from the rule engine (drives red/green marks)
            "rule": e["rule"],
            "line": e["line"],
            "col": e["col"],
            "end_col": e.get("end_col", e["col"] + 1),
            "severity": e.get("severity", "error"),
            # extra positional facts the frontend uses to place green ghosts
            "opener_line": e.get("opener_line"),
            "header_line": e.get("header_line"),
            "before_end_col": e.get("before_end_col"),
            "ch": e.get("ch"),
            "needs": e.get("needs"),
            "expected": e.get("expected"),
            "quote": e.get("quote"),
            "suggest": e.get("suggest"),
            # natural language from the AI (or templates)
            "title": text["title"],
            "explanation": text["explanation"],
            "hint": text["hint"],
            "fix": text["fix"],
            "suggestion": text.get("suggestion", ""),
            "lesson": text.get("lesson", ""),
        })

    error_count = sum(1 for e in diagnostics if e.get("severity") != "warning")

    return jsonify({
        "findings": findings,
        "recovered": result["recovered"],
        "changed_lines": result["changed_lines"],
        "error_count": error_count,
        "warning_count": len(diagnostics) - error_count,
        "ai_available": ai_assistant.is_ai_available(),
        "nl_source": nl[0]["source"] if nl else "none",
    })


@app.route("/api/autocorrect", methods=["POST"])
def autocorrect():
    data = request.get_json(silent=True) or {}
    code = data.get("code", "")
    if not isinstance(code, str):
        return jsonify({"error": "code must be a string"}), 400

    # deterministic recovery is both the fallback and a safety net
    recovered = parser_engine.analyze(code)["recovered"]
    result = ai_assistant.autocorrect(code, deterministic_recovered=recovered)
    return jsonify(result)


if __name__ == "__main__":
    banner = "ON ({})".format(config.GEMINI_MODEL) if ai_assistant.is_ai_available() else "OFF (template fallback)"
    config.log("Parsely starting. AI explanations: {}".format(banner))
    config.log("Open http://localhost:{}/".format(config.PORT))
    app.run(host="0.0.0.0", port=config.PORT, debug=config.DEBUG)
