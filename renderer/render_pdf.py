"""Renderer locale derivato dalla v5 allegata. Nessuna funzione OpenAI."""
import copy, os, re, urllib.parse
from pathlib import Path
from bs4 import BeautifulSoup
import pymupdf
OUTPUT=Path(os.environ.get("SCHEMI_EXAM_DIR", str(Path(__file__).resolve().parents[1]))) / "output"
FLOW_ALLOWED_CLASSES = {
    "flow-title", "flow-node", "flow-arrow", "flow-decision",
    "flow-branches", "flow-branch", "branch-label"
}
RECOVER_BLOCK_TAGS = {"h1","h2","h3","h4","h5","h6","p","ul","ol","table","figure","section","blockquote"}


def _has_class(tag, name):
    return name in set(tag.get("class") or [])


def _is_recoverable_block(tag):
    if not getattr(tag, "name", None):
        return False
    if tag.name in RECOVER_BLOCK_TAGS:
        return True
    if tag.name == "div" and (
        _has_class(tag, "callout") or
        _has_class(tag, "image-card") or
        _has_class(tag, "recognize-box")
    ):
        return True
    return False


def _nearest_flowchart_ancestor(tag):
    parent = tag.parent
    while parent is not None:
        if getattr(parent, "name", None) == "div" and _has_class(parent, "flowchart"):
            return parent
        parent = getattr(parent, "parent", None)
    return None


def _candidate_has_recoverable_ancestor_inside(tag, flow):
    parent = tag.parent
    while parent is not None and parent is not flow:
        if _is_recoverable_block(parent):
            return True
        parent = parent.parent
    return False


def _clean_text_leaks(article):
    suspicious_attr = re.compile(
        r'(?:(?:cl|w)?ass|href|style|data-[a-zA-Z0-9_-]+)\s*=\s*["\'][^"\']*["\']\s*>?',
        re.I,
    )
    dangling_tag = re.compile(r'</?[a-zA-Z][^>]{0,120}>')
    for node in list(article.find_all(string=True)):
        if node.parent and node.parent.name in {"script", "style"}:
            continue
        s = str(node)
        cleaned = suspicious_attr.sub("", s)
        cleaned = dangling_tag.sub("", cleaned)
        if cleaned != s:
            cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip()
            node.replace_with(cleaned)


def _repair_flowcharts(article):
    for flow in list(article.find_all("div", class_="flowchart")):
        anchor = flow

        # Never allow nested flowcharts.
        for inner in [x for x in flow.find_all("div", class_="flowchart") if x is not flow]:
            inner.extract()
            anchor.insert_after(inner)
            anchor = inner

        # Recover normal document blocks accidentally swallowed by an unclosed flowchart.
        candidates = []
        for tag in list(flow.find_all(True)):
            if _nearest_flowchart_ancestor(tag) is not flow:
                continue
            if not _is_recoverable_block(tag):
                continue
            if _candidate_has_recoverable_ancestor_inside(tag, flow):
                continue
            candidates.append(tag)

        anchor = flow
        for tag in candidates:
            tag.extract()
            anchor.insert_after(tag)
            anchor = tag

        # Unknown wrappers inside a chart cannot control the rest of the document.
        for div in list(flow.find_all("div")):
            if div is flow:
                continue
            classes = set(div.get("class") or [])
            if classes & FLOW_ALLOWED_CLASSES:
                continue
            txt = div.get_text(" ", strip=True)
            if txt:
                div.clear()
                div["class"] = ["flow-node"]
                div.string = txt
            else:
                div.decompose()

        primitives = flow.find_all(
            "div",
            class_=lambda c: c and any(x in {"flow-node","flow-decision"} for x in (c if isinstance(c,list) else [c]))
        )
        if len(primitives) > 8:
            flow["class"] = list(dict.fromkeys((flow.get("class") or []) + ["flow-long"]))

        if not flow.get_text(" ", strip=True):
            flow.decompose()


def _normalize_tables(article):
    for table in article.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            table.decompose()
            continue
        counts = [len(r.find_all(["th","td"], recursive=False)) for r in rows]
        cols = max(counts) if counts else 0
        if cols == 0:
            table.decompose()
            continue

        for row, count in zip(rows, counts):
            for _ in range(cols - count):
                filler = BeautifulSoup("<td></td>", "html.parser").td
                row.append(filler)

        if cols > 5:
            table["class"] = list(dict.fromkeys((table.get("class") or []) + ["table-wide"]))
        elif cols >= 4:
            table["class"] = list(dict.fromkeys((table.get("class") or []) + ["table-compact"]))

        widths = []
        for ci in range(cols):
            lengths = []
            for row in rows:
                cells = row.find_all(["th","td"], recursive=False)
                if ci < len(cells):
                    lengths.append(min(80, max(4, len(cells[ci].get_text(" ", strip=True)))))
            widths.append(max(lengths or [8]))
        total = sum(widths) or cols
        percentages = [max(12, round(w / total * 100, 1)) for w in widths]
        s = sum(percentages)
        percentages = [round(x / s * 100, 1) for x in percentages]

        old = table.find("colgroup", recursive=False)
        if old:
            old.decompose()
        cg = BeautifulSoup("<colgroup></colgroup>", "html.parser").colgroup
        for pct in percentages:
            col = BeautifulSoup(f'<col style="width:{pct}%">', "html.parser").col
            cg.append(col)
        table.insert(0, cg)



def _normalize_typography(article):
    """
    Conservative cleanup only: remove characters/artifacts that commonly
    create ugly PDF text without rewriting medical content.
    """
    for node in list(article.find_all(string=True)):
        if node.parent and node.parent.name in {"script", "style"}:
            continue
        s = str(node)

        # Soft hyphens and zero-width characters can create apparent typos
        # such as words broken in the middle at page/column boundaries.
        s = s.replace("\u00ad", "")
        s = s.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
        s = s.replace("\ufeff", "")

        # Normalize spaces, but preserve meaningful paragraph structure because
        # each BeautifulSoup text node remains separate.
        s = re.sub(r"[ \t]{2,}", " ", s)
        s = re.sub(r"\s+([,.;:!?])", r"\1", s)
        s = re.sub(r"([(\[]) +", r"\1", s)
        s = re.sub(r" +([)\]])", r"\1", s)

        if s != str(node):
            node.replace_with(s)


def _split_long_tables(article, max_rows=4, max_chars=1050):
    """
    Split long tables into small autonomous tables with repeated headers.
    This avoids Chromium paged-media bugs where a row/table fragment can
    stretch to almost an entire page.
    """
    for table in list(article.find_all("table")):
        tbody = table.find("tbody", recursive=False)
        if tbody is None:
            # Normalize implicit bodies first.
            direct_rows = table.find_all("tr", recursive=False)
            if not direct_rows:
                continue
            tbody = BeautifulSoup("", "html.parser").new_tag("tbody")
            for r in direct_rows:
                # Keep direct TH-only first row as a header if there is no thead.
                if r.find("th") and table.find("thead", recursive=False) is None:
                    thead = BeautifulSoup("", "html.parser").new_tag("thead")
                    r.extract()
                    thead.append(r)
                    table.insert(0, thead)
                else:
                    r.extract()
                    tbody.append(r)
            table.append(tbody)

        rows = tbody.find_all("tr", recursive=False)
        if len(rows) <= max_rows:
            # Very verbose cells can still create an oversized fragment.
            total_chars = sum(len(r.get_text(" ", strip=True)) for r in rows)
            if total_chars <= max_chars:
                table["class"] = list(dict.fromkeys((table.get("class") or []) + ["table-chunk"]))
                continue

        chunks = []
        current = []
        current_chars = 0
        for r in rows:
            chars = len(r.get_text(" ", strip=True))
            if current and (len(current) >= max_rows or current_chars + chars > max_chars):
                chunks.append(current)
                current = []
                current_chars = 0
            current.append(r)
            current_chars += chars
        if current:
            chunks.append(current)

        if len(chunks) <= 1:
            table["class"] = list(dict.fromkeys((table.get("class") or []) + ["table-chunk"]))
            continue

        replacements = []
        for idx, chunk in enumerate(chunks):
            clone = copy.deepcopy(table)
            clone_body = clone.find("tbody", recursive=False)
            clone_rows = clone_body.find_all("tr", recursive=False)
            keep_start = sum(len(c) for c in chunks[:idx])
            keep_end = keep_start + len(chunk)
            for ri, rr in enumerate(list(clone_rows)):
                if not (keep_start <= ri < keep_end):
                    rr.decompose()

            classes = list(dict.fromkeys((clone.get("class") or []) + ["table-chunk"]))
            clone["class"] = classes

            if idx > 0:
                cont = BeautifulSoup("", "html.parser").new_tag("div")
                cont["class"] = ["table-continued"]
                cont.string = "Tabella — continua"
                replacements.append(cont)
            replacements.append(clone)

        anchor = table
        first = replacements[0]
        table.replace_with(first)
        anchor = first
        for item in replacements[1:]:
            anchor.insert_after(item)
            anchor = item



def repair_layout(article_html):
    soup = BeautifulSoup(article_html, "html.parser")
    article = soup.find("article", class_="study-schema")
    if article is None:
        article = soup.new_tag("article")
        article["class"] = ["study-schema"]
        for node in list(soup.contents):
            article.append(node.extract())
        soup.append(article)

    _clean_text_leaks(article)
    _normalize_typography(article)
    _repair_flowcharts(article)
    _normalize_tables(article)
    _split_long_tables(article)
    _normalize_tables(article)
    return str(article)


def validate_layout(article_html):
    soup = BeautifulSoup(article_html, "html.parser")
    article = soup.find("article", class_="study-schema")
    errors = []

    if article is None:
        errors.append("manca article.study-schema")
    else:
        for i, flow in enumerate(article.find_all("div", class_="flowchart"), start=1):
            forbidden = flow.find(["h1","h2","h3","h4","table","figure","section"])
            if forbidden:
                errors.append(f"flowchart {i} contiene <{forbidden.name}>")
            if flow.find("div", class_="flowchart"):
                errors.append(f"flowchart {i} contiene un flowchart annidato")

        for i, table in enumerate(article.find_all("table"), start=1):
            counts = [len(r.find_all(["th","td"], recursive=False)) for r in table.find_all("tr")]
            counts = [c for c in counts if c]
            if counts and len(set(counts)) > 1:
                errors.append(f"tabella {i} incoerente: {counts}")

        patterns = [
            re.compile(r'\b(?:cl|w)?ass\s*=\s*["\']', re.I),
            re.compile(r'\bhref\s*=\s*["\']', re.I),
            re.compile(r'</?[a-zA-Z][^>]{0,80}>'),
            re.compile(r'\bhttps?://', re.I),
            re.compile(r'\ufffd'),
            # Typical truncated-token artifact, e.g. "diagnosi pree/".
            re.compile(r'\b[A-Za-zÀ-ÖØ-öø-ÿ]{2,}/(?=\s|$|[.,;:!?])'),
        ]
        for node in article.find_all(string=True):
            if node.parent and node.parent.name in {"script","style"}:
                continue
            s = str(node)
            if any(rx.search(s) for rx in patterns):
                errors.append(f"testo/markup sospetto: {s[:120]!r}")
                break

    if errors:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        debug = OUTPUT / "_layout_error_debug.html"
        debug.write_text(full_html(str(article) if article else article_html, "Layout error"), encoding="utf-8")
        raise RuntimeError(
            "Controllo impaginazione fallito; PDF NON generato. "
            + " | ".join(errors)
            + f" | Debug: {debug}"
        )


# ---------- bibliography: all sources at the end ----------

def _clean_source_url(url):
    try:
        p = urllib.parse.urlparse(url)
        if p.scheme not in {"http", "https"}:
            return url
        q = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        q = [(k, v) for k, v in q if not k.lower().startswith("utm_")]
        return urllib.parse.urlunparse((
            p.scheme, p.netloc, p.path, p.params,
            urllib.parse.urlencode(q, doseq=True), p.fragment
        ))
    except Exception:
        return url


def _source_label(url, preferred=None):
    preferred = (preferred or "").strip()
    generic = {"fonte", "source", "fonte immagine", "link", "qui"}
    if preferred and preferred.lower() not in generic and len(preferred) <= 90:
        return preferred
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host or "Fonte esterna"
    except Exception:
        return "Fonte esterna"


def move_sources_to_end(article_html, local_image_records=None):
    """
    Removes inline citations/URLs from the study text and appends a single
    bibliography at the very end of the schema.
    """
    soup = BeautifulSoup(article_html, "html.parser")
    article = soup.find("article", class_="study-schema")
    if article is None:
        return article_html

    external = []
    image_external = []
    internal_images = []

    def add_source(bucket, url, label=None):
        if not url:
            return
        url = _clean_source_url(url.strip())
        if not url.startswith(("http://", "https://")):
            return
        item = (_source_label(url, label), url)
        if item not in bucket:
            bucket.append(item)

    # Record local-image provenance if still available at this stage.
    by_id = {r["id"]: r for r in (local_image_records or [])}
    for img in article.find_all("img"):
        lid = img.get("data-local-id")
        if lid and lid in by_id:
            rec = by_id[lid]
            item = (rec.get("source", "Fonte interna"), rec.get("location", ""))
            if item not in internal_images:
                internal_images.append(item)
        elif img.get("data-local-source"):
            item = (img.get("data-local-source"), img.get("data-local-location", ""))
            if item not in internal_images:
                internal_images.append(item)
            img.attrs.pop("data-local-source", None)
            img.attrs.pop("data-local-location", None)

    # Collect all normal hyperlinks and remove them from the body.
    for a in list(article.find_all("a", href=True)):
        href = a.get("href", "").strip()
        if not href.startswith(("http://", "https://")):
            continue
        label = a.get_text(" ", strip=True)
        classes = set(a.get("class") or [])
        if "image-source" in classes:
            add_source(image_external, href, label)
        else:
            add_source(external, href, label)
        a.decompose()

    # Collect web-image source pages.
    for fig in article.find_all("figure", class_="web-image-card"):
        page_url = fig.get("data-page-url")
        if page_url:
            add_source(image_external, page_url, None)
        fig.attrs.pop("data-page-url", None)

    # Catch leaked Markdown links and raw URLs.
    md_paren_re = re.compile(r'\(\s*\[([^\]]+)\]\((https?://[^)]+)\)\s*\)')
    md_re = re.compile(r'\[([^\]]+)\]\((https?://[^)]+)\)')
    paren_url_re = re.compile(r'\(\s*(https?://[^)]+)\s*\)')
    bare_url_re = re.compile(r'https?://[^\s<>"\]]+')

    for node in list(article.find_all(string=True)):
        if node.parent and node.parent.name in {"script", "style"}:
            continue

        s = str(node)
        changed = False

        def repl_md_paren(m):
            nonlocal changed
            changed = True
            add_source(external, m.group(2), m.group(1))
            return ""

        def repl_md(m):
            nonlocal changed
            changed = True
            add_source(external, m.group(2), m.group(1))
            return m.group(1)

        def repl_paren_url(m):
            nonlocal changed
            changed = True
            add_source(external, m.group(1), None)
            return ""

        s = md_paren_re.sub(repl_md_paren, s)
        s = md_re.sub(repl_md, s)
        s = paren_url_re.sub(repl_paren_url, s)

        for u in bare_url_re.findall(s):
            changed = True
            add_source(external, u.rstrip(".,;:)"), None)

        s = bare_url_re.sub("", s)

        if changed:
            s = re.sub(r'\s{2,}', ' ', s)
            s = re.sub(r'\s+([,.;:])', r'\1', s)
            s = re.sub(r'\(\s*\)', '', s)
            node.replace_with(s.strip())

    # Remove model-created bibliography headings to avoid duplication.
    for h in list(article.find_all(["h2", "h3"])):
        title = h.get_text(" ", strip=True).lower()
        if title in {
            "fonti", "fonti esterne", "fonti interne",
            "fonti immagini", "fonti delle immagini", "sources"
        }:
            h.decompose()

    # Append one final deterministic bibliography.
    if external or image_external or internal_images:
        section = soup.new_tag("section")
        section["class"] = ["sources-section"]

        h2 = soup.new_tag("h2")
        h2.string = "Fonti"
        section.append(h2)

        if external:
            h3 = soup.new_tag("h3")
            h3.string = "Fonti esterne"
            section.append(h3)

            ol = soup.new_tag("ol")
            ol["class"] = ["source-list"]
            for label, url in external:
                li = soup.new_tag("li")
                a = soup.new_tag("a", href=url)
                a.string = label
                li.append(a)
                ol.append(li)
            section.append(ol)

        if internal_images:
            h3 = soup.new_tag("h3")
            h3.string = "Immagini da fonti interne"
            section.append(h3)

            ul = soup.new_tag("ul")
            ul["class"] = ["source-list"]
            for source, location in internal_images:
                li = soup.new_tag("li")
                li.string = source + (f" — {location}" if location else "")
                ul.append(li)
            section.append(ul)

        if image_external:
            h3 = soup.new_tag("h3")
            h3.string = "Fonti immagini"
            section.append(h3)

            ol = soup.new_tag("ol")
            ol["class"] = ["source-list"]
            for label, url in image_external:
                li = soup.new_tag("li")
                a = soup.new_tag("a", href=url)
                a.string = label
                li.append(a)
                ol.append(li)
            section.append(ol)

        article.append(section)

    return str(article)


# ---------- HTML -> PDF ----------

CSS = r"""
:root{--ink:#1f2937;--navy:#16324f;--blue:#eaf4ff;--green:#eaf7ef;--amber:#fff5dc;--red:#fdecec;--purple:#f2edfb;--grid:#cbd5df}
@page{size:A4;margin:14mm 14mm 16mm 14mm}
*{box-sizing:border-box}
html,body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;color:var(--ink);font-size:10.1pt;line-height:1.38}
body{margin:0;background:white}
h1{color:var(--navy);font-size:21pt;line-height:1.14;margin:0 0 9mm;padding-bottom:4mm;border-bottom:2.2px solid #7aa7cf}
h2{color:var(--navy);font-size:14pt;line-height:1.22;margin:7mm 0 3mm;break-after:avoid}
h3{color:#2c4e6d;font-size:11.2pt;margin:5mm 0 2mm;break-after:avoid}
p{margin:0 0 2.6mm} ul,ol{margin:1.8mm 0 3mm;padding-left:6mm} li{margin-bottom:1.2mm} strong{color:#172a3c}
a{color:#255f91;text-decoration:none}.source{font-size:8pt;border:1px solid #c8d8e6;background:#f5f9fc;border-radius:4px;padding:.4mm 1.5mm;margin-left:1mm}
.callout{border-left:4px solid #7e8b98;background:#f5f7f9;padding:3.2mm 4mm;margin:4mm 0;border-radius:5px;break-inside:avoid}
.callout.key{background:var(--blue);border-color:#6ba6d9}.callout.warning{background:var(--red);border-color:#c96262}.callout.external{background:var(--green);border-color:#66a87b}.callout.discordance{background:var(--amber);border-color:#d5a53b}.callout.memory{background:var(--purple);border-color:#8a72b5}
table{width:100%;table-layout:fixed;border-collapse:collapse;margin:4mm 0 5mm;font-size:9pt;line-height:1.28;break-inside:auto}
thead{display:table-header-group}th{background:transparent;color:#183b56;font-weight:700;text-align:left;padding:2.4mm;overflow-wrap:break-word;word-break:normal;hyphens:none}
td{vertical-align:top;padding:2.3mm;overflow-wrap:break-word;word-break:normal;hyphens:none}tbody tr{break-inside:avoid-page;page-break-inside:avoid;height:auto!important;min-height:0!important}
table.table-chunk{break-inside:avoid-page;page-break-inside:avoid}
.table-continued{font-size:8pt;font-weight:700;color:#58718a;margin:2.5mm 0 1mm}
p,li{orphans:3;widows:3}
.flowchart{width:100%;margin:5mm auto 6mm;padding:4mm 5mm 5mm;border-radius:8px;break-inside:avoid-page;text-align:center;display:flow-root;overflow:hidden}
.flow-title{font-size:10.5pt;font-weight:750;margin-bottom:3mm;color:#1f3448}.flow-diagnostic{background:#f4f9fd;border:1px solid #a8c8e3}.flow-pathophys{background:#f5f0fb;border:1px solid #c0acd8}.flow-treatment{background:#f2faf4;border:1px solid #a9d1b5}.flow-warning{background:#fff5ef;border:1px solid #e4b28f}
.flow-node,.flow-decision{display:block;width:84%;margin:0 auto;padding:3mm 3.8mm;background:white;border:1.3px solid #87a3ba;border-radius:7px;font-size:9.6pt;font-weight:600;overflow-wrap:anywhere}
.flow-decision{border:1.5px solid #c28a34;background:#fffaf0}.flow-arrow{font-size:15pt;line-height:1;margin:1.3mm 0;color:#597a96;font-weight:700}
.flow-branches{display:grid;grid-template-columns:1fr 1fr;gap:4mm;margin-top:3mm;align-items:start}.flow-branch{padding:2mm;border-radius:6px;background:rgba(255,255,255,.65)}.flow-branch .flow-node{width:100%}
.branch-label{display:inline-block;min-width:13mm;padding:.7mm 1.6mm;border-radius:999px;font-weight:800;font-size:8pt;margin-bottom:1.5mm}.branch-label.yes{background:#dff3e6;color:#286a3f}.branch-label.no{background:#fce3e3;color:#8a3434}
figure.image-card{margin:5mm auto;padding:3.5mm;width:92%;border:1px solid #cbd6df;border-radius:8px;background:#fbfcfd;break-inside:avoid}
figure.image-card img{display:block;max-width:100%;max-height:105mm;margin:0 auto 2.5mm;object-fit:contain}figcaption{font-size:8.5pt;color:#5f6b7a;text-align:center;margin-bottom:2.5mm}
.recognize-box{margin-top:2.5mm;background:#fff7df;border-left:4px solid #d6a641;border-radius:4px;padding:2.7mm 3.4mm;text-align:left}.recognize-title{font-weight:800;color:#684f13;margin-bottom:1.3mm}.recognize-box ul{margin:0}
.web-image-slot{min-height:25mm;display:flex;align-items:center;justify-content:center;text-align:center;border:1px dashed #9facb7;border-radius:5px;background:#f4f6f8;color:#67727e;padding:4mm}.image-source{display:block;margin-top:2mm;text-align:right;font-size:7.5pt;color:#4c6f8f}
h2,h3,.callout,figure,.flowchart{break-inside:avoid}

.table-compact{font-size:8.7pt}
.table-wide{font-size:8.1pt;line-height:1.2}
.table-wide th,.table-wide td{padding:1.8mm 1.6mm}
.flow-long{break-inside:auto}
.flow-long .flow-node,.flow-long .flow-decision{width:90%}

.sources-section{
  break-before:auto;
  page-break-before:auto;
  margin-top:6mm;
  padding-top:4mm;
  border-top:2px solid #9eb9cf;
}
.sources-section h2{margin-top:0}
.sources-section h3{
  font-size:10.5pt;
  margin:4mm 0 2mm;
  color:#405d78;
}
.source-list{
  font-size:8.1pt;
  line-height:1.28;
  margin:1.5mm 0 4mm;
  padding-left:6mm;
}
.source-list li{margin-bottom:1.1mm}
.source-list a{
  color:#375f80;
  text-decoration:none;
}

@media print{
  body{-webkit-print-color-adjust:exact;print-color-adjust:exact}
  table{border-collapse:collapse!important}
  thead{display:table-header-group}
  tfoot{display:table-footer-group}
  tbody{display:table-row-group}
  th,td{hyphens:none!important;word-break:normal!important;overflow-wrap:break-word!important}
}
"""

def full_html(article_html, title):
    safe = re.sub(r"<[^>]+>","",title)
    return f'<!doctype html><html lang="it"><head><meta charset="utf-8"><title>{safe}</title><style>{CSS}</style></head><body>{article_html}</body></html>'

def render_pdf(article_html, pdf_path, title):
    validate_layout(article_html)
    html = full_html(article_html,title)
    native_css = """
      body { font-family: Helvetica, Arial, sans-serif; font-size: 10.5pt; line-height: 1.35; color: #1f2937; }
      h1 { color: #7a1922; font-size: 21pt; margin-bottom: 17pt; border-bottom: 2px solid #bb6470; }
      h2 { color: #9b2733; font-size: 15pt; margin-top: 19pt; margin-bottom: 8pt; }
      h3 { color: #2c4e6d; font-size: 11.5pt; margin-top: 14pt; margin-bottom: 6pt; }
      h4 { color: #5c6f80; font-size: 10.2pt; }
      p { margin: 0 0 8pt; } li { margin-bottom: 4pt; }
      table { border-collapse: collapse; width: 100%; font-size: 9pt; margin: 12pt 0; }
      th, td { border: none; padding: 6pt; vertical-align: top; background: transparent; }
      th { color: #183b56; }
      .callout { background: #eaf4ff; border-left: 3pt solid #6ba6d9; padding: 9pt; margin: 11pt 0; }
      .callout.warning { background: #fdecec; border-color: #c96262; }
      .callout.memory { background: #f2edfb; border-color: #8a72b5; }
      .flowchart { background: #f4f9fd; border: 1px solid #a8c8e3; padding: 8pt; margin: 12pt 0; }
      .flow-title { color: #1f3448; font-weight: bold; font-size: 10.5pt; margin-bottom: 5pt; }
      .flowchart img { width: 100%; height: auto; }
      figure.image-card { border: 1px solid #cbd6df; padding: 8pt; margin: 12pt 0; }
      figure.image-card img { max-width: 100%; height: auto; }
      figcaption, .image-source { color: #5f6b7a; font-size: 8pt; }
      .recognize-box { background: #fff7df; padding: 8pt; }
      .sources-section { border-top: 2px solid #9eb9cf; margin-top: 18pt; padding-top: 8pt; }
      .source-list { font-size: 8.5pt; }
    """
    if "break-before:auto" not in CSS:
        native_css += "\n.sources-section { page-break-before: always; }"
    story = pymupdf.Story(html=html, user_css=native_css)
    page = pymupdf.paper_rect("a4")
    content = pymupdf.Rect(40, 40, page.width - 40, page.height - 45)
    writer = pymupdf.DocumentWriter(str(pdf_path))
    try:
        story.write(writer, lambda number, filled: (page, content, None))
    finally:
        writer.close()
