"""Web tools (spec Stage 3): search, then read a page for the details.

Search uses the Brave Search API when a key is in the Keychain and falls back
to DuckDuckGo's HTML results otherwise, so it works with no setup. Everything
from the web is untrusted third-party text.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import keyring
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from backend.agent.tools import Permission, Tool, ToolContext, ToolError
from backend.integrations.google_auth import KEYCHAIN_SERVICE

BRAVE_KEY = "brave_api_key"
PAGE_CHARS = 6_000  # per page, so one long article can't flood the context
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Accept-Language": "en-US,en;q=0.9",
}


class SearchArgs(BaseModel):
    query: str
    max_results: int = Field(6, ge=1, le=10)


class PageArgs(BaseModel):
    url: str = Field(pattern=r"^https?://")


async def _brave(client: httpx.AsyncClient, key: str, a: SearchArgs) -> list[dict[str, Any]]:
    r = await client.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": a.query, "count": a.max_results},
        headers={"X-Subscription-Token": key, "Accept": "application/json"},
    )
    r.raise_for_status()
    return [
        {"title": x.get("title"), "url": x.get("url"), "snippet": x.get("description"), "age": x.get("age")}
        for x in r.json().get("web", {}).get("results", [])[: a.max_results]
    ]


async def _duckduckgo(client: httpx.AsyncClient, a: SearchArgs) -> list[dict[str, Any]]:
    r = await client.post("https://html.duckduckgo.com/html/", data={"q": a.query}, headers=_HEADERS)
    r.raise_for_status()
    out = []
    for res in BeautifulSoup(r.text, "html.parser").select(".result"):
        link = res.select_one("a.result__a")
        if link is None or "result--ad" in (res.get("class") or []):
            continue
        href = str(link.get("href", ""))
        # Links come wrapped in a redirect: //duckduckgo.com/l/?uddg=<real url>
        if (target := parse_qs(urlparse(href).query).get("uddg")) is not None:
            href = target[0]
        snippet = res.select_one(".result__snippet")
        out.append(
            {
                "title": link.get_text(" ", strip=True),
                "url": href,
                "snippet": snippet.get_text(" ", strip=True) if snippet else None,
            }
        )
        if len(out) >= a.max_results:
            break
    return out


async def _public_host(url: str) -> None:
    """Refuse local and private addresses, so a page can't point the agent at
    this Mac's own services (the backend listens on 127.0.0.1)."""
    host = urlparse(url).hostname or ""
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except socket.gaierror as e:
        raise ToolError(f"couldn't find {host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ToolError("that address is on the local network; only public web pages can be read")


def page_text(html: str) -> tuple[str, str]:
    """Title and readable text of an HTML page."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    for tag in soup(["script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    lines = (line.strip() for line in root.get_text("\n").splitlines())
    return title, "\n".join(line for line in lines if line)


def web_tools() -> list[Tool]:
    async def search(a: SearchArgs, ctx: ToolContext) -> Any:
        key = keyring.get_password(KEYCHAIN_SERVICE, BRAVE_KEY)
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
            try:
                results = await (_brave(client, key, a) if key else _duckduckgo(client, a))
            except httpx.HTTPError as e:
                raise ToolError(f"web search failed: {e}") from e
        return {"query": a.query, "engine": "brave" if key else "duckduckgo", "results": results}

    async def read_page(a: PageArgs, ctx: ToolContext) -> Any:
        async def check(request: httpx.Request) -> None:  # every hop, redirects included
            await _public_host(str(request.url))

        hooks = {"request": [check]}
        async with httpx.AsyncClient(
            timeout=10, follow_redirects=True, headers=_HEADERS, event_hooks=hooks
        ) as client:
            try:
                r = await client.get(a.url)
                r.raise_for_status()
            except httpx.HTTPError as e:
                raise ToolError(f"couldn't load the page: {e}") from e
        if "html" not in r.headers.get("content-type", "html"):
            raise ToolError(f"not a web page ({r.headers.get('content-type')})")
        title, text = page_text(r.text)
        return {
            "url": str(r.url),
            "title": title,
            "text": text[:PAGE_CHARS],
            "truncated": len(text) > PAGE_CHARS,
        }

    return [
        Tool("web.search", "Search the web. Returns titles, URLs and snippets.", SearchArgs, search,
             Permission.READ, timeout_s=12, untrusted=True, label="Searched the web"),
        Tool("web.read_page", "Read the text of a web page, e.g. a search result, for details.", PageArgs,
             read_page, Permission.READ, timeout_s=15, untrusted=True, label="Read a page"),
    ]  # fmt: skip
