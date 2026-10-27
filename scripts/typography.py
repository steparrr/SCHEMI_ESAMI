from bs4 import BeautifulSoup

def apply_v4_typography(article_html):
    soup = BeautifulSoup(article_html, "html.parser")
    article = soup.find("article", class_="study-schema")

    if article is None:
        return article_html

    def add_style(tag, rules):
        current = (tag.get("style") or "").strip()
        if current and not current.endswith(";"):
            current += ";"
        tag["style"] = current + rules

    # H1 — titolo dello schema
    h1 = article.find("h1")
    if h1:
        h1["class"] = list(dict.fromkeys((h1.get("class") or []) + ["schema-title"]))
        add_style(
            h1,
            "color:#991B1B;"
            "font-size:21pt;"
            "line-height:1.14;"
            "font-weight:800;"
            "margin:0 0 9mm;"
            "padding-bottom:4mm;"
            "border-bottom:2.2px solid #C98B8B;"
        )

    # H2 diretti dell'article = macro-argomenti.
    # Il primo continua normalmente; tutti i successivi iniziano su nuova pagina.
    macro_h2 = article.find_all("h2", recursive=False)

    for i, h2 in enumerate(macro_h2):
        classes = h2.get("class") or []
        if "macro-topic" not in classes:
            classes.append("macro-topic")
        h2["class"] = classes

        rules = (
            "color:#B42318;"
            "font-size:15pt;"
            "line-height:1.22;"
            "font-weight:800;"
            "margin:7mm 0 3mm;"
            "break-after:avoid;"
        )

        if i > 0:
            rules += (
                "break-before:page;"
                "page-break-before:always;"
                "margin-top:0;"
            )

        add_style(h2, rules)

    # H3 — sottosezioni
    for h3 in article.find_all("h3"):
        add_style(
            h3,
            "color:#2C5D7C;"
            "font-size:11.5pt;"
            "line-height:1.24;"
            "font-weight:750;"
            "margin:5mm 0 2mm;"
            "break-after:avoid;"
        )

    # H4 — livello minore
    for h4 in article.find_all("h4"):
        add_style(
            h4,
            "color:#607487;"
            "font-size:10.2pt;"
            "line-height:1.25;"
            "font-weight:700;"
            "margin:4mm 0 1.8mm;"
            "break-after:avoid;"
        )

    return str(article)
