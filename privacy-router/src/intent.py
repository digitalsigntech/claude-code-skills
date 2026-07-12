"""Query-intent privacy classifier — deterministic, on-box, no network.

Classifies the QUERY, not the data: the query text itself is usually not the secret
(the retrieved file is), so classifying on-box leaks nothing. Bias toward private —
a false positive just uses the private model; a false NEGATIVE leaks real data to
the cloud. Recall is the whole ballgame: keep the patterns broad.
"""
import re

# HARD identifiers: if any of these appear, it is unconditionally private.
_HARD = re.compile(
    r"\b(ssn|social security|credit[\s-]?card|card number|cvv|"
    r"bank account|account number|routing number|iban|swift|"
    r"passport|driver'?s? licen[cs]e|tax id|ein|sin number)\b", re.I)

# Financial-relationship words: money owed/held BETWEEN you and a party — plus
# refunds/complaints/disputes, which are always customer-relationship matters.
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

# Public product-pricing questions you do NOT want to misfire on. CUSTOMIZE the
# product terms to your catalog so catalog questions pass through to the cloud.
_PUBLIC_PRICE = re.compile(
    r"\b(price|cost|how much (is|are|does)|list price|quote)\b", re.I)
_PRODUCTish = re.compile(r"\b(product|model|sku|adapter|part|machine|"
                         r"widget-\d+|acme-\w+)\b", re.I)


def is_private(text):
    """Return (private: bool, reason: str)."""
    t = text or ""
    if _HARD.search(t):
        return True, "hard-identifier"
    if _FINANCIAL.search(t):
        return True, "financial-relationship"
    if _LEDGER.search(t) and _PARTY.search(t):
        return True, "ledger+party"
    if _PUBLIC_PRICE.search(t) and _PRODUCTish.search(t) and not _PARTY.search(t):
        return False, "public-price"
    return False, "no-signal"
