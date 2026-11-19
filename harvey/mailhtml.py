"""Email HTML the dashboard can show safely: an email exactly as it went out,
with the mailbox's signature, its logo and its line breaks.

The dashboard's content policy only loads images from itself, so a
signature's logo is fetched once on the server and inlined as a data: URI
(images only, size-capped, public addresses only). Everything else goes
through a small allow-list sanitizer: no scripts, styles blocks, frames,
event handlers or javascript: links survive it.
"""

import base64
import html
import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urlparse

MAX_IMAGE_BYTES = 300_000

_ALLOWED = {
    "p", "br", "div", "span", "a", "b", "strong", "i", "em", "u", "img", "table", "tbody",
    "thead", "tr", "td", "th", "font", "ul", "ol", "li", "hr", "small", "h1", "h2", "h3", "h4",
    "blockquote", "center",
}
_VOID = {"br", "img", "hr"}
_DROP_WITH_CONTENT = {"script", "style", "head", "title", "iframe", "object", "noscript", "svg"}
_PLAIN_ATTRS = {"alt", "width", "height", "colspan", "rowspan", "align", "valign", "color",
                "size", "border", "cellpadding", "cellspacing", "bgcolor", "title"}
_BAD_STYLE = re.compile(r"(?i)url\s*\(|expression|@import|behavior|position\s*:")
_IMG_SRC = re.compile(r"""(?i)(<img\b[^>]*?\bsrc\s*=\s*)(["'])(https?://[^"']+)\2""")


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_WITH_CONTENT:
            self.skip += 1
            return
        if self.skip or tag not in _ALLOWED:
            return
        keep = []
        for k, v in attrs:
            v = (v or "").strip()
            if k == "href" and re.match(r"(?i)^(https?:|mailto:)", v):
                keep.append((k, v))
            elif k == "src" and tag == "img" and v.startswith("data:image/"):
                keep.append((k, v))
            elif k == "style" and not _BAD_STYLE.search(v):
                keep.append((k, v))
            elif k in _PLAIN_ATTRS:
                keep.append((k, v))
        if tag == "a":
            keep += [("target", "_blank"), ("rel", "noopener noreferrer")]
        if tag == "img" and not any(k == "src" for k, _ in keep):
            return  # an image we couldn't inline shows nothing rather than break the policy
        attrs_html = "".join(f' {k}="{html.escape(v, quote=True)}"' for k, v in keep)
        self.out.append(f"<{tag}{attrs_html}>")

    def handle_endtag(self, tag):
        if tag in _DROP_WITH_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in _ALLOWED or tag in _VOID:
            return
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data))


def sanitize(markup: str) -> str:
    """Only the harmless parts of an email's HTML."""
    parser = _Sanitizer()
    parser.feed(markup or "")
    parser.close()
    return "".join(parser.out)


def text_to_html(text: str) -> str:
    """Plain email text as HTML, its line breaks kept."""
    return html.escape(text or "").replace("\r\n", "\n").replace("\n", "<br>")


def _public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return bool(infos)


async def fetch_image(url: str) -> str:
    """A web image as a data: URI, or "" when it isn't a small public image."""
    import httpx
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    if not _public_host(parsed.hostname):
        return ""
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
            r = await client.get(url)
    except Exception:
        return ""
    kind = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    if r.status_code != 200 or not kind.startswith("image/") or kind == "image/svg+xml":
        return ""
    if len(r.content) > MAX_IMAGE_BYTES:
        return ""
    return f"data:{kind};base64," + base64.b64encode(r.content).decode()


async def inline_images(markup: str, fetch) -> str:
    """Swap each web image in ``markup`` for its data: URI (``fetch`` caches)."""
    urls = list(dict.fromkeys(m.group(3) for m in _IMG_SRC.finditer(markup or "")))
    found = {u: await fetch(u) for u in urls}

    def swap(m):
        data = found.get(m.group(3)) or ""
        return f"{m.group(1)}{m.group(2)}{data}{m.group(2)}"
    return _IMG_SRC.sub(swap, markup or "")


async def safe_email_html(markup: str, fetch) -> str:
    return sanitize(await inline_images(markup, fetch))
