"""kern.injection — canonical prompt-injection scrub list (overhaul P6.4/F15).

Previously duplicated verbatim as syscalls._INJECTION_PATTERNS (audit #5.2)
and journal._COMPACT_INJECTION_PATTERNS (audit #3.1); both copies carried
comments warning "keep in sync manually" — the exact drift hazard this
overhaul exists to remove. One list, one scrub(), two importers.
"""

from __future__ import annotations

import re

# Case-insensitive + DOTALL so <system>...inner stuff...</system> spanning
# newlines is matched as one block.
INJECTION_PATTERNS = [
    (re.compile(r"<system>.*?</system>", re.IGNORECASE | re.DOTALL),
     "[redacted: <system> block]"),
    (re.compile(r"<ip_reminder>.*?</ip_reminder>", re.IGNORECASE | re.DOTALL),
     "[redacted: ip_reminder block]"),
    (re.compile(r"<harness_hint>.*?</harness_hint>", re.IGNORECASE | re.DOTALL),
     "[redacted: harness_hint block]"),
    (re.compile(r"<assistant-hint>.*?</assistant-hint>", re.IGNORECASE | re.DOTALL),
     "[redacted: assistant-hint block]"),
    # Prose patterns like "[harness hint: 3 consecutive actions failed]" —
    # jammed into tool_result text by older Kern versions.
    (re.compile(r"\[harness hint:[^\]]*\]", re.IGNORECASE),
     "[redacted: harness-hint prose]"),
]


def scrub(text: str) -> str:
    """Replace known injection wrappers with neutral markers.

    The marker preserves the *fact* that something was there (so the user
    can see the model didn't hallucinate it away) without leaking the
    injection content into the model's context.
    """
    out = text
    for pat, repl in INJECTION_PATTERNS:
        out = pat.sub(repl, out)
    return out
