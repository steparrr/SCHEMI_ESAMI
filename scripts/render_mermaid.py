#!/usr/bin/env python3
import argparse
import base64
import html
import re
import textwrap
from io import BytesIO
from pathlib import Path
from bs4 import BeautifulSoup
import pymupdf
from PIL import Image

FLOW_CLASS = {
    "diagnostic": "flow-diagnostic",
    "pathophys": "flow-pathophys",
    "treatment": "flow-treatment",
    "warning": "flow-warning",
}


NODE = re.compile(r'([A-Za-z][A-Za-z0-9_]*)\s*(?:\["([^"]*)"\]|\{"([^"]*)"\}|\[([^\]]*)\])')
ID = re.compile(r'^\s*([A-Za-z][A-Za-z0-9_]*)')


def _diagram_image(code):
    """Render the documented flowchart subset with native SVG/PyMuPDF, never a browser."""
    nodes, edges = {}, []
    for raw in code.splitlines():
        line = raw.strip()
        if not line or line.startswith("%%") or line.startswith("flowchart "):
            continue
        for match in NODE.finditer(line):
            nodes[match.group(1)] = (next((x for x in match.groups()[1:] if x is not None), match.group(1)), bool(match.group(3)))
        pieces = line.split("-->")
        for left, right in zip(pieces, pieces[1:]):
            src = ID.search(left)
            dst = re.match(r'\s*(?:\|([^|]+)\|\s*)?([A-Za-z][A-Za-z0-9_]*)', right)
            if src and dst:
                edges.append((src.group(1), dst.group(2), dst.group(1) or ""))
    for src, dst, _ in edges:
        nodes.setdefault(src, (src, False))
        nodes.setdefault(dst, (dst, False))
    if not nodes or not edges:
        raise ValueError("Diagramma non interpretabile: servono nodi e frecce Mermaid.")
    depth = {key: 0 for key in nodes}
    for _ in range(len(nodes)):
        changed = False
        for src, dst, _ in edges:
            if depth[dst] <= depth[src] and depth[src] + 1 < len(nodes):
                depth[dst] = depth[src] + 1
                changed = True
        if not changed:
            break
    levels = {}
    for key in nodes:
        levels.setdefault(depth[key], []).append(key)
    width, gap, top, bottom = 1040, 34, 28, 30
    rows, y = [], top
    for level in sorted(levels):
        keys = levels[level]
        cols = len(keys)
        box_w = min(770, (width - gap * (cols + 1)) / cols)
        if box_w < 170:
            raise ValueError("Diagramma troppo ramificato per una pagina: suddividi il flusso.")
        texts = {}
        height = 0
        for key in keys:
            label, decision = nodes[key]
            wrap = max(16, int(box_w / 19))
            lines = textwrap.wrap(label, width=wrap, break_long_words=False) or [label]
            texts[key] = lines
            height = max(height, 30 + 31 * len(lines))
        boxes = {}
        for index, key in enumerate(keys):
            x = (width - (box_w * cols + gap * (cols - 1))) / 2 + index * (box_w + gap)
            boxes[key] = (x, y, box_w, height, texts[key])
        rows.append(boxes)
        y += height + 82
    boxes = {key: value for row in rows for key, value in row.items()}
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{int(y + bottom)}" viewBox="0 0 {width} {int(y + bottom)}">',
           '<defs><marker id="arrow" markerWidth="9" markerHeight="9" refX="8" refY="4.5" orient="auto"><path d="M0,0 L9,4.5 L0,9 Z" fill="#587895"/></marker></defs>',
           '<rect width="100%" height="100%" fill="#f6f9fc"/>']
    for src, dst, label in edges:
        a, b = boxes[src], boxes[dst]
        sx, sy = a[0] + a[2] / 2, a[1] + a[3]
        dx, dy = b[0] + b[2] / 2, b[1]
        mid = (sy + dy) / 2
        svg.append(f'<path d="M{sx:.1f},{sy:.1f} V{mid:.1f} H{dx:.1f} V{dy-7:.1f}" fill="none" stroke="#587895" stroke-width="3" marker-end="url(#arrow)"/>')
        if label:
            svg.append(f'<text x="{(sx+dx)/2+7:.1f}" y="{mid-7:.1f}" font-size="24" fill="#274d6e">{html.escape(label)}</text>')
    for key, (x, py, w, h, lines) in boxes.items():
        decision = nodes[key][1]
        svg.append(f'<rect x="{x:.1f}" y="{py:.1f}" width="{w:.1f}" height="{h:.1f}" rx="16" fill="{ "#fff7e8" if decision else "#ffffff" }" stroke="{ "#b77a22" if decision else "#86a6bd" }" stroke-width="3"/>')
        baseline = py + h / 2 - (len(lines)-1) * 15 + 8
        for index, line in enumerate(lines):
            svg.append(f'<text x="{x+w/2:.1f}" y="{baseline+index*31:.1f}" text-anchor="middle" font-size="26" font-family="Helvetica,Arial,sans-serif" fill="#19334a">{html.escape(line)}</text>')
    svg.append('</svg>')
    doc = pymupdf.open(stream="".join(svg).encode(), filetype="svg")
    try:
        pixmap = doc[0].get_pixmap(matrix=pymupdf.Matrix(1.6, 1.6), alpha=False)
        output = BytesIO()
        Image.open(BytesIO(pixmap.tobytes("png"))).convert("RGB").save(output, format="JPEG", quality=91, optimize=True)
        return output.getvalue()
    finally:
        doc.close()


def render_code(code, stem, project_root, diagrams_dir, config_path=None):
    diagrams_dir.mkdir(parents=True, exist_ok=True)

    (diagrams_dir / f"{stem}.mmd").write_text(code.strip() + "\n", encoding="utf-8")
    image = _diagram_image(code)
    (diagrams_dir / f"{stem}.jpg").write_bytes(image)
    return "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")


def make_img(soup, data_uri, title, max_height_mm=58, width="100%"):
    img = soup.new_tag("img")
    img["src"] = data_uri
    img["alt"] = title
    img["class"] = ["mermaid-svg"]
    img["style"] = (
        f"display:block;"
        f"width:{width};"
        f"max-height:{max_height_mm}mm;"
        f"object-fit:contain;"
        f"margin:0 auto;"
    )
    return img


def render_single_blocks(
    soup,
    project_root,
    diagrams_dir,
    config_path,
):
    blocks = list(soup.select("div.mermaid-block"))

    for idx, block in enumerate(blocks, start=1):
        pre = block.find("pre", class_="mermaid")

        if pre is None:
            raise RuntimeError(
                f"Mermaid block {idx}: manca <pre class='mermaid'>"
            )

        code = pre.get_text("\n").strip()

        if not code:
            raise RuntimeError(
                f"Mermaid block {idx}: codice vuoto"
            )

        title_node = block.find("div", class_="flow-title")
        title = (
            title_node.get_text(" ", strip=True)
            if title_node
            else f"Diagramma {idx}"
        )

        flow_type = (
            block.get("data-flow-type") or "diagnostic"
        ).strip()

        flow_css = FLOW_CLASS.get(
            flow_type,
            "flow-diagnostic",
        )

        data_uri = render_code(
            code,
            f"flow_{idx:02d}",
            project_root,
            diagrams_dir,
            config_path,
        )

        wrapper = soup.new_tag("div")
        wrapper["class"] = [
            "flowchart",
            flow_css,
            "mermaid-rendered",
        ]

        title_div = soup.new_tag("div")
        title_div["class"] = ["flow-title"]
        title_div.string = title
        wrapper.append(title_div)

        wrapper.append(
            make_img(
                soup,
                data_uri,
                title,
                max_height_mm=118,
                width="100%",
            )
        )

        block.replace_with(wrapper)


def render_boards(
    soup,
    project_root,
    diagrams_dir,
    config_path,
):
    boards = list(soup.select("div.mermaid-board"))

    for bidx, board in enumerate(boards, start=1):
        rows = board.find_all("pre", class_="mermaid-row")

        if not rows:
            raise RuntimeError(
                f"Mermaid board {bidx}: nessuna mermaid-row"
            )

        title_node = board.find("div", class_="flow-title")
        title = (
            title_node.get_text(" ", strip=True)
            if title_node
            else f"Diagramma {bidx}"
        )

        flow_type = (
            board.get("data-flow-type") or "diagnostic"
        ).strip()

        flow_css = FLOW_CLASS.get(
            flow_type,
            "flow-diagnostic",
        )

        wrapper = soup.new_tag("div")
        wrapper["class"] = [
            "flowchart",
            flow_css,
            "mermaid-rendered",
            "mermaid-board-rendered",
        ]

        title_div = soup.new_tag("div")
        title_div["class"] = ["flow-title"]
        title_div.string = title
        wrapper.append(title_div)

        for ridx, row in enumerate(rows, start=1):
            code = row.get_text("\n").strip()

            if not code:
                raise RuntimeError(
                    f"Mermaid board {bidx}, riga {ridx}: codice vuoto"
                )

            data_uri = render_code(
                code,
                f"board_{bidx:02d}_row_{ridx:02d}",
                project_root,
                diagrams_dir,
                config_path,
            )

            # Prima fascia leggermente più larga.
            width = "100%" if ridx == 1 else "88%"

            wrapper.append(
                make_img(
                    soup,
                    data_uri,
                    f"{title} — parte {ridx}",
                    max_height_mm=58,
                    width=width,
                )
            )

            if ridx < len(rows):
                arrow = soup.new_tag("div")
                arrow["class"] = ["flow-arrow"]
                arrow.string = "↓"
                wrapper.append(arrow)

        board.replace_with(wrapper)


def render_mermaid_blocks(
    article_html,
    project_root,
    diagrams_dir,
    config_path=None,
):
    soup = BeautifulSoup(article_html, "html.parser")

    # Prima i board, perché contengono più diagrammi coordinati.
    render_boards(
        soup,
        project_root,
        diagrams_dir,
        config_path,
    )

    # Poi gli eventuali diagrammi singoli.
    render_single_blocks(
        soup,
        project_root,
        diagrams_dir,
        config_path,
    )

    return str(soup)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input_html")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--project-root", default=".")
    ap.add_argument(
        "--diagrams-dir",
        default="diagrams/generated",
    )
    ap.add_argument(
        "--config",
        default="config/mermaid.json",
    )

    args = ap.parse_args()

    project_root = Path(args.project_root).resolve()

    html_text = Path(
        args.input_html
    ).read_text(encoding="utf-8")

    out_html = render_mermaid_blocks(
        html_text,
        project_root,
        project_root / args.diagrams_dir,
        project_root / args.config,
    )

    out = Path(args.output)
    out.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    out.write_text(
        out_html,
        encoding="utf-8",
    )

    print(f"Creato: {out}")


if __name__ == "__main__":
    main()
