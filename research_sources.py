"""
Low-cost research source discovery.
Uses OpenAlex and Crossref public APIs.
Does not call an LLM or send email.
"""

import argparse
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

USER_AGENT = "ResearchSwarm/1.0 (source discovery)"
TIMEOUT = 25
MAX_PER_PROVIDER = 15

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})


def clean_text(value):
    if not value:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def reconstruct_abstract(inverted_index):
    """Reconstruct an OpenAlex abstract when one is available."""
    if not isinstance(inverted_index, dict):
        return ""

    words = {}
    for word, positions in inverted_index.items():
        for position in positions:
            words[position] = word

    return clean_text(" ".join(words[i] for i in sorted(words)))


def get_json(url, params):
    response = SESSION.get(url, params=params, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def search_openalex(query, limit):
    url = "https://api.openalex.org/works"
    params = {
        "search": query,
        "per_page": limit,
        "sort": "relevance_score:desc",
        "select": (
            "id,doi,title,publication_date,publication_year,"
            "authorships,primary_location,open_access,"
            "abstract_inverted_index,type"
        ),
    }

    data = get_json(url, params)
    records = []

    for item in data.get("results", []):
        location = item.get("primary_location") or {}
        source = location.get("source") or {}
        authors = [
            a.get("author", {}).get("display_name", "")
            for a in item.get("authorships", [])
            if a.get("author")
        ]

        records.append({
            "provider": "OpenAlex",
            "title": clean_text(item.get("title")),
            "authors": authors[:20],
            "publication_date": item.get("publication_date"),
            "publication_year": item.get("publication_year"),
            "doi": item.get("doi"),
            "url": (
                item.get("doi")
                or item.get("id")
                or location.get("landing_page_url")
            ),
            "landing_page": location.get("landing_page_url"),
            "source_name": source.get("display_name"),
            "document_type": item.get("type"),
            "abstract": reconstruct_abstract(
                item.get("abstract_inverted_index")
            ),
            "open_access": (item.get("open_access") or {}).get("is_oa"),
        })

    return records


def search_crossref(query, limit):
    params = {
        "query.bibliographic": query,
        "rows": limit,
        "select": (
            "DOI,title,author,published,published-print,"
            "published-online,type,URL,container-title,abstract"
        ),
    }

    # Crossref recommends identifying yourself in the polite pool.
    contact = os.getenv("RESEARCH_CONTACT_EMAIL")
    if contact:
        params["mailto"] = contact

    data = get_json("https://api.crossref.org/works", params)
    records = []

    for item in data.get("message", {}).get("items", []):
        published = (
            item.get("published-online")
            or item.get("published-print")
            or item.get("published")
            or {}
        )
        parts = published.get("date-parts", [[]])
        date_parts = parts[0] if parts else []
        pub_date = (
            "-".join(str(x).zfill(2) if i > 0 else str(x)
                     for i, x in enumerate(date_parts))
            if date_parts else None
        )

        authors = [
            clean_text(" ".join(
                filter(None, [a.get("given"), a.get("family")])
            ))
            for a in item.get("author", [])
        ]

        doi = item.get("DOI")
        records.append({
            "provider": "Crossref",
            "title": clean_text((item.get("title") or [""])[0]),
            "authors": authors[:20],
            "publication_date": pub_date,
            "publication_year": date_parts[0] if date_parts else None,
            "doi": f"https://doi.org/{doi}" if doi else None,
            "url": item.get("URL"),
            "landing_page": item.get("URL"),
            "source_name": clean_text(
                (item.get("container-title") or [""])[0]
            ),
            "document_type": item.get("type"),
            "abstract": clean_text(item.get("abstract")),
            "open_access": None,
        })

    return records


def deduplicate(records):
    """Prefer DOI matching; use normalized titles when DOI is absent."""
    unique = {}

    for record in records:
        doi = (record.get("doi") or "").lower().strip()
        doi = doi.removeprefix("https://doi.org/").removeprefix("http://doi.org/")
        title = re.sub(r"[^a-z0-9]+", " ",
                       (record.get("title") or "").lower()).strip()

        key = f"doi:{doi}" if doi else f"title:{title}"
        if key == "title:":
            continue

        if key not in unique:
            unique[key] = record
        else:
            old = unique[key]
            # Keep whichever record has more useful abstract information.
            if len(record.get("abstract", "")) > len(old.get("abstract", "")):
                unique[key] = {**old, **record}

    return list(unique.values())


def run_research(query, limit):
    started = datetime.now(timezone.utc)
    all_records = []
    errors = []

    for provider, search_fn in (
        ("OpenAlex", search_openalex),
        ("Crossref", search_crossref),
    ):
        try:
            records = search_fn(query, limit)
            logging.info("%s returned %d records", provider, len(records))
            all_records.extend(records)
        except (requests.RequestException, ValueError, KeyError) as exc:
            errors.append({"provider": provider, "error": str(exc)})
            logging.exception("%s search failed", provider)

        time.sleep(1)

    records = deduplicate(all_records)

    # Never label a failed or empty search as successful research.
    result = {
        "schema_version": "1.0",
        "query": query,
        "started_at": started.isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "providers_attempted": ["OpenAlex", "Crossref"],
        "providers_failed": [e["provider"] for e in errors],
        "search_errors": errors,
        "status": (
            "failed" if len(errors) == 2
            else "partial" if errors
            else "completed"
        ),
        "raw_records_returned": len(all_records),
        "unique_sources": len(records),
        "sources": records,
    }

    output_dir = Path("docs/research")
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = started.strftime("%Y%m%dT%H%M%SZ")
    safe_query = re.sub(r"[^a-zA-Z0-9]+", "-", query).strip("-")[:50]
    output_file = output_dir / f"sources-{timestamp}-{safe_query}.json"
    output_file.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    logging.info("Saved %d unique sources to %s", len(records), output_file)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=MAX_PER_PROVIDER)
    args = parser.parse_args()

    if not 1 <= args.limit <= 25:
        parser.error("--limit must be between 1 and 25")

    result = run_research(args.query, args.limit)
    print(json.dumps({
        "status": result["status"],
        "unique_sources": result["unique_sources"],
        "output": "See docs/research/ for the JSON source ledger.",
    }, indent=2))

    if result["status"] == "failed" or result["unique_sources"] == 0:
        raise SystemExit(1)
