"""
parser_engine.py
================
The deterministic, heuristic core of Parsely.

This module implements, FROM SCRATCH, the two front-end stages of a compiler for
Python source text:

    1. Lexical analysis  (tokenize)      -> a stream of tokens
    2. Syntax analysis   (firstHardError /
                          collect_hints)  -> structural errors

On top of those it adds an *error-recovery loop* (`analyze`): it finds the
earliest genuine error, applies a safe structural repair to a COPY of the code,
re-parses, and repeats. This is what lets Parsely report several real errors in
one pass instead of stopping at the first one (which is what CPython does).

Nothing in this file talks to a network or an AI model. It is fully
deterministic: the same input always produces the same errors and the same
recovered code. The natural-language explanations and the "smart" autocorrect
are layered on top of this by `ai_assistant.py`.

Returned error objects are plain dicts so they serialise straight to JSON for
the frontend:

    {
      "rule": "missing-colon",      # machine id of the error type
      "line": 3,                     # 1-based line
      "col": 17,                     # 1-based column where the mark starts
      "end_col": 18,                 # 1-based column where the mark ends
      "severity": "error",           # "error" | "warning"
      ...rule-specific extra fields (e.g. "keyword", "suggest", "needs")...
    }
"""

import re

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
KEYWORDS = {
    "False", "None", "True", "and", "as", "assert", "async", "await", "break",
    "class", "continue", "def", "del", "elif", "else", "except", "finally",
    "for", "from", "global", "if", "import", "in", "is", "lambda", "nonlocal",
    "not", "or", "pass", "raise", "return", "try", "while", "with", "yield",
}
BLOCK_KW = {"if", "elif", "else", "for", "while", "def", "class", "try",
            "except", "finally", "with", "async"}

# keywords beginners commonly misspell; used for the "did you mean" heuristic
TYPO_TARGETS = ["if", "elif", "else", "for", "while", "def", "class", "import",
                "from", "return", "print", "True", "False", "None", "try",
                "except", "finally", "with", "break", "continue", "lambda",
                "and", "or", "not", "in", "is"]

# multi-char operators first so the longest match wins
OPS = ["**=", "//=", ">>=", "<<=", "...", "!=", "==", ">=", "<=", "->", ":=",
       "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "@=", "**", "//", "<<",
       ">>", "&&", "+", "-", "*", "/", "%", "@", "&", "|", "^", "~", "<", ">",
       "=", "(", ")", "[", "]", "{", "}", ",", ":", ".", ";"]
OPS.sort(key=len, reverse=True)

OPENERS = {"(": ")", "[": "]", "{": "}"}
CLOSERS = {")": "(", "]": "[", "}": "{"}
BRACKET_NAME = {
    "(": "round bracket (", "[": "square bracket [", "{": "curly brace {",
    ")": "round bracket )", "]": "square bracket ]", "}": "curly brace }",
}

# which rules are advisory (shown amber as "suggestions") vs hard errors
WARNING_RULES = {"eq-in-cond", "keyword-typo", "missing-comma"}

_STR_PREFIX = re.compile(r"[rbfuRBFU]{0,3}(?=['\"])")
_NUMBER = re.compile(
    r"0[xXoObB][0-9a-fA-F_]+|[0-9][0-9_]*\.?[0-9_]*(?:[eE][+-]?[0-9]+)?[jJ]?|\.[0-9][0-9_]*"
)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# --------------------------------------------------------------------------- #
# Lexer
# --------------------------------------------------------------------------- #
def tokenize(src):
    """Turn source text into a flat list of token dicts.

    A whole string literal (including triple / f / r strings) is ONE token, so
    quotes and ``#`` inside strings never confuse later stages. A string that is
    never closed is still emitted, carrying ``unterminated=True``. A backslash
    before a newline is swallowed so the two physical lines join.
    """
    tokens = []
    i, line, col, n = 0, 1, 1, len(src)

    def adv(k):
        nonlocal i, line, col
        for _ in range(k):
            if i < n and src[i] == "\n":
                line += 1
                col = 1
            else:
                col += 1
            i += 1

    while i < n:
        c = src[i]

        # line continuation
        if c == "\\" and i + 1 < n and src[i + 1] == "\n":
            adv(2); continue
        if c == "\\" and src[i + 1:i + 3] == "\r\n":
            adv(3); continue
        if c == "\r":
            adv(1); continue
        if c == "\n":
            tokens.append({"type": "NEWLINE", "value": "\n", "line": line, "col": col})
            adv(1); continue
        if c in " \t":
            adv(1); continue
        if c == "#":
            s_line, s_col = line, col
            while i < n and src[i] != "\n":
                adv(1)
            tokens.append({"type": "COMMENT", "value": "#", "line": s_line, "col": s_col})
            continue

        # strings (with optional prefix like r, b, f, u)
        pm = _STR_PREFIX.match(src[i:i + 5])
        pfx = pm.group(0) if pm else ""
        q = src[i + len(pfx)] if i + len(pfx) < n else ""
        if q in ("'", '"'):
            s_line, s_col = line, col
            triple = src[i + len(pfx):i + len(pfx) + 3] == q * 3
            quote = q * 3 if triple else q
            adv(len(pfx) + len(quote))
            closed = False
            while i < n:
                if src[i] == "\\":
                    adv(2); continue
                if not triple and src[i] == "\n":
                    break
                if src[i:i + len(quote)] == quote:
                    adv(len(quote)); closed = True; break
                adv(1)
            tokens.append({"type": "STRING", "value": quote, "line": s_line,
                           "col": s_col, "end_line": line, "end_col": col,
                           "unterminated": not closed})
            continue

        # numbers
        if c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit()):
            s_line, s_col = line, col
            m = _NUMBER.match(src[i:])
            num = m.group(0) if m else c
            adv(len(num))
            tokens.append({"type": "NUMBER", "value": num, "line": s_line,
                           "col": s_col, "end_col": col})
            continue

        # names / keywords
        if c.isalpha() or c == "_":
            s_line, s_col = line, col
            name = _NAME.match(src[i:]).group(0)
            adv(len(name))
            tokens.append({"type": "KEYWORD" if name in KEYWORDS else "NAME",
                           "value": name, "line": s_line, "col": s_col,
                           "end_col": col})
            continue

        # operators / brackets
        matched = None
        for op in OPS:
            if src[i:i + len(op)] == op:
                matched = op; break
        if matched:
            s_line, s_col = line, col
            t = {"type": "OP", "value": matched, "line": s_line, "col": s_col}
            if matched in OPENERS:
                t["type"] = "OPEN"
            elif matched in CLOSERS:
                t["type"] = "CLOSE"
            adv(len(matched))
            t["end_col"] = col
            tokens.append(t)
            continue

        # unknown single char
        tokens.append({"type": "OP", "value": c, "line": line, "col": col, "end_col": col + 1})
        adv(1)

    return tokens


# --------------------------------------------------------------------------- #
# Logical-line grouping
# --------------------------------------------------------------------------- #
def _indent_width(ws):
    w = 0
    for ch in ws:
        w += (8 - (w % 8)) if ch == "\t" else 1
    return w


def build_logical(tokens, raw_lines):
    """Group real tokens into logical lines, honouring bracket depth so an
    expression spread across several physical lines stays one logical line."""
    groups, cur, depth = [], [], 0
    for t in tokens:
        if t["type"] == "NEWLINE":
            if depth <= 0 and cur:
                groups.append(cur); cur = []
            continue
        if t["type"] == "COMMENT":
            continue
        if t["type"] == "OPEN":
            depth += 1
        elif t["type"] == "CLOSE":
            depth = max(0, depth - 1)
        cur.append(t)
    if cur:
        groups.append(cur)

    out = []
    for toks in groups:
        first = toks[0]
        indent_text = re.match(r"^[ \t]*", raw_lines[first["line"] - 1]
                               if first["line"] - 1 < len(raw_lines) else "").group(0)
        out.append({
            "toks": toks, "first": first, "last": toks[-1],
            "indent": _indent_width(indent_text),
            "header_kw": first["value"] if first["value"] in BLOCK_KW else None,
        })
    return out


def _depth_zero_colon(toks):
    d = 0
    for idx, t in enumerate(toks):
        if t["type"] == "OPEN":
            d += 1
        elif t["type"] == "CLOSE":
            d = max(0, d - 1)
        elif d == 0 and t["type"] == "OP" and t["value"] == ":":
            return idx
    return -1


def _opens_block(ll):
    if not ll["header_kw"]:
        return False
    ci = _depth_zero_colon(ll["toks"])
    return ci == len(ll["toks"]) - 1 and ci >= 0


def _plain_assign_index(toks):
    d = 0
    for idx, t in enumerate(toks):
        if t["type"] == "OPEN":
            d += 1
        elif t["type"] == "CLOSE":
            d = max(0, d - 1)
        elif d == 0 and t["type"] == "OP" and t["value"] == "=":
            return idx
    return -1


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _mk(rule, line, col, end_col, **extra):
    e = {"rule": rule, "line": line, "col": col, "end_col": end_col,
         "severity": "warning" if rule in WARNING_RULES else "error"}
    e.update(extra)
    return e


def _edit_distance_1(a, b):
    """Return 1 if a and b are exactly one edit (insert/delete/substitute)
    apart, else 2 (meaning 'not close')."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return 2
    if la == lb:
        diff = 0
        for x, y in zip(a, b):
            if x != y:
                diff += 1
                if diff > 1:
                    return 2
        return diff
    s, lng = (a, b) if la < lb else (b, a)
    j = sk = 0
    for ch in lng:
        if j < len(s) and s[j] == ch:
            j += 1
        else:
            sk += 1
            if sk > 1:
                return 2
    return 1


def _keyword_typo(ll):
    first = ll["first"]
    if first["type"] != "NAME":
        return None
    w = first["value"]
    if len(w) < 2 or len(w) > 9 or w in KEYWORDS:
        return None
    best, bd = None, 99
    for kw in TYPO_TARGETS:
        if abs(len(kw) - len(w)) > 1:
            continue
        dd = _edit_distance_1(w, kw)
        if dd < bd:
            bd, best = dd, kw
    if bd != 1:
        return None
    toks = ll["toks"]
    looks_stmt = (len(toks) == 1 or _depth_zero_colon(toks) >= 0 or
                  (len(toks) > 1 and toks[1]["value"] not in ("=", "(", ".", "[")
                   and not re.match(r"^(\+=|-=|\*=|/=|%=)$", toks[1]["value"])))
    if not looks_stmt:
        return None
    return _mk("keyword-typo", first["line"], first["col"],
               first.get("end_col", first["col"] + len(w)), word=w, suggest=best)


def _is_value_tok(t):
    return (t["type"] in ("NUMBER", "STRING", "NAME") or
            (t["type"] == "KEYWORD" and t["value"] in ("True", "False", "None")))


# --------------------------------------------------------------------------- #
# Bracket scan
# --------------------------------------------------------------------------- #
def _scan_brackets(tokens):
    stack = []
    for t in tokens:
        if t["type"] == "OPEN":
            stack.append(t)
        elif t["type"] == "CLOSE":
            if not stack:
                return _mk("unexpected-close", t["line"], t["col"], t["col"] + 1, ch=t["value"])
            top = stack[-1]
            if OPENERS[top["value"]] != t["value"]:
                return _mk("mismatched-bracket", t["line"], t["col"], t["col"] + 1,
                           ch=t["value"], expected=OPENERS[top["value"]],
                           opener_line=top["line"], opener_col=top["col"])
            stack.pop()
    if stack:
        o = stack[0]
        return _mk("unclosed-bracket", o["line"], o["col"], o["col"] + 1,
                   ch=o["value"], needs=OPENERS[o["value"]],
                   opener_line=o["line"], opener_col=o["col"])
    return None


# --------------------------------------------------------------------------- #
# The single earliest structural error
# --------------------------------------------------------------------------- #
def first_hard_error(src):
    tokens = tokenize(src)
    raw_lines = src.split("\n")
    lls = build_logical(tokens, raw_lines)
    cands = []

    # 1) unterminated string (earliest only)
    for t in tokens:
        if t["type"] == "STRING" and t.get("unterminated"):
            cands.append(_mk("unterminated-string", t["line"], t["col"], t["col"] + 1,
                             quote=t["value"]))
            break

    # 2) brackets
    br = _scan_brackets(tokens)
    if br:
        cands.append(br)

    # 3) per logical-line checks
    for ll in lls:
        toks = ll["toks"]
        last = ll["last"]
        if ll["header_kw"] and _depth_zero_colon(toks) < 0:
            c = last.get("end_col", last["col"] + 1)
            cands.append(_mk("missing-colon", last["line"], c, c + 1,
                             keyword=ll["header_kw"]))
        first = ll["first"]
        if first["value"] in ("print", "exec") and first["type"] == "NAME" and len(toks) > 1:
            nx = toks[1]
            benign = nx["value"] in ("(", "=", ".", "[") or \
                re.match(r"^(\+=|-=|\*=|/=|%=|&=|\|=|\^=|//=|\*\*=|>>=|<<=)$", nx["value"])
            if not benign:
                cands.append(_mk("print-statement", first["line"], first["col"],
                                 first["col"] + len(first["value"]), keyword=first["value"]))
        eqi = _plain_assign_index(toks)
        if eqi > 0:
            lhs = toks[:eqi]
            if len(lhs) == 1 and (lhs[0]["type"] in ("NUMBER", "STRING") or
                                  lhs[0]["value"] in ("True", "False", "None")):
                cands.append(_mk("assign-literal", lhs[0]["line"], lhs[0]["col"],
                                 lhs[0].get("end_col", lhs[0]["col"] + 1)))
        kt = _keyword_typo(ll)
        if kt:
            cands.append(kt)

    # 4) indentation
    prev = None
    for ll in lls:
        f = ll["first"]
        if prev and _opens_block(prev) and ll["indent"] <= prev["indent"]:
            cands.append(_mk("expected-indent", f["line"], f["col"], f["col"] + 1,
                             header_line=prev["last"]["line"],
                             header_end_col=prev["last"].get("end_col", prev["last"]["col"] + 1),
                             header_kw=prev["header_kw"]))
        elif prev and not _opens_block(prev) and ll["indent"] > prev["indent"]:
            cands.append(_mk("unexpected-indent", f["line"], f["col"], f["col"] + 1,
                             prev_line=prev["first"]["line"]))
        elif not prev and ll["indent"] > 0:
            cands.append(_mk("unexpected-indent", f["line"], f["col"], f["col"] + 1,
                             prev_line=None))
        prev = ll
    if prev and _opens_block(prev):
        c = prev["last"].get("end_col", prev["last"]["col"] + 1)
        cands.append(_mk("expected-indent", prev["last"]["line"], c, c + 1,
                         header_line=prev["last"]["line"],
                         header_end_col=c, header_kw=prev["header_kw"], eof=True))

    if not cands:
        return None
    cands.sort(key=lambda e: (e["line"], e["col"]))
    return cands[0]


# --------------------------------------------------------------------------- #
# Advisory hints (do not block parsing)
# --------------------------------------------------------------------------- #
def collect_hints(src):
    tokens = tokenize(src)
    raw_lines = src.split("\n")
    lls = build_logical(tokens, raw_lines)
    hints = []
    for ll in lls:
        toks = ll["toks"]
        # '=' where '==' was likely meant, inside a condition header
        if ll["header_kw"] in ("if", "elif", "while"):
            ci = _depth_zero_colon(toks)
            end = len(toks) if ci < 0 else ci
            d = 0
            for idx in range(1, end):
                t = toks[idx]
                if t["type"] == "OPEN":
                    d += 1
                elif t["type"] == "CLOSE":
                    d = max(0, d - 1)
                elif d == 0 and t["type"] == "OP" and t["value"] == "=":
                    hints.append(_mk("eq-in-cond", t["line"], t["col"], t["col"] + 1))
                    break
        # possible missing comma between two adjacent values inside brackets
        d = 0
        for idx in range(len(toks) - 1):
            a, b = toks[idx], toks[idx + 1]
            if a["type"] == "OPEN":
                d += 1
            elif a["type"] == "CLOSE":
                d = max(0, d - 1)
            if (d > 0 and a["line"] == b["line"] and _is_value_tok(a) and _is_value_tok(b)
                    and not (a["type"] == "STRING" and b["type"] == "STRING")):
                hints.append(_mk("missing-comma", b["line"], b["col"], b["col"] + 1,
                                 before_end_col=a.get("end_col", a["col"] + 1)))
    return hints


# --------------------------------------------------------------------------- #
# Structural repair (deterministic) used by the recovery loop
# --------------------------------------------------------------------------- #
def _insert_before_comment(line, ins):
    m = re.match(r"^(.*?)(\s*#.*)?$", line, re.S)
    code, cm = m.group(1), m.group(2) or ""
    return re.sub(r"\s*$", "", code) + ins + cm


def _replace_at(lines, line_no, col, length, rep):
    L = line_no - 1
    if L < 0 or L >= len(lines):
        return None
    out = lines[:]
    s = out[L]
    out[L] = s[:col - 1] + rep + s[col - 1 + length:]
    return out


def _apply_fix(lines, err):
    L = err["line"] - 1
    rule = err["rule"]
    if rule == "missing-colon":
        lines = lines[:]; lines[L] = _insert_before_comment(lines[L], ":")
        return lines, [err["line"]]
    if rule == "unterminated-string":
        lines = lines[:]; lines[L] = re.sub(r"\s*$", "", lines[L]) + err["quote"]
        return lines, [err["line"]]
    if rule == "unclosed-bracket":
        oL = err["opener_line"] - 1; lines = lines[:]
        lines[oL] = _insert_before_comment(lines[oL], err["needs"])
        return lines, [err["opener_line"]]
    if rule in ("unexpected-close", "mismatched-bracket"):
        rep = err["expected"] if rule == "mismatched-bracket" else ""
        r = _replace_at(lines, err["line"], err["col"], 1, rep)
        return (r, [err["line"]]) if r else (None, [])
    if rule == "print-statement":
        lines = lines[:]
        lines[L] = re.sub(r"^(\s*)(print|exec)\s+(.*?)(\s*#.*)?$",
                          r"\1\2(\3)\4", lines[L])
        return lines, [err["line"]]
    if rule == "keyword-typo":
        r = _replace_at(lines, err["line"], err["col"], len(err["word"]), err["suggest"])
        return (r, [err["line"]]) if r else (None, [])
    if rule == "assign-literal":
        lines = lines[:]; lines[L] = re.sub(r"^(\s*)", r"\1# ", lines[L])
        return lines, [err["line"]]
    if rule == "expected-indent":
        hL = err["header_line"] - 1; lines = lines[:]
        m = re.match(r"^(.*?)(\s*#.*)?$", lines[hL], re.S)
        hcode, hcm = re.sub(r"\s*$", "", m.group(1)), m.group(2) or ""
        if not hcode.endswith(":"):
            hcode += ":"
        lines[hL] = hcode + " pass" + hcm
        return lines, [err["header_line"]]
    if rule == "unexpected-indent":
        lines = lines[:]
        if err.get("prev_line"):
            target = re.match(r"^[ \t]*", lines[err["prev_line"] - 1]).group(0)
        else:
            target = ""
        lines[L] = target + re.sub(r"^[ \t]*", "", lines[L])
        return lines, [err["line"]]
    return None, []


# --------------------------------------------------------------------------- #
# Public entry point: the recovery loop
# --------------------------------------------------------------------------- #
def analyze(src, max_iters=14):
    """Run the fix-reparse-repeat recovery loop.

    Returns a dict:
        {
          "errors":        [error dicts, in the order discovered],
          "hints":         [advisory dicts],
          "recovered":     "<deterministic best-guess repaired source>",
          "changed_lines": [1-based line numbers that the repair touched],
        }
    """
    work = src.split("\n")
    errors, seen, changed = [], set(), set()

    for _ in range(max_iters):
        cur = "\n".join(work)
        err = first_hard_error(cur)
        if not err:
            break
        sig = "{}@{}:{}".format(err["rule"], err["line"], err["col"])
        if sig in seen:                 # cannot clear it — record once and stop
            errors.append(err)
            break
        seen.add(sig)
        errors.append(err)
        new_lines, touched = _apply_fix(work, err)
        if new_lines is None:
            break
        work = new_lines
        for ln in touched:
            changed.add(ln)

    hints = collect_hints(src)

    # make the recovered copy runnable: also apply the safe '=' -> '==' fix,
    # recomputed on the recovered text (columns may have shifted during repair)
    for h in collect_hints("\n".join(work)):
        if h["rule"] == "eq-in-cond":
            r = _replace_at(work, h["line"], h["col"], 1, "==")
            if r:
                work = r
                changed.add(h["line"])

    return {
        "errors": errors,
        "hints": hints,
        "recovered": "\n".join(work),
        "changed_lines": sorted(changed),
    }


if __name__ == "__main__":
    # tiny smoke test when run directly
    import json
    sample = ('def calculate_total(prices)\n    total = 0\n    for p in prices\n'
              '        total += p\n    if total > 100\n        print "Big order!"\n'
              '    return total')
    print(json.dumps(analyze(sample), indent=2))
