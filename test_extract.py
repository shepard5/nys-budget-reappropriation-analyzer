"""Quick test: extract reappropriations from the 26-27 executive PDF."""
from lbdc_editor import LBDCClient, LBDCDocument
from lbdc_extract import extract_from_html, print_extraction_report

EXEC_PDF = "C:/Users/samsc/Desktop/SED-ATL-2-House-Reapprops-S09003A_A10003A_pg268-352-lbdc_pdf_editor-2026-02-27-1618.pdf"

print("=== UPLOADING ===")
client = LBDCClient()
html = client.upload_pdf(EXEC_PDF)

# Cache HTML
with open("executive.html", "w", encoding="utf-8") as f:
    f.write(html)
print(f"Cached executive.html ({len(html)} chars)")

print("\n=== EXTRACTING ===")
result = extract_from_html(html, "exec_26-27")

print(f"\nTotal reappropriations: {len(result.reapprops)}  (expect 362)")
print(f"Structural elements: {len(result.structures)}")

print_extraction_report(result)

# First 5
print("\nFirst 5 reappropriations:")
for r in result.reapprops[:5]:
    aid = r.approp_id or "N/A"
    print(f"  ChYr {r.chapter_year} | ID {aid:>5} | ${r.reapprop_amount:>12,} | "
          f"pg{r.page_idx} p{r.p_start}-{r.p_end} | {r.bill_language[:60]}")

# Last 5
print("\nLast 5 reappropriations:")
for r in result.reapprops[-5:]:
    aid = r.approp_id or "N/A"
    print(f"  ChYr {r.chapter_year} | ID {aid:>5} | ${r.reapprop_amount:>12,} | "
          f"pg{r.page_idx} p{r.p_start}-{r.p_end} | {r.bill_language[:60]}")

# Structural elements summary
print("\nStructural elements:")
for s in result.structures[:20]:
    print(f"  [{s.elem_type:12}] pg{s.page_idx} p{s.p_idx} | {s.text[:70]}")
