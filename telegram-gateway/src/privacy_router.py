"""Gate #3 — route queries that touch PRIVATE information to the local model only.

The interactive Telegram path *is* the cloud: when Claude answers a question it
reads the underlying file into its cloud context, so a question like "what balance
does customer ABC have with us?" would send both the customer name and the amount
to Anthropic. This gate intercepts such questions BEFORE the cloud turn and answers
them entirely on-box (docpipe → local/Nemotron), so the sensitive data never leaves
the appliance.

Design (mirrors grammar-local's fail-safe philosophy):
  • Classify the QUERY locally (regex, no network). The query text itself is not the
    secret — the retrieved file is — so classifying on-box leaks nothing.
  • Bias toward private. A false positive just uses the weaker local model; a false
    NEGATIVE leaks real data to the cloud. When in doubt, treat as private.
  • Fail closed. If the local answerer is down, we tell the user — we NEVER fall
    through to Claude, because that is exactly the leak this gate exists to prevent.

Recall of `is_private()` is the whole ballgame: anything it misses goes to the cloud.
Keep the patterns broad, and see PRIVACY_MODEL_CHECK for a future local second-opinion.
"""
import re
import subprocess

# --- classifier -------------------------------------------------------------
# HARD identifiers: if any of these appear, it is unconditionally private.
_HARD = re.compile(
    r"\b(ssn|social security|credit[\s-]?card|card number|cvv|"
    r"bank account|account number|routing number|iban|swift|"
    r"passport|driver'?s? licen[cs]e|tax id|ein|sin number)\b", re.I)

# Financial-relationship words: money owed/held BETWEEN us and a party. These are
# what distinguish a private-ledger question ("what does ABC owe us") from a public
# product-pricing question ("how much is the PHD17"). Bias toward private.
_FINANCIAL = re.compile(
    r"\b(balance|owe|owes|owed|owing|receivable|payable|outstanding|"
    r"unpaid|past[\s-]?due|overdue|arrears|statement of account|"
    r"on account|credit limit|amount due|how much (do|does|did).*(owe|pay|paid)|"
    r"refunds?|refunded|chargeback|credit note|money back|"
    r"complain(?:t|ts|ed|ing)?|dispute[ds]?)\b",
    re.I)

# Ledger/record words that are private when tied to a named party or "customer".
_LEDGER = re.compile(r"\b(invoice|purchase order|\bpo\b|payment|prepayment|deposit|"
                     r"account statement|ledger|aging report)\b", re.I)
_PARTY = re.compile(r"\b(customer|client|account|vendor|supplier|reseller|dealer|"
                    r"mr\.?|ms\.?|mrs\.?|company)\b", re.I)

# Public product-pricing questions we do NOT want to misfire on: "price/cost of the
# PHD17", "how much is the adapter". These are catalog facts, not private ledgers.
_PUBLIC_PRICE = re.compile(
    r"\b(price|cost|how much (is|are|does)|list price|quote)\b", re.I)
_PRODUCTish = re.compile(r"\b(phd\d*|pht|adapter|fluid|nozzle|print[\s-]?head|"
                         r"machine|11uv|12uv|9uv|4uv|1dx|s2|km\d+|ricoh|epson|"
                         r"kyocera|starfire|xaar|konica)\b", re.I)


def is_private(text):
    """Return (private: bool, reason: str). Deterministic, on-box, no network."""
    t = text or ""
    if _HARD.search(t):
        return True, "hard-identifier"
    if _FINANCIAL.search(t):
        return True, "financial-relationship"
    if _LEDGER.search(t) and _PARTY.search(t):
        return True, "ledger+party"
    # A plain public price question about a product is NOT private.
    if _PUBLIC_PRICE.search(t) and _PRODUCTish.search(t) and not _PARTY.search(t):
        return False, "public-price"
    return False, "no-signal"


# --- local answerer ---------------------------------------------------------
def answer_locally(text, docpipe_bin, timeout=180, k=4, max_tokens=320):
    """Answer a private query fully on-box via docpipe (local RAG → local Qwen2.5-7B).
    Returns the answer string. Raises on failure — the caller must FAIL CLOSED (never
    fall through to the cloud), so we surface the error rather than swallowing it.

    k/max_tokens are bounded deliberately: on CPU/iGPU the 7B's prefill+generation is
    slow, and an unbounded RAG answer (many chunks, long output) can exceed a minute.
    Fewer chunks + shorter output keeps the interactive reply responsive (~15-30s)."""
    r = subprocess.run([docpipe_bin, "ask", text, "-k", str(k), "--max-tokens", str(max_tokens)],
                       capture_output=True, text=True, timeout=timeout)
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        raise RuntimeError((r.stderr or r.stdout or "docpipe returned nothing").strip()[:300])
    return out
