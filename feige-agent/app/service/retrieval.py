"""Local lexical retrieval with merchant synonyms and conservative answer gates."""
from __future__ import annotations

import difflib
import re
import unicodedata
from datetime import datetime, timedelta, timezone


def _instant(value, end_of_day=False):
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if end_of_day and len(str(value)) == 10:
        moment += timedelta(days=1)
    return moment


def validity(row, at=None):
    """'valid', 'expired', 'not_effective' or 'invalid' for one knowledge row."""
    at = at or datetime.now(timezone.utc)
    try:
        if row.get("effective_from") and at < _instant(row["effective_from"]):
            return "not_effective"
        if row.get("expires_at") and at >= _instant(row["expires_at"], end_of_day=True):
            return "expired"
    except (ValueError, TypeError):
        return "invalid"
    return "valid"


def normalize(text, synonyms=None):
    value = unicodedata.normalize("NFKC", str(text)).lower()
    for source, target in sorted((synonyms or {}).items(), key=lambda x: -len(x[0])):
        value = value.replace(source.lower(), target.lower())
    return re.sub(r"[\s，。！？?!、,.;；：:～~]", "", value)


def search_faq(query: str, rows: list, product_id=None, synonyms=None, limit=5) -> list[dict]:
    query = normalize(query, synonyms)
    if not query:
        return []
    scored = []
    for index, row in enumerate(rows):
        scope = row.get("product_ids") or []
        if row.get("requires_context") and not scope:
            continue
        if scope and product_id not in scope:
            continue
        best = 0
        kind = "lexical_candidate"
        for form in [row["question"], *(row.get("aliases") or [])]:
            normalized = normalize(form, synonyms)
            if normalized == query:
                best, kind = 1.0, "approved_form"
                break
            a = {query[i:i+2] for i in range(max(1, len(query)-1))}
            b = {normalized[i:i+2] for i in range(max(1, len(normalized)-1))}
            overlap = len(a & b) / max(1, len(a | b))
            ratio = difflib.SequenceMatcher(None, query, normalized).ratio()
            best = max(best, .55 * ratio + .45 * overlap)
        if best >= .25:
            state = validity(row)
            # Expired or not-yet-effective knowledge leaves retrieval: it is
            # still reported so the gap can be classified as stale knowledge.
            scored.append({"index": index, "question": row["question"], "answer": row["answer"],
                           "score": round(best, 4), "match_type": kind if state == "valid" else state,
                           "exact": kind == "approved_form",
                           "answerable": kind == "approved_form" and state == "valid", "product_ids": scope})
    scored.sort(key=lambda x: -x["score"])
    # Conflicting approved forms cannot choose an answer by list order.
    exact_answers = {r["answer"] for r in scored if r["answerable"]}
    if len(exact_answers) > 1:
        for r in scored:
            r["answerable"] = False
            r["match_type"] = "conflict"
    return scored[:max(1, min(int(limit), 20))]
