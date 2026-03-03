"""
LBDC PDF Editor - API Replay Toolkit v2
=========================================
Automates editing of NYS Legislative Bill Drafting Commission documents
by directly calling the Propylon cloud editor's API endpoints.

CONFIRMED API:
  Upload:   POST /extract-html/   (multipart/form-data, PDF → HTML)
  Generate: POST /generate-pdf/   (urlencoded, HTML → signed PDF)
  Auth:     Django CSRF tokens (no login required)

SETUP:
  pip install requests beautifulsoup4 lxml

QUICKSTART:
  from lbdc_editor import LBDCClient, LBDCDocument

  client = LBDCClient()
  html = client.upload_pdf("my_bill.pdf")
  doc = LBDCDocument(html)
  doc.replace_text_tracked("54,000,000", "58,000,000")
  pdf_bytes = client.generate_pdf(doc.to_html())
  open("edited_bill.pdf", "wb").write(pdf_bytes)
"""

import requests
import json
import re
import time
import uuid
from urllib.parse import urlencode, quote, unquote
from bs4 import BeautifulSoup, NavigableString
from pathlib import Path
from typing import Optional, List, Tuple
import copy
import sys


# =============================================================================
# API CLIENT
# =============================================================================

BASE_URL = "https://ny-pdf-editor.propyloncloud.com"


class LBDCClient:
    """
    Client for the LBDC PDF Editor API.

    Handles CSRF token management and the upload/generate cycle.
    No login required — the editor is public, just needs CSRF tokens.
    """

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
        """
        Fetch the CSRF token by hitting the homepage.
        Django sets csrftoken cookie on first visit.
        """
        resp = self.session.get(f"{BASE_URL}/")
        resp.raise_for_status()

        # Get token from cookie
        csrf_cookie = self.session.cookies.get("csrftoken")
        if csrf_cookie:
            self._csrf_token = csrf_cookie
            print(f"[+] CSRF token acquired: {csrf_cookie[:20]}...")
        else:
            # Try to extract from page HTML
            match = re.search(r'csrfmiddlewaretoken.*?value="([^"]+)"', resp.text)
            if match:
                self._csrf_token = match.group(1)
                print(f"[+] CSRF token from HTML: {self._csrf_token[:20]}...")
            else:
                print("[!] Warning: Could not find CSRF token")
                print("    You may need to set it manually:")
                print("    client._csrf_token = 'your_token_here'")

    def set_csrf(self, token: str):
        """Manually set CSRF token if auto-detection fails."""
        self._csrf_token = token
        self.session.cookies.set("csrftoken", token)

    def upload_pdf(self, pdf_path: str) -> str:
        """
        Upload a PDF file and get back the HTML editor content.

        POST /extract-html/
        Content-Type: multipart/form-data
        Fields: csrfmiddlewaretoken, file
        Returns: HTML string (the editor's internal representation)
        """
        url = f"{BASE_URL}/extract-html/"

        with open(pdf_path, "rb") as f:
            files = {
                "file": (Path(pdf_path).name, f, "application/pdf")
            }
            data = {
                "csrfmiddlewaretoken": self._csrf_token
            }
            headers = {
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            }

            print(f"[*] Uploading {pdf_path}...")
            resp = self.session.post(url, files=files, data=data, headers=headers)

        if resp.status_code != 200:
            raise Exception(
                f"Upload failed ({resp.status_code}): {resp.text[:500]}"
            )

        # Response should be JSON containing the HTML
        try:
            result = resp.json()
            # The HTML might be under various keys - try common ones
            html = None
            if isinstance(result, dict):
                for key in ["html", "content", "body", "document", "data"]:
                    if key in result:
                        html = result[key]
                        break
                if html is None:
                    # Maybe the whole response is the relevant data
                    # Try the first string value that looks like HTML
                    for v in result.values():
                        if isinstance(v, str) and "<" in v:
                            html = v
                            break
            elif isinstance(result, str):
                html = result

            if html:
                print(f"[+] Got HTML content ({len(html)} chars)")
                return html
            else:
                print(f"[!] Unexpected response format. Keys: {list(result.keys()) if isinstance(result, dict) else type(result)}")
                print(f"    Raw response (first 500): {resp.text[:500]}")
                return resp.text

        except json.JSONDecodeError:
            # Maybe it returns raw HTML, not JSON
            if "<div" in resp.text or "<p>" in resp.text:
                print(f"[+] Got raw HTML content ({len(resp.text)} chars)")
                return resp.text
            else:
                raise Exception(f"Unexpected response format: {resp.text[:500]}")

    def generate_pdf(self, html_content: str, filename: str = "output") -> bytes:
        """
        Submit HTML content to generate a signed PDF.

        POST /generate-pdf/
        Content-Type: application/x-www-form-urlencoded; charset=UTF-8
        Body: html=<URL-encoded HTML>
        Returns: PDF bytes
        """
        url = f"{BASE_URL}/generate-pdf/"

        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "*/*",
            "X-CSRFToken": self._csrf_token,
            "X-Requested-With": "XMLHttpRequest",
        }

        # URL-encode the HTML as form data
        body = urlencode({"html": html_content})

        print(f"[*] Generating PDF ({len(html_content)} chars of HTML)...")
        resp = self.session.post(url, data=body, headers=headers)

        if resp.status_code != 200:
            raise Exception(
                f"Generate PDF failed ({resp.status_code}): {resp.text[:500]}"
            )

        # Check if response is actually a PDF
        if resp.content[:4] == b"%PDF":
            print(f"[+] Got PDF ({len(resp.content)} bytes)")
            return resp.content
        else:
            # Might return JSON with a download URL or base64
            try:
                result = resp.json()
                print(f"[!] Got JSON response instead of PDF: {list(result.keys()) if isinstance(result, dict) else type(result)}")
                # Check for base64-encoded PDF
                if isinstance(result, dict):
                    for v in result.values():
                        if isinstance(v, str) and len(v) > 1000:
                            import base64
                            try:
                                decoded = base64.b64decode(v)
                                if decoded[:4] == b"%PDF":
                                    print("[+] Found base64-encoded PDF in response")
                                    return decoded
                            except:
                                pass
                return resp.content
            except:
                print(f"[+] Got binary response ({len(resp.content)} bytes)")
                return resp.content

    def upload_edit_download(self, pdf_path: str, edit_fn, output_path: str,
                              user_color: str = "blue"):
        """
        Full pipeline: upload PDF → apply edits → download signed PDF.

        Args:
            pdf_path: Path to LBDC-compatible PDF
            edit_fn: Function that takes LBDCDocument and modifies it
            output_path: Where to save the output PDF
            user_color: Color for tracked changes (blue, red, green, etc.)

        Example:
            def my_edits(doc):
                doc.replace_text_tracked("54,000,000", "58,000,000")
                doc.replace_text_tracked("$47,038,000", "$51,038,000")

            client = LBDCClient()
            client.upload_edit_download("input.pdf", my_edits, "output.pdf")
        """
        html = self.upload_pdf(pdf_path)

        doc = LBDCDocument(html, user_color=user_color)
        print(f"[*] Document has {len(doc.get_pages())} page(s)")

        edit_fn(doc)

        pdf_bytes = self.generate_pdf(doc.to_html())

        with open(output_path, "wb") as f:
            f.write(pdf_bytes)

        print(f"[+] Saved: {output_path} ({len(pdf_bytes)} bytes)")
        return output_path


# =============================================================================
# DOCUMENT MODEL
# =============================================================================

# Color name → user ID mapping (observed from the editor)
COLOR_USER_IDS = {
    "blue": "4",
    "red": "1",
    "green": "2",
    "purple": "3",
    "orange": "5",
    "black": "0",  # default (no multiuser color selected)
}


class LBDCDocument:
    """
    Represents an LBDC document in HTML editor format.

    Structure:
        <div class="page" contenteditable="true">
            <p>Line of text</p>
            <p style="min-height: 14.75px;"> </p>    ← blank line
            <p class="new-line"><ins ...>new text</ins></p>  ← inserted line
        </div>

    Change tracking (ICE.js):
        <del class="ice-del cts-1" data-cid="UUID" data-userid="4"
             data-username="blue" data-time="1772404602311">struck text</del>
        <ins class="ice-ins cts-1" data-cid="UUID" data-userid="4"
             data-username="blue" data-time="1772489568719">added text</ins>

    Key details:
        - data-cid: UUID v1 (e.g., "1eb1ed70-15bf-11f1-862a-1b07331b4eb0")
        - data-userid: numeric, maps to color
        - data-username: color name ("blue", "red", etc.)
        - data-time: Unix timestamp in milliseconds
        - Bulk deletes share the same data-cid
        - New inserted lines have class="new-line" on the <p> tag
    """

    def __init__(self, html: str, user_color: str = "blue"):
        self.raw_html = html
        self.soup = BeautifulSoup(html, "lxml")
        self.user_color = user_color
        self.user_id = COLOR_USER_IDS.get(user_color, "4")

    def _new_cid(self) -> str:
        """Generate a UUID v1 for change tracking (matches editor format)."""
        return str(uuid.uuid1())

    def _timestamp(self) -> str:
        """Current time as Unix milliseconds."""
        return str(int(time.time() * 1000))

    def _make_del(self, text: str, cid: str = None) -> object:
        """Create a <del> tag for striking through text."""
        tag = self.soup.new_tag(
            "del",
            attrs={
                "class": "ice-del cts-1",
                "data-cid": cid or self._new_cid(),
                "data-userid": self.user_id,
                "data-username": self.user_color,
                "data-time": self._timestamp(),
            }
        )
        tag.string = text
        return tag

    def _make_ins(self, text: str, cid: str = None) -> object:
        """Create an <ins> tag for inserting new text."""
        tag = self.soup.new_tag(
            "ins",
            attrs={
                "class": "ice-ins cts-1",
                "data-cid": cid or self._new_cid(),
                "data-userid": self.user_id,
                "data-username": self.user_color,
                "data-time": self._timestamp(),
            }
        )
        tag.string = text
        return tag

    # ─────────────────────────────────────────────────────────────────
    # NAVIGATION
    # ─────────────────────────────────────────────────────────────────

    def get_pages(self) -> list:
        """Get all page <div> elements."""
        return self.soup.find_all("div", class_="page")

    def get_lines(self, page: int = 0) -> list:
        """Get all <p> elements (lines) on a page."""
        pages = self.get_pages()
        if page < len(pages):
            return pages[page].find_all("p")
        return []

    def get_line_text(self, page: int, line: int) -> str:
        """Get plain text of a specific line."""
        lines = self.get_lines(page)
        return lines[line].get_text() if line < len(lines) else ""

    def find_text(self, text: str, page: int = None) -> List[Tuple[int, int, object]]:
        """
        Find all lines containing text.
        Returns: [(page_idx, line_idx, element), ...]
        If page is specified, only searches that page.
        """
        results = []
        pages = [page] if page is not None else range(len(self.get_pages()))
        for p in pages:
            for i, line in enumerate(self.get_lines(p)):
                if text in line.get_text():
                    results.append((p, i, line))
        return results

    # ─────────────────────────────────────────────────────────────────
    # CORE EDITING
    # ─────────────────────────────────────────────────────────────────

    def replace_text_tracked(self, old_text: str, new_text: str,
                              page: int = 0, occurrence: int = 0) -> bool:
        """
        Replace text with tracked changes: <del>old</del><ins>new</ins>

        This is the workhorse for appropriation edits.

        Args:
            old_text: Text to strike through
            new_text: Text to insert in its place
            page: Page index (0-based)
            occurrence: Which occurrence (0 = first match)

        Returns: True if replacement was made
        """
        found = 0
        for line in self.get_lines(page):
            for text_node in line.find_all(string=True):
                # Skip text inside existing del/ins tags
                if text_node.parent.name in ("del", "ins"):
                    # Still check — the text might be inside existing content
                    pass

                node_text = str(text_node)
                if old_text in node_text:
                    if found == occurrence:
                        self._splice_tracked(text_node, old_text, new_text)
                        return True
                    found += 1

        print(f"  Warning: Not found: '{old_text}' (occurrence {occurrence}, page {page})")
        return False

    def _splice_tracked(self, text_node, old_text: str, new_text: str):
        """Replace within a text node, preserving surrounding text."""
        full = str(text_node)
        idx = full.index(old_text)
        before = full[:idx]
        after = full[idx + len(old_text):]

        nodes = []
        if before:
            nodes.append(NavigableString(before))
        nodes.append(self._make_del(old_text))
        if new_text:  # allow empty new_text for pure deletion
            nodes.append(self._make_ins(new_text))
        if after:
            nodes.append(NavigableString(after))

        # Replace the original text node with the new node chain
        text_node.replace_with(nodes[0])
        current = nodes[0]
        for node in nodes[1:]:
            current.insert_after(node)
            current = node

    def insert_line(self, after_line: int, text: str, page: int = 0) -> bool:
        """
        Insert a new tracked line after the specified line index.
        Uses <p class="new-line"><ins ...>text</ins></p> format.
        """
        lines = self.get_lines(page)
        if after_line >= len(lines):
            return False

        new_p = self.soup.new_tag("p", attrs={"class": "new-line"})
        new_p.append(self._make_ins(text))
        lines[after_line].insert_after(new_p)
        return True

    def delete_line_tracked(self, line_idx: int, page: int = 0,
                             cid: str = None) -> bool:
        """
        Strike through all text on a line.
        Use shared cid to group multiple line deletions (like the editor does).
        """
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
        """
        Strike through a range of lines with a shared change ID.
        Returns the shared cid (useful for undo tracking).
        """
        shared_cid = self._new_cid()
        for i in range(start, end + 1):
            self.delete_line_tracked(i, page, cid=shared_cid)
        return shared_cid

    def strike_return(self, old_text: str, new_text: str, page: int = 0) -> bool:
        """
        Emulate the Strike/Return button for appropriation columns.
        Strikes old text and inserts new text on a new line below,
        tabbed to align with the original.
        """
        matches = self.find_text(old_text, page)
        if not matches:
            print(f"  Warning: Not found for strike/return: '{old_text}'")
            return False

        pg, line_idx, line = matches[0]

        # Strike the old text
        for text_node in line.find_all(string=True):
            if old_text in str(text_node):
                self._splice_tracked(text_node, old_text, "")
                break

        # Insert replacement on new line
        self.insert_line(line_idx, new_text, page)
        return True

    # ─────────────────────────────────────────────────────────────────
    # BATCH OPERATIONS
    # ─────────────────────────────────────────────────────────────────

    def batch_replace(self, replacements: List[Tuple[str, str]],
                       page: int = 0) -> list:
        """
        Apply multiple tracked replacements.

        Args:
            replacements: [(old_text, new_text), ...]

        Example:
            doc.batch_replace([
                ("54,000,000", "58,000,000"),
                ("$47,038,000", "$51,038,000"),
                ("16,000,000", "18,500,000"),
            ])
        """
        results = []
        for old, new in replacements:
            ok = self.replace_text_tracked(old, new, page)
            status = "OK" if ok else "FAIL"
            print(f"  [{status}] {old} -> {new}")
            results.append((old, new, ok))
        return results

    def apply_assembly_recs(self, changes: dict, page: int = 0):
        """
        Apply Assembly recommendation changes from a structured dict.

        Args:
            changes: {
                "21713": {"amount": "58,000,000", "reapprop": "$51,038,000"},
                "21856": {"amount": "18,500,000"},
                ...
            }
        """
        for code, edits in changes.items():
            print(f"\n  Appropriation ({code}):")

            # Find the line(s) with this code
            matches = self.find_text(f"({code})", page)
            if not matches:
                print(f"    Warning: Code ({code}) not found")
                continue

            pg, line_idx, line = matches[0]
            line_text = line.get_text()

            if "amount" in edits:
                new_amt = edits["amount"]
                # Find the dollar amount near this code
                # Check same line first (inline format: (23462) ... 750,000)
                amt_match = re.search(r'\.{2,}\s*([\d,]+)', line_text)
                if amt_match:
                    self.replace_text_tracked(amt_match.group(1), new_amt, page)
                else:
                    # Check next line (multiline format)
                    next_text = self.get_line_text(page, line_idx + 1)
                    amt_match = re.match(r'\s*([\d,]+)', next_text)
                    if amt_match:
                        self.replace_text_tracked(amt_match.group(1), new_amt, page)

            if "reapprop" in edits:
                new_re = edits["reapprop"]
                # Find (re. $X,XXX,XXX) near this code
                for check in range(line_idx, min(line_idx + 3, len(self.get_lines(page)))):
                    check_text = self.get_line_text(page, check)
                    re_match = re.search(r'\(re\.\s+\$[\d,]+\)', check_text)
                    if re_match:
                        self.replace_text_tracked(
                            re_match.group(0),
                            f"(re. {new_re})",
                            page
                        )
                        break

    # ─────────────────────────────────────────────────────────────────
    # SERIALIZATION
    # ─────────────────────────────────────────────────────────────────

    def to_html(self) -> str:
        """
        Serialize to HTML string for the generate-pdf API.
        Returns just the page div(s), not a full HTML document.
        """
        pages = self.get_pages()
        if pages:
            return "".join(str(p) for p in pages)
        # Fallback: return everything inside <body> if present
        body = self.soup.find("body")
        if body:
            return "".join(str(c) for c in body.children)
        return str(self.soup)

    def preview(self, page: int = 0, max_lines: int = 60):
        """Print human-readable preview with change markup visible."""
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
        """Print a summary of all changes in the document."""
        dels = self.soup.find_all("del")
        inss = self.soup.find_all("ins")
        print(f"Pages: {len(self.get_pages())}")
        print(f"Deletions: {len(dels)}")
        print(f"Insertions: {len(inss)}")
        if dels:
            print("\nDeletions:")
            for d in dels[:20]:
                txt = d.get_text()[:60]
                print(f"  - {txt}")
        if inss:
            print("\nInsertions:")
            for i in inss[:20]:
                txt = i.get_text()[:60]
                print(f"  + {txt}")


# =============================================================================
# OFFLINE MODE (work with captured HTML, no API calls)
# =============================================================================

def load_html_from_file(path: str) -> LBDCDocument:
    """Load HTML from a saved file for offline editing."""
    with open(path) as f:
        return LBDCDocument(f.read())


def save_html_to_file(doc: LBDCDocument, path: str):
    """Save the current HTML state to a file."""
    with open(path, "w") as f:
        f.write(doc.to_html())


# =============================================================================
# CLI
# =============================================================================

def main():
    """Command-line interface."""
    if len(sys.argv) < 2:
        print("""
LBDC PDF Editor - API Replay Toolkit v2
========================================

Usage:
  python lbdc_editor.py upload <pdf_file>           Upload PDF, save HTML
  python lbdc_editor.py preview <html_file>         Preview HTML with changes
  python lbdc_editor.py generate <html_file> <out>  Generate signed PDF from HTML
  python lbdc_editor.py pipeline <pdf> <out>        Full upload->edit->download
  python lbdc_editor.py demo                        Run demo with sample data
""")
        return

    cmd = sys.argv[1]

    if cmd == "demo":
        demo()
    elif cmd == "upload":
        client = LBDCClient()
        html = client.upload_pdf(sys.argv[2])
        out = sys.argv[3] if len(sys.argv) > 3 else "extracted.html"
        with open(out, "w") as f:
            f.write(html)
        print(f"[+] HTML saved to {out}")
    elif cmd == "preview":
        doc = load_html_from_file(sys.argv[2])
        doc.preview()
        doc.summary()
    elif cmd == "generate":
        client = LBDCClient()
        doc = load_html_from_file(sys.argv[2])
        out = sys.argv[3] if len(sys.argv) > 3 else "output.pdf"
        pdf = client.generate_pdf(doc.to_html())
        with open(out, "wb") as f:
            f.write(pdf)
        print(f"[+] PDF saved to {out}")
    elif cmd == "pipeline":
        print("Pipeline mode -- edit the edit_fn() in this script to define your changes")
    else:
        print(f"Unknown command: {cmd}")


def demo():
    """Run demo with sample appropriation HTML."""
    sample = """<div class="page" contenteditable="true">
<p>                          315                          12553-09-5</p>
<p style="min-height: 14.75px;"> </p>
<p>                       EDUCATION DEPARTMENT</p>
<p style="min-height: 14.75px;"> </p>
<p>            AID TO LOCALITIES - REAPPROPRIATIONS   2025-26</p>
<p style="min-height: 14.75px;"> </p>
<p> 1  ADULT CAREER AND CONTINUING EDUCATION SERVICES PROGRAM</p>
<p style="min-height: 14.75px;"> </p>
<p> 2    General Fund</p>
<p> 3    Local Assistance Account - 10000</p>
<p style="min-height: 14.75px;"> </p>
<p> 4  By chapter 53, section 1, of the laws of 2024:</p>
<p> 5    For case services provided  on or after October 1, 2022 to disabled</p>
<p> 6      individuals in accordance with economic eligibility criteria  devel-</p>
<p> 7      oped by the department (21713) .....................................</p>
<p> 8      54,000,000 ....................................... (re. $47,038,000)</p>
<p> 9    For services and expenses of independent living centers (21856) ......</p>
<p>10      16,000,000 ....................................... (re. $14,404,000)</p>
<p>11    For additional services and expenses of independent living centers</p>
<p>12      (23462) ... 750,000 ................................. (re. $750,000)</p>
</div>"""

    doc = LBDCDocument(sample, user_color="blue")

    print("=== BEFORE ===")
    doc.preview()

    print("\n=== APPLYING ASSEMBLY RECOMMENDATIONS ===")
    doc.batch_replace([
        ("54,000,000", "58,000,000"),
        ("$47,038,000", "$51,038,000"),
        ("16,000,000", "18,500,000"),
        ("$14,404,000", "$16,000,000"),
        ("750,000", "1,000,000"),
    ])

    print("\n=== AFTER ===")
    doc.preview()

    print("\n=== CHANGE SUMMARY ===")
    doc.summary()

    # Show that the HTML is valid
    html = doc.to_html()
    print(f"\n=== OUTPUT HTML: {len(html)} chars ===")
    print("(Ready to POST to /generate-pdf/)")


if __name__ == "__main__":
    main()
