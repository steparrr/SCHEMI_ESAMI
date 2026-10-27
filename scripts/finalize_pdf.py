from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

from bs4 import BeautifulSoup

from common import PROJECT, read_json
from render_mermaid import render_mermaid_blocks
from resolve_images import resolve_images
from typography import apply_v4_typography


def ensure_article(text):
    if "</article>" not in text.lower():
        raise ValueError("HTML incompleto: manca la chiusura dell'article.")
    soup = BeautifulSoup(text, "html.parser")
    article = soup.find("article", class_="study-schema")
    if article is None:
        raise ValueError("Manca <article class='study-schema'>.")
    for tag in article.find_all(["script", "iframe", "object", "embed", "link", "base"]):
        tag.decompose()
    for tag in article.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith("on"):
                del tag[attr]
    return str(article)


def add_pack_sources(article_html, pack):
    soup = BeautifulSoup(article_html, "html.parser")
    article = soup.find("article", class_="study-schema")
    section = article.find("section", class_="sources-section")
    if section is None:
        section = soup.new_tag("section", attrs={"class": "sources-section"})
        title = soup.new_tag("h2")
        title.string = "Fonti"
        section.append(title)
        article.append(section)
    sources = set()
    for chunk in pack.get("chunks", []):
        name = chunk.get("document_name", "")
        begin, end = chunk.get("page_start"), chunk.get("page_end")
        if begin:
            name += f" — p. {begin}" + (f"–{end}" if end and end != begin else "")
        if name:
            sources.add(name)
    for question in pack.get("questions", []):
        if question.get("source_document"):
            sources.add("Domande: " + question["source_document"])
    listing = soup.new_tag("ul", attrs={"class": "source-list"})
    for source in sorted(sources):
        item = soup.new_tag("li")
        item.string = source
        listing.append(item)
    if sources:
        heading = soup.new_tag("h3")
        heading.string = "Materiali del corso"
        section.append(heading)
        section.append(listing)
    for source in (pack.get("external_supplement") or {}).get("sources", []):
        paragraph = soup.new_tag("p", attrs={"class": "source-list"})
        link = soup.new_tag("a", href=source.get("url", ""))
        link.string = source.get("title") or "Fonte esterna"
        paragraph.append(link)
        section.append(paragraph)
    # La bibliografia è sempre l'ultimo blocco dell'article.
    section.extract()
    article.append(section)
    return str(article)


def finalize(input_path: Path, output: Path, title: str, root: Path, pack: dict, manifest: Path, project: Path = PROJECT, renderer_path=None):
    article = ensure_article(input_path.read_text(encoding="utf-8"))
    article = render_mermaid_blocks(article, project, root / "diagrams" / "generated", project / "config" / "mermaid.json")
    article, _ = resolve_images(article, manifest, root)
    # Restano soltanto immagini incorporate: nessun download al rendering.
    soup = BeautifulSoup(article, "html.parser")
    for image in soup.find_all("img"):
        if not str(image.get("src", "")).startswith("data:image/"):
            raise ValueError("Immagine non incorporata o non risolta: " + str(image.get("data-local-id", image.get("src", ""))))
    path = renderer_path or project / "renderer" / "render_pdf.py"
    spec = importlib.util.spec_from_file_location("study_renderer", path)
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    renderer.OUTPUT = root / "output"
    article = renderer.move_sources_to_end(article)
    exam = read_json(root / "esame.json", {})
    if (exam.get("pipeline") or {}).get("hide_sources_section"):
        source_soup = BeautifulSoup(article, "html.parser")
        for section in source_soup.select("section.sources-section"):
            section.decompose()
        article = str(source_soup)
    else:
        article = add_pack_sources(article, pack)
    article = apply_v4_typography(renderer.repair_layout(article))
    if pack.get("mode") == "veloce":
        soup = BeautifulSoup(article, "html.parser")
        for heading in soup.select("h2.macro-topic"):
            heading["style"] = str(heading.get("style", "")) + ";break-before:auto;page-break-before:auto;"
        # La sintesi conserva le dimensioni dei caratteri; toglie solo salti imposti.
        renderer.CSS += "\n.sources-section{break-before:auto;page-break-before:auto;margin-top:5mm}"
        article = str(soup)
    work = input_path.with_name(input_path.stem.replace(".raw", "") + ".final.html")
    work.write_text(article, encoding="utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.stem + ".tmp.pdf")
    renderer.render_pdf(article, temporary, title)
    temporary.replace(output)
    return work


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rendering PDF locale, senza API.")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--pack", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    pack = json.loads(args.pack.read_text(encoding="utf-8"))
    finalize(args.input, args.output, pack["scheme"]["titolo"], args.root, pack, args.manifest)
