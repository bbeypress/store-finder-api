"""
Store Finder API — live search endpoint
-----------------------------------------
Wraps store_finder.py's logic (crt.sh + public storefront checks) as a
live HTTP API, so a search box on a webpage can get real results back
in real time instead of waiting for a scheduled batch job.

Run it:
  pip install -r requirements.txt
  uvicorn main:app --host 0.0.0.0 --port 8000

Then:
  GET /search?keyword=pet&limit=15&find_contact=true&draft_outreach=false
"""

import asyncio
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from store_finder import (
    fetch_certs,
    dedupe_and_sort,
    check_store_live,
    find_contact_email,
    draft_outreach as build_outreach_note,
)

app = FastAPI(
    title="Store Finder API",
    description="Live search for newly-created Shopify stores via public certificate transparency logs.",
    version="1.0.0",
)

# Allow the GitHub Pages frontend (or anywhere) to call this live
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {"name": "Store Finder API", "status": "ok", "endpoints": ["/search", "/health"]}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/search")
async def search(
    keyword: Optional[str] = Query(None, description="Filter subdomains containing this word"),
    limit: int = Query(15, ge=1, le=50, description="Max stores to return (kept modest for live response time)"),
    find_contact: bool = Query(False, description="Look up a public contact email for each store"),
    draft_outreach: bool = Query(False, description="Draft a personalized outreach note (implies find_contact)"),
    your_name: str = Query("Alex", description="Used in drafted outreach notes"),
    your_offer: str = Query("a tool that helps small store owners", description="Used in drafted outreach notes"),
):
    if draft_outreach:
        find_contact = True

    try:
        # Fast-fail policy for live requests: 3 tries, short backoff (1s,2s,3s
        # = 6s worst case) instead of the batch script's patient 75s default —
        # someone waiting on a search box needs a quick answer either way.
        raw_certs = await asyncio.to_thread(fetch_certs, keyword, 3, 1)
    except RuntimeError as e:
        raise HTTPException(
            status_code=503,
            detail=(
                "The store lookup service (crt.sh) is temporarily unavailable "
                "or rate-limiting this server. Please try again in a minute."
            ),
        )

    stores = dedupe_and_sort(raw_certs)[:limit]

    async def enrich(s: dict) -> dict:
        entry = {"domain": s["domain"], "url": f"https://{s['domain']}", "cert_issued": s["not_before"]}
        # Always verify live + fetch products so results are useful, not just bare domains
        live_info = await asyncio.to_thread(check_store_live, s["domain"])
        entry.update(live_info)

        if find_contact:
            entry["contact_email"] = await asyncio.to_thread(find_contact_email, s["domain"])

        if draft_outreach and entry.get("contact_email"):
            entry["outreach_draft"] = await asyncio.to_thread(
                build_outreach_note,
                s["domain"], entry.get("shop_name"), entry.get("products", []), your_name, your_offer,
            )
        return entry

    # Enrich stores concurrently (network-bound work) instead of one-by-one,
    # so results come back in seconds rather than minutes even at limit=50.
    results = await asyncio.gather(*(enrich(s) for s in stores))

    return {
        "query": {"keyword": keyword, "limit": limit},
        "count": len(results),
        "results": results,
    }
