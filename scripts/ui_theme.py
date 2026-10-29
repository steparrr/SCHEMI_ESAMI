"""Shared desktop palette and native Tk styles for the local Mac interface."""
from __future__ import annotations


COLORS = {
    "canvas": "#F3F6F7",
    "surface": "#FFFFFF",
    "surface_alt": "#EAF0F2",
    "ink": "#142C43",
    "muted": "#596F7D",
    "line": "#D9E2E6",
    "navy": "#142B40",
    "navy_hover": "#21415C",
    "navy_muted": "#B5C8D2",
    "teal": "#087D79",
    "teal_hover": "#0A6967",
    "teal_soft": "#DDF2EE",
    "amber": "#A76313",
    "amber_soft": "#FFF0D8",
    "red": "#A63A42",
    "red_soft": "#FCE8E9",
}


def install_theme(root):
    """Apply one readable palette without extra GUI packages or image assets."""
    from tkinter import ttk

    c = COLORS
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    root.configure(background=c["canvas"])
    root.option_add("*Font", "Helvetica 11")
    root.option_add("*Text.background", c["surface"])
    root.option_add("*Text.foreground", c["ink"])
    root.option_add("*Text.insertBackground", c["ink"])
    root.option_add("*Text.selectBackground", c["teal_soft"])
    root.option_add("*Text.selectForeground", c["ink"])

    style.configure("TFrame", background=c["surface"])
    style.configure("App.TFrame", background=c["canvas"])
    style.configure("Main.TFrame", background=c["surface"])
    style.configure("Sidebar.TFrame", background=c["navy"])
    style.configure("Card.TFrame", background=c["surface_alt"], borderwidth=1, relief="solid")
    style.configure("TLabel", background=c["surface"], foreground=c["ink"], font=("Helvetica", 11))
    style.configure("Title.TLabel", background=c["surface"], foreground=c["ink"], font=("Helvetica", 19, "bold"))
    style.configure("Section.TLabel", background=c["surface"], foreground=c["ink"], font=("Helvetica", 15, "bold"))
    style.configure("Eyebrow.TLabel", background=c["surface"], foreground=c["teal"], font=("Helvetica", 9, "bold"))
    style.configure("Muted.TLabel", background=c["surface"], foreground=c["muted"], font=("Helvetica", 10))
    style.configure("CardTitle.TLabel", background=c["surface_alt"], foreground=c["ink"], font=("Helvetica", 14, "bold"))
    style.configure("CardBody.TLabel", background=c["surface_alt"], foreground=c["muted"], font=("Helvetica", 10))
    style.configure("SidebarTitle.TLabel", background=c["navy"], foreground=c["surface"], font=("Helvetica", 17, "bold"))
    style.configure("Sidebar.TLabel", background=c["navy"], foreground=c["surface"], font=("Helvetica", 11))
    style.configure("SidebarMuted.TLabel", background=c["navy"], foreground=c["navy_muted"], font=("Helvetica", 10))
    style.configure("Badge.TLabel", background=c["teal_soft"], foreground=c["teal"], font=("Helvetica", 9, "bold"), padding=(9, 5))
    style.configure("AttentionBadge.TLabel", background=c["amber_soft"], foreground=c["amber"], font=("Helvetica", 9, "bold"), padding=(9, 5))
    style.configure("MutedBadge.TLabel", background=c["line"], foreground=c["muted"], font=("Helvetica", 9, "bold"), padding=(9, 5))
    style.configure("TButton", background=c["surface_alt"], foreground=c["ink"],
                    font=("Helvetica", 10, "bold"), borderwidth=0, relief="flat", padding=(12, 8))
    style.map("TButton", background=[("disabled", c["surface_alt"]), ("active", c["line"])],
              foreground=[("disabled", c["muted"])])
    style.configure("Accent.TButton", background=c["teal"], foreground=c["surface"], padding=(14, 9))
    style.map("Accent.TButton", background=[("disabled", c["line"]), ("active", c["teal_hover"])],
              foreground=[("disabled", c["muted"]), ("active", c["surface"])])
    style.configure("Sidebar.TButton", background=c["navy_hover"], foreground=c["surface"], padding=(11, 9))
    style.map("Sidebar.TButton", background=[("active", c["teal"])], foreground=[("active", c["surface"])])
    style.configure("TEntry", fieldbackground=c["surface"], foreground=c["ink"],
                    bordercolor=c["line"], lightcolor=c["line"], darkcolor=c["line"], padding=(7, 6))
    style.map("TEntry", bordercolor=[("focus", c["teal"])])
    style.configure("TCombobox", fieldbackground=c["surface"], background=c["surface"],
                    foreground=c["ink"], arrowcolor=c["ink"], padding=(7, 5))
    style.map("TCombobox", fieldbackground=[("readonly", c["surface"])],
              foreground=[("readonly", c["ink"])])
    style.configure("TRadiobutton", background=c["surface"], foreground=c["ink"], font=("Helvetica", 10))
    style.configure("TCheckbutton", background=c["surface"], foreground=c["ink"], font=("Helvetica", 10))
    style.configure("TNotebook", background=c["surface"], borderwidth=0, tabmargins=(0, 0, 0, 0))
    style.configure("TNotebook.Tab", background=c["surface_alt"], foreground=c["muted"],
                    font=("Helvetica", 10, "bold"), padding=(13, 10), borderwidth=0)
    style.map("TNotebook.Tab", background=[("selected", c["surface"]), ("active", c["teal_soft"])],
              foreground=[("selected", c["teal"]), ("active", c["ink"])])
    style.configure("Treeview", background=c["surface"], fieldbackground=c["surface"],
                    foreground=c["ink"], rowheight=33, borderwidth=0, font=("Helvetica", 10))
    style.map("Treeview", background=[("selected", c["teal_soft"])], foreground=[("selected", c["ink"])])
    style.configure("Treeview.Heading", background=c["surface_alt"], foreground=c["muted"],
                    font=("Helvetica", 10, "bold"), padding=(8, 9), borderwidth=0)
    style.map("Treeview.Heading", background=[("active", c["line"])])
    style.configure("TProgressbar", background=c["teal"], troughcolor=c["line"], borderwidth=0)
    return c


def vertical_scroll_area(parent):
    """Keep forms reachable when the Mac window is at its minimum height."""
    import tkinter as tk
    from tkinter import ttk

    canvas = tk.Canvas(parent, background=COLORS["surface"], highlightthickness=0, borderwidth=0)
    scroll = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
    body = ttk.Frame(canvas)
    window = canvas.create_window((0, 0), window=body, anchor="nw")
    canvas.configure(yscrollcommand=scroll.set)
    canvas.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    body.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
    return body
