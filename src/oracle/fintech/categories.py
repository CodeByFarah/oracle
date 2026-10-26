"""Spending categories, merchant category codes (ISO 18245 MCC) and rule-based categorisation."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    color: str
    spending: bool = True


CATEGORIES: dict[str, Category] = {
    c.key: c
    for c in (
        Category("income", "Income", "#2fbf71", spending=False),
        Category("housing", "Housing", "#7c6cf2"),
        Category("groceries", "Groceries", "#35c2a1"),
        Category("dining", "Dining", "#ff8a3d"),
        Category("transport", "Transport", "#3d9bff"),
        Category("shopping", "Shopping", "#f25c8a"),
        Category("subscriptions", "Subscriptions", "#a66cff"),
        Category("entertainment", "Entertainment", "#ffc23d"),
        Category("utilities", "Utilities", "#5ad1e6"),
        Category("health", "Health", "#4fd18b"),
        Category("travel", "Travel", "#ff6b5a"),
        Category("education", "Education", "#8fa3ff"),
        Category("cash", "Cash", "#b8c0cc"),
        Category("fees", "Fees", "#e0707a"),
        Category("gambling", "Gambling", "#d4443f"),
        Category("crypto", "Crypto", "#f0a93b"),
        Category("transfers", "Transfers", "#8a94a6", spending=False),
        Category("other", "Other", "#9aa3b2"),
    )
}
SPENDING_CATEGORIES = tuple(k for k, c in CATEGORIES.items() if c.spending)

# Merchant category codes. Ranges are inclusive.
_MCC_RANGES: tuple[tuple[int, int, str], ...] = (
    (3000, 3999, "travel"),  # airlines, car rental, lodging brands
    (4111, 4131, "transport"),
    (4411, 4411, "travel"),
    (4511, 4582, "travel"),
    (4722, 4722, "travel"),
    (4784, 4789, "transport"),
    (4812, 4816, "utilities"),
    (4829, 4829, "transfers"),
    (4899, 4899, "subscriptions"),
    (4900, 4900, "utilities"),
    (5262, 5262, "shopping"),
    (5300, 5399, "shopping"),
    (5411, 5411, "groceries"),
    (5422, 5499, "groceries"),
    (5541, 5542, "transport"),
    (5600, 5699, "shopping"),
    (5700, 5799, "shopping"),
    (5811, 5814, "dining"),
    (5815, 5818, "subscriptions"),
    (5912, 5912, "health"),
    (5900, 5999, "shopping"),
    (6010, 6011, "cash"),
    (6051, 6051, "crypto"),
    (6513, 6513, "housing"),
    (7011, 7011, "travel"),
    (7230, 7299, "shopping"),
    (7832, 7841, "entertainment"),
    (7900, 7994, "entertainment"),
    (7995, 7995, "gambling"),
    (7996, 7999, "entertainment"),
    (8011, 8099, "health"),
    (8211, 8299, "education"),
)
# Codes the fraud model treats as high risk (gambling, quasi-cash, money transfer, direct mail).
HIGH_RISK_MCC = frozenset({4829, 5967, 6051, 7995})

_KEYWORDS: tuple[tuple[str, str], ...] = (
    (r"payroll|salary|wages", "income"),
    (r"rent|property|mortgage|lease", "housing"),
    (r"market|grocer|fresh|basket|supermarket", "groceries"),
    (r"caf[eé]|coffee|kitchen|bistro|noodle|taco|pizza|burger|sushi|bar\b", "dining"),
    (r"metro|transit|ride|taxi|fuel|parking|rail", "transport"),
    (r"stream|music|cloud|news|subscription|premium", "subscriptions"),
    (r"cinema|games|theat|concert|tickets", "entertainment"),
    (r"power|electric|water|gas co|internet|mobile|telecom", "utilities"),
    (r"pharma|clinic|dental|health|gym|fitness", "health"),
    (r"airline|hotel|air\b|travel|booking", "travel"),
    (r"casino|bet|poker|lottery", "gambling"),
    (r"crypto|coin|exchange", "crypto"),
    (r"atm|cash", "cash"),
)


def category_for_mcc(mcc: int | None) -> str | None:
    if mcc is None:
        return None
    for lo, hi, key in _MCC_RANGES:
        if lo <= mcc <= hi:
            return key
    return None


def categorize(merchant: str, mcc: int | None) -> tuple[str, str]:
    """Return (category, source); the source is "mcc", "rules" or "default"."""
    by_code = category_for_mcc(mcc)
    if by_code is not None:
        return by_code, "mcc"
    name = merchant.lower()
    for pattern, key in _KEYWORDS:
        if re.search(pattern, name):
            return key, "rules"
    return "other", "default"
