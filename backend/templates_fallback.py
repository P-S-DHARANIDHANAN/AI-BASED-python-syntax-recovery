"""
templates_fallback.py
======================
Deterministic, offline natural-language text for each error type.

This is the ORIGINAL rule-based explanation layer. It is kept for one important
reason: if the Gemini API key is missing, invalid, or the network is down, the
app must still work and still explain errors clearly. So this module is the
graceful-degradation path behind `ai_assistant.py`.

Each function returns a dict with the same shape the AI layer produces:
    {"title", "explanation", "hint", "fix", "suggestion", "lesson"}
so the frontend renders both sources identically.
"""


def _suggest_line(src_lines, line, rule, err):
    """A deterministic single-line suggestion where one is obvious."""
    raw = src_lines[line - 1] if 0 <= line - 1 < len(src_lines) else ""
    if rule == "missing-colon":
        return (raw.rstrip() + ":").strip()
    if rule == "print-statement":
        import re
        return re.sub(r"^(\s*)(print|exec)\s+(.*?)(\s*#.*)?$", r"\2(\3)", raw).strip()
    return ""


def describe(err, src_lines):
    rule = err["rule"]
    line = err["line"]
    sev = "warning" if err.get("severity") == "warning" else "error"
    d = {"title": "Syntax problem",
         "explanation": "Something looks off on line {}.".format(line),
         "hint": "Take another look at line {}.".format(line),
         "fix": "", "suggestion": "", "lesson": "", "severity": sev}

    if rule == "missing-colon":
        kw = err.get("keyword", "block")
        d.update(
            title="Missing colon",
            explanation=("A `{}` statement has to end with a colon before its indented "
                         "block. Line {} doesn't have one.").format(kw, line),
            fix="Add a `:` at the end of line {}.".format(line),
            hint=("Line {} starts a `{}` block. What single character does every block "
                  "header need at the end?").format(line, kw),
            suggestion=_suggest_line(src_lines, line, rule, err),
            lesson=("Block headers (if, elif, else, for, while, def, class, try, except, "
                    "finally, with) all end in a colon. The colon tells Python 'an "
                    "indented block follows.'"))
    elif rule == "unterminated-string":
        q = err.get("quote", "'")
        d.update(
            title="Unclosed string",
            explanation=("A string on line {} opens with `{}` but never closes, so Python "
                         "reads to the end looking for the matching quote.").format(line, q),
            fix="Add a closing `{}` to finish the string on line {}.".format(q, line),
            hint=("Look at the quotes on line {}. Does every quote that opens a string also "
                  "have a partner that closes it?").format(line),
            lesson=("Every string needs matching quotes. If you need a quote inside the text, "
                    "use the other kind of quote or escape it with a backslash."))
    elif rule == "unclosed-bracket":
        d.update(
            title="Bracket never closed",
            explanation=("The bracket `{}` opened on line {} is never closed, so Python runs "
                         "off the end of your code looking for its partner.").format(
                             err.get("ch", "("), err.get("opener_line", line)),
            fix="Add a `{}` to match the `{}` on line {}.".format(
                err.get("needs", ")"), err.get("ch", "("), err.get("opener_line", line)),
            hint=("Count the opening and closing brackets. One `{}` opened on line {} — where "
                  "does it close?").format(err.get("ch", "("), err.get("opener_line", line)),
            lesson=("Every ( [ { needs a matching ) ] }. A missing closer is a common cause of "
                    "a confusing error that points at the wrong line."))
    elif rule == "unexpected-close":
        d.update(
            title="Extra closing bracket",
            explanation="There's a `{}` on line {} with no matching opener before it.".format(
                err.get("ch", ")"), line),
            fix="Remove the stray `{}` on line {}, or add the opening bracket it should close.".format(
                err.get("ch", ")"), line),
            hint="Line {} has a `{}`. Is there an opening bracket earlier that it belongs to?".format(
                line, err.get("ch", ")")),
            lesson="Closing brackets must line up with an earlier opener of the same kind.")
    elif rule == "mismatched-bracket":
        d.update(
            title="Mismatched bracket",
            explanation=("Line {} closes with `{}`, but the bracket still open was a different "
                         "kind. Python expected `{}` here.").format(
                             line, err.get("ch", ")"), err.get("expected", ")")),
            fix="Change the `{}` on line {} to `{}`.".format(
                err.get("ch", ")"), line, err.get("expected", ")")),
            hint=("The bracket opened on line {} and the one closing on line {} are different "
                  "shapes. Should they match?").format(err.get("opener_line", line), line),
            lesson="Brackets must match by type as well as count: ( with ), [ with ], { with }.")
    elif rule == "print-statement":
        d.update(
            title="print needs parentheses",
            explanation=("In Python 3, `print` is a function, so its text goes inside "
                         "parentheses. Line {} uses the old Python 2 style.").format(line),
            fix="Wrap what you're printing in parentheses.",
            hint="Line {} prints something. In Python 3, how do you pass values to print?".format(line),
            suggestion=_suggest_line(src_lines, line, rule, err),
            lesson="Python 2 wrote print \"hi\"; Python 3 writes print(\"hi\").")
    elif rule == "assign-literal":
        d.update(
            title="Can't assign to a value",
            explanation=("The left side of `=` on line {} is a fixed value, not a variable "
                         "name. You can only assign to a name.").format(line),
            fix="Put a variable name on the left of `=`. To compare, use `==`.",
            hint="Line {} has `=` with a literal on the left. What has to be on the left of an assignment?".format(line),
            lesson="Assignment stores a value into a name: total = 5. You can't write 5 = total.")
    elif rule == "expected-indent":
        kw = err.get("header_kw", "block")
        d.update(
            title="Missing indented block",
            explanation=("The `{}` header ending on line {} opens a block, but the next line "
                         "isn't indented, so the block is empty.").format(
                             kw, err.get("header_line", line)),
            fix="Indent the lines that belong inside the block. If it's meant to be empty, use `pass`.",
            hint="After a line that ends with a colon, Python expects the next line to step inward. Does it?",
            lesson="Everything inside an if/for/def must be indented further than the header. Four spaces per level is the convention.")
    elif rule == "unexpected-indent":
        d.update(
            title="Unexpected indentation",
            explanation=("Line {} is indented further than the line before it, but nothing "
                         "above opened a new block.").format(line),
            fix="Line up line {} with the code above it. Only indent after a header that ends in a colon.".format(line),
            hint="Line {} sits further right than the line above. What has to come before an indent?".format(line),
            lesson="You can only increase indentation right after a header line that ends with a colon.")
    elif rule == "eq-in-cond":
        d.update(
            title="= where == was likely meant",
            explanation=("Line {} uses a single `=` inside a condition. A single `=` assigns; "
                         "comparing two values needs `==`.").format(line),
            fix="Change `=` to `==` if you're checking for equality.",
            hint="Line {} compares something with `=`. Is one equals sign enough to test equality?".format(line),
            lesson="= stores a value (x = 5). == asks a yes/no question (x == 5).")
    elif rule == "keyword-typo":
        d.update(
            title="Possible misspelled keyword",
            explanation="`{}` on line {} isn't a Python keyword. It's one letter away from `{}`.".format(
                err.get("word", ""), line, err.get("suggest", "")),
            fix="Change `{}` to `{}` if that's what you meant.".format(
                err.get("word", ""), err.get("suggest", "")),
            hint="Line {} begins with `{}`. Does that spelling look right?".format(line, err.get("word", "")),
            lesson="Python keywords must be spelled exactly. A typo turns a keyword into a plain name.")
    elif rule == "missing-comma":
        d.update(
            title="Possible missing comma",
            explanation=("Inside the brackets on line {}, two values sit next to each other with "
                         "nothing between them.").format(line),
            fix="Add a `,` between the two values if they're separate items.",
            hint="Line {} has two values side by side inside brackets. How do you separate items?".format(line),
            lesson="Lists, tuples, sets and calls separate their items with commas: [1, 2, 3], f(a, b).")
    return d
