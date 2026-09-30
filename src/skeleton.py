import csv
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

FILTER_Q = "language:rust stars:>100"
FILTER = {"q": FILTER_Q, "sort": None, "order": None, "per_page": 100, "max_pages": 3}
HASH_PAYLOAD = '{"max_pages":3,"order":null,"per_page":100,"q":"language:rust stars:>100","sort":null}'
ALLOWED_QUALIFIERS = {"language", "stars"}
SEARCH_URL = "https://api.github.com/search/repositories"
ACCEPT = "application/vnd.github+json"
API_VERSION = "2022-11-28"
USER_AGENT = "gitcrawl/0.0-skeleton"
TIMEOUT = 30


def validate(filter_q):
    for token in filter_q.split():
        if ":" in token:
            name = token.split(":", 1)[0].lower()
            if name not in ALLOWED_QUALIFIERS:
                print(f"invalid qualifier: {name}", file=sys.stderr)
                sys.exit(2)


def filter_hash():
    return hashlib.sha256(HASH_PAYLOAD.encode("utf-8")).hexdigest()[:12]


def build_headers():
    headers = {
        "Accept": ACCEPT,
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": USER_AGENT,
    }
    token = os.environ.get("GITHUB_TOKEN") or ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def http_get(url, headers):
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()
    except urllib.error.URLError as error:
        print(f"search failed: {error.reason}", file=sys.stderr)
        sys.exit(1)


def retry_delay(headers):
    retry_after = headers.get("Retry-After")
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            return 0.0
    reset = headers.get("x-ratelimit-reset")
    if reset:
        try:
            remaining = float(reset) - time.time()
        except ValueError:
            return 0.0
        if 0.0 < remaining <= 65.0:
            return remaining
    return 0.0


def fetch_page(page, headers):
    query = urllib.parse.urlencode({"q": FILTER_Q, "per_page": FILTER["per_page"], "page": page})
    url = f"{SEARCH_URL}?{query}"
    status, response_headers, body = http_get(url, headers)
    if status in (403, 429):
        delay = retry_delay(response_headers)
        if delay > 0:
            time.sleep(delay)
        status, response_headers, body = http_get(url, headers)
    return status, response_headers, body


def fail(status, body):
    print(f"search failed: {status}", file=sys.stderr)
    print(body.decode("utf-8", "replace")[:300], file=sys.stderr)
    sys.exit(1)


def has_next(link):
    if not link:
        return False
    return any('rel="next"' in part for part in link.split(","))


def search(headers):
    items = []
    incomplete = []
    total_count = None
    for page in range(1, FILTER["max_pages"] + 1):
        status, response_headers, body = fetch_page(page, headers)
        if status != 200:
            fail(status, body)
        payload = json.loads(body)
        if total_count is None:
            total_count = payload.get("total_count", 0)
        page_items = payload.get("items", [])
        items.extend(page_items)
        incomplete.append(bool(payload.get("incomplete_results")))
        if len(page_items) < FILTER["per_page"]:
            break
        if not has_next(response_headers.get("Link", "")):
            break
    return total_count, items, incomplete


def display(items):
    columns = ["full_name", "stargazers_count", "language", "pushed_at"]
    rows = []
    for item in items:
        stars = item.get("stargazers_count")
        rows.append([
            item.get("full_name") or "",
            "" if stars is None else str(stars),
            item.get("language") or "",
            item.get("pushed_at") or "",
        ])
    widths = [len(column) for column in columns]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    print("  ".join(column.ljust(widths[index]) for index, column in enumerate(columns)).rstrip())
    for row in rows:
        print("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip())


def write_bundle(directory, total_count, items, incomplete):
    bundle = {
        "filter": FILTER,
        "filter_hash": filter_hash(),
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "api_version": API_VERSION,
        "total_count": total_count,
        "fetched": len(items),
        "incomplete_results": incomplete,
        "items": items,
    }
    path = os.path.join(directory, "bundle.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(bundle, handle, indent=2)
        handle.write("\n")


def write_csv(directory, items):
    path = os.path.join(directory, "corpus.csv")
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "full_name", "stargazers_count", "language", "pushed_at", "html_url"])
        for item in items:
            writer.writerow([
                item.get("id", ""),
                item.get("full_name", ""),
                item.get("stargazers_count", ""),
                item.get("language") or "",
                item.get("pushed_at") or "",
                item.get("html_url", ""),
            ])


def main():
    validate(FILTER_Q)
    headers = build_headers()
    total_count, items, incomplete = search(headers)
    display(items)
    print(f"total={total_count} fetched={len(items)} incomplete={str(any(incomplete)).lower()}")
    directory = os.path.join("runs", filter_hash())
    os.makedirs(directory, exist_ok=True)
    write_bundle(directory, total_count, items, incomplete)
    write_csv(directory, items)


if __name__ == "__main__":
    main()
