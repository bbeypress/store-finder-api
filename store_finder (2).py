"""
Store Finder — discover newly-created Shopify stores, find a public
contact, and draft a personalized outreach note for each one.
-------------------------------------------------------------------
Every data source used here is public and belongs to no single
platform's proprietary dashboard, so no third-party Terms of Service
are implicated:

  1. crt.sh (Certificate Transparency logs) — public-by-design log of
     every HTTPS certificate ever issued. New *.myshopify.com stores
     show up here fast, often before a custom domain is even bought.

  2. <store>.myshopify.com/products.json — Shopify's own intentional
     public storefront API. Confirms a store is live and shows what
     it sells.

  3. The store's own public pages (homepage, /pages/contact-us, etc.)
     — scraped only for a publicly-displayed contact email/mailto
     link the store owner put there themselves for people to use.

Usage:
    pip install -r requirements.txt

    # Just find stores
    python store_finder.py --keyword pet --limit 20

    # Also find a public contact email for each
    python store_finder.py --keyword pet --limit 20 --find-contact

    # Also draft a personalized outreach note for each (implies --find-contact)
    python store_finder.py --keyword pet --limit 20 --draft-outreach \\
        --your-name "Alex" --your-offer "Reply Guardian, a tool that checks customer-service replies before you send them"

    # Save everything to JSON (for the GitHub Action / later use)
    python store_finder.py --limit 50 --draft-outreach --json results.json

Legal/ethical note: this only collects an email address if the store
itself displays it publicly for contact purposes. If you email people
found this way, be upfront about who you are and make it easy for them
to say "not interested" — that's both the decent thing to do and, in
most places, a legal requirement for commercial email.
"""

import argparse
import json
import re
import sys
import time

import httpx

CRTSH_URL = "https://crt.sh/"
# crt.sh sits behind bot protection that's more aggressive toward
# datacenter/cloud IPs (like GitHub Actions runners) than home IPs.
# A browser-like User-Agent and full header set reduces false blocks.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/json,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

CONTACT_PATHS = ["/", "/pages/contact-us", "/pages/contact", "/pages/about-us", "/pages/about"]
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
# Domains that show up as false positives (Shopify's own infra, image CDNs, etc.)
EMAIL_JUNK_DOMAINS = {"shopify.com", "sentry.io", "example.com", "wixpress.com"}


# ---------- Step 1: find stores via crt.sh ----------

def fetch_certs(keyword: str | None, retries: int = 5, backoff_seconds: int = 5) -> list[dict]:
    query = f"%{keyword}%.myshopify.com" if keyword else "%.myshopify.com"
    params = {"q": query, "output": "json"}

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            with httpx.Client(timeout=30, headers=BROWSER_HEADERS) as client:
                resp = client.get(CRTSH_URL, params=params)
                resp.raise_for_status()
                text = resp.text.strip()
                return json.loads(text) if text else []
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            last_err = e
            # Longer, increasing backoff — crt.sh's bot protection backs off
            # faster than a fixed short retry can outlast. Callers with tight
            # time budgets (e.g. a live web request) should pass a smaller
            # retries/backoff_seconds instead of using these defaults.
            wait = attempt * backoff_seconds
            print(f"  crt.sh request failed ({e}); retrying in {wait}s...", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(
        f"crt.sh failed after {retries} attempts: {last_err}\n"
        "This usually means crt.sh is temporarily blocking automated/cloud "
        "traffic (common on GitHub Actions' shared IPs). Try running "
        "locally instead, or re-run the workflow later."
    )


def dedupe_and_sort(certs: list[dict]) -> list[dict]:
    latest_by_domain: dict[str, dict] = {}
    for c in certs:
        name = c.get("common_name") or c.get("name_value", "")
        for domain in name.split("\n"):
            domain = domain.strip().lower()
            if domain.startswith("www."):
                domain = domain[4:]
            if not domain.endswith(".myshopify.com") or domain.startswith("*."):
                continue
            not_before = c.get("not_before", "")
            existing = latest_by_domain.get(domain)
            if not existing or not_before > existing.get("not_before", ""):
                latest_by_domain[domain] = {"domain": domain, "not_before": not_before}

    results = list(latest_by_domain.values())
    results.sort(key=lambda r: r["not_before"], reverse=True)
    return results


# ---------- Step 2: confirm the store is live ----------

def check_store_live(domain: str) -> dict:
    url = f"https://{domain}/products.json"
    try:
        with httpx.Client(timeout=10, headers=BROWSER_HEADERS, follow_redirects=True) as client:
            resp = client.get(url, params={"limit": 5})
            if resp.status_code != 200:
                return {"status": "unreachable_or_locked", "products": [], "shop_name": None}
            data = resp.json()
            products = [p.get("title", "") for p in data.get("products", [])]
            vendor = data["products"][0].get("vendor") if data.get("products") else None
            return {"status": "live", "products": products, "shop_name": vendor}
    except Exception:
        return {"status": "unreachable_or_locked", "products": [], "shop_name": None}


# ---------- Step 3: find a publicly-displayed contact email ----------

def find_contact_email(domain: str) -> str | None:
    """Checks the store's own public pages for a contact email they
    displayed themselves (mailto: link or plain email text). Returns
    the first plausible match, or None if nothing is found."""
    for path in CONTACT_PATHS:
        url = f"https://{domain}{path}"
        try:
            with httpx.Client(timeout=8, headers=BROWSER_HEADERS, follow_redirects=True) as client:
                resp = client.get(url)
                if resp.status_code != 200:
                    continue
                html = resp.text

                # Prefer explicit mailto: links — strongest signal it's meant as a contact point
                mailto_matches = re.findall(r'mailto:([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', html)
                for m in mailto_matches:
                    if not any(junk in m.lower() for junk in EMAIL_JUNK_DOMAINS):
                        return m

                # Fall back to any plain-text email on the page
                text_matches = EMAIL_RE.findall(html)
                for m in text_matches:
                    if not any(junk in m.lower() for junk in EMAIL_JUNK_DOMAINS):
                        return m
        except Exception:
            continue
    return None


# ---------- Step 4: draft a personalized outreach note ----------

def draft_outreach(domain: str, shop_name: str | None, products: list[str], your_name: str, your_offer: str) -> str:
    display_name = shop_name or domain.replace(".myshopify.com", "").replace("-", " ").title()
    product_line = f" I noticed you're selling things like {', '.join(products[:2])}." if products else ""

    return (
        f"Subject: Quick note for {display_name}\n\n"
        f"Hi there,\n\n"
        f"I came across your store, {display_name}, and wanted to reach out directly "
        f"rather than send anything automated-sounding.{product_line}\n\n"
        f"I'm {your_name}, and I built {your_offer}. Thought it might be useful as you "
        f"grow the store — no pressure either way, just wanted to put it on your radar.\n\n"
        f"Happy to answer any questions, or feel free to ignore this if it's not useful "
        f"to you right now — either way, best of luck with the store!\n\n"
        f"— {your_name}\n\n"
        f"(If you'd rather not hear from me again, just reply and let me know.)"
    )


# ---------- Main ----------

def main():
    parser = argparse.ArgumentParser(description="Find newly-created Shopify stores, their contacts, and draft outreach.")
    parser.add_argument("--keyword", type=str, default=None, help="Filter subdomains containing this word")
    parser.add_argument("--limit", type=int, default=20, help="Max number of stores to output")
    parser.add_argument("--verify-live", action="store_true", help="Check each store's live product catalog")
    parser.add_argument("--find-contact", action="store_true", help="Also look for a public contact email on each store")
    parser.add_argument("--draft-outreach", action="store_true", help="Also draft a personalized outreach note (implies --verify-live and --find-contact)")
    parser.add_argument("--your-name", type=str, default="Alex", help="Your name, used in drafted outreach notes")
    parser.add_argument("--your-offer", type=str, default="a tool that helps small store owners", help="One-line description of what you're offering, used in drafted outreach notes")
    parser.add_argument("--json", type=str, default=None, help="Write results to this JSON file")
    args = parser.parse_args()

    if args.draft_outreach:
        args.verify_live = True
        args.find_contact = True

    search_desc = f'stores matching "{args.keyword}"' if args.keyword else "all recent stores"
    print(f"Querying crt.sh for {search_desc}...")
    raw_certs = fetch_certs(args.keyword)
    print(f"  Found {len(raw_certs)} certificate log entries.")

    stores = dedupe_and_sort(raw_certs)[: args.limit]
    print(f"  {len(stores)} unique store(s) after de-duplication.\n")

    output = []
    for i, s in enumerate(stores, 1):
        entry = {"domain": s["domain"], "url": f"https://{s['domain']}", "cert_issued": s["not_before"]}
        print(f"[{i}/{len(stores)}] {entry['url']}")

        if args.verify_live:
            entry.update(check_store_live(s["domain"]))
            if entry.get("products"):
                print(f"    sells: {', '.join(entry['products'][:3])}")

        if args.find_contact:
            entry["contact_email"] = find_contact_email(s["domain"])
            print(f"    contact: {entry['contact_email'] or 'none found'}")

        if args.draft_outreach and entry.get("contact_email"):
            entry["outreach_draft"] = draft_outreach(
                s["domain"], entry.get("shop_name"), entry.get("products", []),
                args.your_name, args.your_offer,
            )

        output.append(entry)

    if args.json:
        with open(args.json, "w") as f:
            json.dump(output, f, indent=2)
        print(f"\nSaved {len(output)} results to {args.json}")

    contactable = sum(1 for e in output if e.get("contact_email"))
    if args.find_contact:
        print(f"\n{contactable}/{len(output)} stores had a public contact email.")


if __name__ == "__main__":
    main()
