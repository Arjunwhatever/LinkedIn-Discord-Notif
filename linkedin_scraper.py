"""
Standalone scraper for the public posts of a LinkedIn company page. No login, no cookies.

Only reads what LinkedIn serves to a logged-out visitor. That is often little, and sometimes
nothing (login wall). Scraping is against LinkedIn's terms and this can break whenever LinkedIn
changes its pages, so keep it private, low-volume, and don't run it more than about once an hour.

Install:  pip install requests beautifulsoup4

CLI:
  python linkedin_scraper.py https://www.linkedin.com/company/some-company/
  python linkedin_scraper.py <url> --new-only --state seen_posts.json   # only print unseen posts

Module:
  from linkedin_scraper import fetch_posts
  posts = fetch_posts("https://www.linkedin.com/company/some-company/")
  # -> [{"key": "...", "url": "...", "text": "...", "published": "..." | None}, ...]
"""

import argparse
import json
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

URN_RE = re.compile(r"urn:li:(?:activity|share|ugcPost):(\d+)")
POST_URL_RE = re.compile(
    r"https?://[a-z]{2,3}\.linkedin\.com/posts/[^\s\"'<>?#]+"
    r"|https?://[a-z]{2,3}\.linkedin\.com/feed/update/urn:li:[A-Za-z]+:\d+"
)
TEXT_KEYS = ("articleBody", "text", "description", "headline", "name")
DATE_KEYS = ("datePublished", "dateCreated", "uploadDate")


class LinkedInBlocked(Exception):
    """LinkedIn returned a login wall, rate limit, or other block."""


# ---------- fetching ----------

def fetch_html(url: str, timeout: int = 30) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    if resp.status_code in (401, 403, 429, 999):
        raise LinkedInBlocked(f"LinkedIn returned HTTP {resp.status_code} (blocked or rate limited)")
    if "authwall" in resp.url or "/login" in resp.url or "/uas/login" in resp.url:
        raise LinkedInBlocked("Redirected to LinkedIn login wall")
    resp.raise_for_status()
    return resp.text


# ---------- parsing ----------

def _clean_url(url: str) -> str:
    return url.split("?")[0].split("#")[0].rstrip("/")


def _key_for(url: str | None, text: str) -> str:
    if url:
        m = URN_RE.search(url)
        if m:
            return f"urn:{m.group(1)}"
        # Public /posts/ URLs end in "...-activity-<id>-xxxx"; the id is a stable key.
        m = re.search(r"activity-(\d{10,})", url)
        if m:
            return f"urn:{m.group(1)}"
        return _clean_url(url)
    return "text:" + text[:80]


def _walk(node):
    """Yield every dict inside nested JSON."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _from_json_ld(soup: BeautifulSoup) -> list[dict]:
    posts = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in _walk(data):
            t = obj.get("@type")
            types = t if isinstance(t, list) else [t]
            looks_like_post = any(
                isinstance(x, str) and ("Posting" in x or x in ("Article", "NewsArticle", "BlogPosting"))
                for x in types
            )
            if not looks_like_post:
                continue
            url = obj.get("url")
            main = obj.get("mainEntityOfPage")
            if not url and isinstance(main, dict):
                url = main.get("@id")
            elif not url and isinstance(main, str):
                url = main
            text = next((obj[k] for k in TEXT_KEYS if isinstance(obj.get(k), str) and obj[k].strip()), "")
            published = next((obj[k] for k in DATE_KEYS if obj.get(k)), None)
            if url or text:
                posts.append(
                    {"key": _key_for(url, text), "url": url, "text": text.strip(), "published": published}
                )
    return posts


def _from_html(soup: BeautifulSoup, html: str) -> list[dict]:
    posts = []

    # Elements that carry a post URN as an attribute.
    for el in soup.find_all(True):
        for attr in ("data-urn", "data-activity-urn", "data-id", "data-entity-urn"):
            val = el.get(attr)
            if isinstance(val, str) and URN_RE.search(val):
                urn = URN_RE.search(val).group(0)
                url = f"https://www.linkedin.com/feed/update/{urn}"
                text = el.get_text(" ", strip=True)[:1000]
                posts.append({"key": _key_for(url, text), "url": url, "text": text, "published": None})
                break

    # Anchors pointing at post URLs.
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not POST_URL_RE.search(href):
            continue
        url = POST_URL_RE.search(href).group(0)
        container = a.find_parent(["article", "li"]) or (a.parent.parent if a.parent else a)
        text = container.get_text(" ", strip=True)[:1000]
        published = None
        time_tag = container.find("time")
        if time_tag:
            published = time_tag.get("datetime") or time_tag.get_text(strip=True)
        posts.append({"key": _key_for(url, text), "url": url, "text": text, "published": published})

    # Last resort: post URLs that appear anywhere in the raw HTML (e.g. inside inline JSON).
    for m in POST_URL_RE.finditer(html.replace("\\/", "/")):
        url = m.group(0)
        posts.append({"key": _key_for(url, ""), "url": url, "text": "", "published": None})

    return posts


def parse_posts(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    candidates = _from_json_ld(soup) + _from_html(soup, html)

    # Deduplicate by key, keeping the richest version (longest text, has a date).
    best: dict[str, dict] = {}
    for p in candidates:
        cur = best.get(p["key"])
        if cur is None:
            best[p["key"]] = p
            continue
        if len(p["text"]) > len(cur["text"]):
            cur["text"] = p["text"]
        cur["url"] = cur["url"] or p["url"]
        cur["published"] = cur["published"] or p["published"]
    return list(best.values())


def fetch_posts(company_url: str) -> list[dict]:
    """Fetch and parse public posts for a company page URL. Raises LinkedInBlocked on a block."""
    base = _clean_url(company_url)
    if "/company/" not in base:
        raise ValueError("Expected a URL like https://www.linkedin.com/company/<slug>/")
    base = base.split("/posts")[0].split("/about")[0]

    posts: list[dict] = []
    last_error: Exception | None = None
    for url in (f"{base}/", f"{base}/posts/"):
        try:
            posts = parse_posts(fetch_html(url))
        except LinkedInBlocked as e:
            last_error = e
            continue
        if posts:
            return posts
    if not posts and last_error:
        raise last_error
    return posts


# ---------- CLI ----------

def main():
    ap = argparse.ArgumentParser(description="Scrape public posts from a LinkedIn company page.")
    ap.add_argument("company_url")
    ap.add_argument("--new-only", action="store_true", help="only output posts not seen before")
    ap.add_argument("--state", default="seen_posts.json", help="file tracking seen post keys")
    args = ap.parse_args()

    try:
        posts = fetch_posts(args.company_url)
    except LinkedInBlocked as e:
        print(f"Blocked: {e}. LinkedIn is not serving this page to logged-out visitors.", file=sys.stderr)
        sys.exit(2)
    except requests.RequestException as e:
        print(f"Network error: {e}", file=sys.stderr)
        sys.exit(1)

    if not posts:
        print("Fetched the page but found no posts (LinkedIn may hide them from logged-out visitors).", file=sys.stderr)
        sys.exit(3)

    if args.new_only:
        state_path = Path(args.state)
        first_run = not state_path.exists()
        seen = set(json.loads(state_path.read_text())) if not first_run else set()
        new = [p for p in posts if p["key"] not in seen]
        state_path.write_text(json.dumps(sorted(seen | {p["key"] for p in posts})))
        if first_run:
            print(f"First run: recorded {len(posts)} existing posts, nothing to report.", file=sys.stderr)
            return
        posts = new

    print(json.dumps(posts, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
