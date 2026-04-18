"""
LBDC Propylon Editor API — client + HTML document model.

Copied verbatim from the previous project's `lbdc_editor.py`. This is the
verified-working layer (API roundtrip + ICE.js tracked-change format). Do not
modify without re-verifying against the editor UI.

Public surface used by the rest of v9:
  LBDCClient.upload_pdf(path)  -> HTML str        (POST /extract-html/)
  LBDCClient.generate_pdf(html) -> PDF bytes      (POST /generate-pdf/)
  LBDCDocument(html)            -> editable doc
    .get_pages() / .get_lines(p) / .get_line_text(p, i)
    .find_text(text, page=None)
    .replace_text_tracked(old, new, page, occurrence)
    .insert_line(after_line, text, page)
    .delete_line_tracked(line_idx, page, cid)
    .bulk_delete_lines(start, end, page)
    .to_html()  -> str for /generate-pdf/
    .preview(page) / .summary()
"""

import requests
import json
import re
import time
import uuid
from urllib.parse import urlencode
from bs4 import BeautifulSoup, NavigableString
from pathlib import Path
from typing import List, Tuple


BASE_URL = "https://ny-pdf-editor.propyloncloud.com"


class LBDCClient:
    """Client for the LBDC PDF Editor API. No login — CSRF cookies only."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/145.0.0.0 Safari/537.36",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
        })
        self._csrf_token = None
        self._init_csrf()

    def _init_csrf(self):
        resp = self.session.get(f"{BASE_URL}/")
        resp.raise_for_status()
        csrf_cookie = self.session.cookies.get("csrftoken")
        if csrf_cookie:
            self._csrf_token = csrf_cookie
            print(f"[+] CSRF token acquired: {csrf_cookie[:20]}...")
            return
        match = re.search(r'csrfmiddlewaretoken.*?value="([^"]+)"', resp.text)
        if match:
            self._csrf_token = match.group(1)
            print(f"[+] CSRF token from HTML: {self._csrf_token[:20]}...")
        else:
            print("[!] Warning: Could not find CSRF token")

    def set_csrf(self, token: str):
        self._csrf_token = token
        self.session.cookies.set("csrftoken", token)

    def upload_pdf(self, pdf_path: str) -> str:
        """POST /extract-html/. Returns HTML string of the editor view."""
        url = f"{BASE_URL}/extract-html/"
        with open(pdf_path, "rb") as f:
            files = {"file": (Path(pdf_path).name, f, "application/pdf")}
            data = {"csrfmiddlewaretoken": self._csrf_token}
            headers = {
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            }
            print(f"[*] Uploading {pdf_path}...")
            resp = self.session.post(url, files=files, data=data, headers=headers)

        if resp.status_code != 200:
            raise Exception(f"Upload failed ({resp.status_code}): {resp.text[:500]}")

        try:
            result = resp.json()
            html = None
            if isinstance(result, dict):
                for key in ["html", "content", "body", "document", "data"]:
                    if key in result:
                        html = result[key]
                        break
                if html is None:
                    for v in result.values():
                        if isinstance(v, str) and "<" in v:
                            html = v
                            break
            elif isinstance(result, str):
                html = result
            if html:
                print(f"[+] Got HTML content ({len(html)} chars)")
                return html
            print(f"[!] Unexpected response keys: "
                  f"{list(result.keys()) if isinstance(result, dict) else type(result)}")
            return resp.text
        except json.JSONDecodeError:
            if "<div" in resp.text or "<p>" in resp.text:
                print(f"[+] Got raw HTML content ({len(resp.text)} chars)")
                return resp.text
            raise Exception(f"Unexpected response format: {resp.text[:500]}")

    def generate_pdf(self, html_content: str) -> bytes:
        """POST /generate-pdf/. Returns PDF bytes (signed)."""
        url = f"{BASE_URL}/generate-pdf/"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "*/*",
            "X-CSRFToken": self._csrf_token,
            "X-Requested-With": "XMLHttpRequest",
        }
        body = urlencode({"html": html_content})
        print(f"[*] Generating PDF ({len(html_content)} chars of HTML)...")
        resp = self.session.post(url, data=body, headers=headers)

        if resp.status_code != 200:
            raise Exception(f"Generate PDF failed ({resp.status_code}): {resp.text[:500]}")
        if resp.content[:4] == b"%PDF":
            print(f"[+] Got PDF ({len(resp.content)} bytes)")
            return resp.content
        raise Exception(f"Unexpected non-PDF response: {resp.content[:200]!r}")


# ICE.js colors -> userid
COLOR_USER_IDS = {
    "blue": "4", "red": "1", "green": "2",
    "purple": "3", "orange": "5", "black": "0",
}


class LBDCDocument:
    """
    Editor HTML model. Pages are <div class="page">; lines are <p>.
    Tracked changes use <del class="ice-del"> and <ins class="ice-ins">.
    """

    def __init__(self, html: str, user_color: str = "blue"):
        self.raw_html = html
        self.soup = BeautifulSoup(html, "lxml")
        self.user_color = user_color
        self.user_id = COLOR_USER_IDS.get(user_color, "4")

    def _new_cid(self) -> str:
        return str(uuid.uuid1())

    def _timestamp(self) -> str:
        return str(int(time.time() * 1000))

    def _make_del(self, text: str, cid: str = None):
        tag = self.soup.new_tag(
            "del",
            attrs={
                "class": "ice-del cts-1",
                "data-cid": cid or self._new_cid(),
                "data-userid": self.user_id,
                "data-username": self.user_color,
                "data-time": self._timestamp(),
            },
        )
        tag.string = text
        return tag

    def _make_ins(self, text: str, cid: str = None):
        tag = self.soup.new_tag(
            "ins",
            attrs={
                "class": "ice-ins cts-1",
                "data-cid": cid or self._new_cid(),
                "data-userid": self.user_id,
                "data-username": self.user_color,
                "data-time": self._timestamp(),
            },
        )
        tag.string = text
        return tag

    # Navigation
    def get_pages(self) -> list:
        return self.soup.find_all("div", class_="page")

    def get_lines(self, page: int = 0) -> list:
        pages = self.get_pages()
        return pages[page].find_all("p") if page < len(pages) else []

    def get_line_text(self, page: int, line: int) -> str:
        lines = self.get_lines(page)
        return lines[line].get_text() if line < len(lines) else ""

    def find_text(self, text: str, page: int = None) -> List[Tuple[int, int, object]]:
        results = []
        pages = [page] if page is not None else range(len(self.get_pages()))
        for p in pages:
            for i, line in enumerate(self.get_lines(p)):
                if text in line.get_text():
                    results.append((p, i, line))
        return results

    # Core editing
    def replace_text_tracked(self, old_text: str, new_text: str,
                             page: int = 0, occurrence: int = 0) -> bool:
        found = 0
        for line in self.get_lines(page):
            for text_node in line.find_all(string=True):
                node_text = str(text_node)
                if old_text in node_text:
                    if found == occurrence:
                        self._splice_tracked(text_node, old_text, new_text)
                        return True
                    found += 1
        print(f"  Warning: not found: '{old_text}' (occurrence {occurrence}, page {page})")
        return False

    def _splice_tracked(self, text_node, old_text: str, new_text: str):
        full = str(text_node)
        idx = full.index(old_text)
        before, after = full[:idx], full[idx + len(old_text):]

        nodes = []
        if before:
            nodes.append(NavigableString(before))
        nodes.append(self._make_del(old_text))
        if new_text:
            nodes.append(self._make_ins(new_text))
        if after:
            nodes.append(NavigableString(after))

        text_node.replace_with(nodes[0])
        current = nodes[0]
        for node in nodes[1:]:
            current.insert_after(node)
            current = node

    def insert_line(self, after_line: int, text: str, page: int = 0) -> bool:
        lines = self.get_lines(page)
        if after_line >= len(lines):
            return False
        new_p = self.soup.new_tag("p", attrs={"class": "new-line"})
        new_p.append(self._make_ins(text))
        lines[after_line].insert_after(new_p)
        return True

    def delete_line_tracked(self, line_idx: int, page: int = 0, cid: str = None) -> bool:
        lines = self.get_lines(page)
        if line_idx >= len(lines):
            return False
        line = lines[line_idx]
        text = line.get_text()
        if not text.strip():
            return False
        shared_cid = cid or self._new_cid()
        line.clear()
        line.append(self._make_del(text, cid=shared_cid))
        return True

    def bulk_delete_lines(self, start: int, end: int, page: int = 0) -> str:
        shared_cid = self._new_cid()
        for i in range(start, end + 1):
            self.delete_line_tracked(i, page, cid=shared_cid)
        return shared_cid

    # Serialization
    def to_html(self) -> str:
        pages = self.get_pages()
        if pages:
            return "".join(str(p) for p in pages)
        body = self.soup.find("body")
        if body:
            return "".join(str(c) for c in body.children)
        return str(self.soup)

    def preview(self, page: int = 0, max_lines: int = 60):
        for i, line in enumerate(self.get_lines(page)[:max_lines]):
            parts = []
            for child in line.children:
                if hasattr(child, "name") and child.name == "del":
                    parts.append(f"~~{child.get_text()}~~")
                elif hasattr(child, "name") and child.name == "ins":
                    parts.append(f"++{child.get_text()}++")
                elif hasattr(child, "name"):
                    parts.append(child.get_text())
                else:
                    parts.append(str(child))
            print(f" {i:3d} | {''.join(parts)}")

    def summary(self):
        dels = self.soup.find_all("del")
        inss = self.soup.find_all("ins")
        print(f"Pages: {len(self.get_pages())}")
        print(f"Deletions: {len(dels)}")
        print(f"Insertions: {len(inss)}")
