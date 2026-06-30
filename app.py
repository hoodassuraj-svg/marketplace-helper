"""
app.py — Marketplace Listing Helper
Double-click "Marketplace Helper.app" to launch. No terminal needed.
"""

import os, sys, queue, threading, tkinter as tk
from tkinter import messagebox

os.chdir(os.path.dirname(os.path.abspath(__file__)))

# ── Palette (matches the icon: deep blue + amber + white) ────────────────────
C = {
    "navy":      "#1E3A8A",   # icon deep-blue background
    "blue":      "#2563EB",   # mid blue
    "blue_lt":   "#3B82F6",   # lighter blue (icon glow)
    "amber":     "#F59E0B",   # icon location-pin gold
    "amber_h":   "#D97706",   # amber hover
    "white":     "#FFFFFF",
    "off_white": "#F1F5F9",   # page background
    "card":      "#FFFFFF",
    "border":    "#E2E8F0",
    "slate":     "#1E293B",   # terminal/log bg
    "slate_lt":  "#334155",   # log border
    "text":      "#0F172A",   # primary text
    "muted":     "#64748B",   # secondary text
    "green":     "#22C55E",
    "red":       "#EF4444",
    "yellow":    "#FBBF24",
}
FF = "Helvetica Neue" if sys.platform == "darwin" else "Segoe UI"


# ── stdout → queue ────────────────────────────────────────────────────────────
class _Writer:
    def __init__(self, q): self._q = q
    def write(self, t):
        if t and t != "\n" or t == "\n":
            self._q.put(t)
    def flush(self): pass


# ── App ───────────────────────────────────────────────────────────────────────
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Marketplace Helper")
        self.root.configure(bg=C["off_white"])
        self.root.resizable(False, False)
        self._q: queue.Queue = queue.Queue()
        self._busy = False
        self._build()
        self._enable_clipboard()
        self._bring_to_front()
        self._poll()

    # ── Force the window to the foreground (it opens behind apps when launched
    #    from a .app bundle, which looks like "nothing happened"). ─────────────
    def _bring_to_front(self):
        # Pure-Tkinter activation only — no osascript (which needs special
        # Automation permission under a Finder-launched .app and can fail there).
        self.root.update_idletasks()
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(500, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    # ── Fix macOS Tk 8.6 bug where Cmd+C/V/X/A don't work in text fields ──────
    def _enable_clipboard(self):
        for key, ev in (("<Command-c>", "<<Copy>>"),
                        ("<Command-x>", "<<Cut>>"),
                        ("<Command-v>", "<<Paste>>")):
            self.root.bind_all(
                key, lambda e, ev=ev: (e.widget.event_generate(ev), "break")[1])

        def _select_all(e):
            try:
                e.widget.select_range(0, "end")
                e.widget.icursor("end")
            except Exception:
                pass
            return "break"
        self.root.bind_all("<Command-a>", _select_all)

    # ── Layout ────────────────────────────────────────────────────────────────
    def _build(self):
        # ── Hero header (deep blue like the icon background) ──
        hero = tk.Frame(self.root, bg=C["navy"], pady=0)
        hero.pack(fill="x")

        # thin amber accent strip at very top
        tk.Frame(hero, bg=C["amber"], height=4).pack(fill="x")

        inner_hero = tk.Frame(hero, bg=C["navy"], padx=28, pady=20)
        inner_hero.pack(fill="x")

        tk.Label(inner_hero,
                 text="🏠  Marketplace Helper",
                 font=(FF, 22, "bold"),
                 fg=C["white"], bg=C["navy"]).pack(anchor="w")
        tk.Label(inner_hero,
                 text="Paste a listing URL · App fills Facebook · You publish the draft",
                 font=(FF, 11),
                 fg="#93C5FD",           # light blue — readable on dark bg
                 bg=C["navy"]).pack(anchor="w", pady=(3, 0))

        # ── URL input card ──
        url_wrap = tk.Frame(self.root, bg=C["off_white"], padx=18, pady=14)
        url_wrap.pack(fill="x")

        url_card = tk.Frame(url_wrap, bg=C["card"],
                            highlightbackground=C["border"],
                            highlightthickness=1, padx=20, pady=18)
        url_card.pack(fill="x")

        tk.Label(url_card, text="Listing URL",
                 font=(FF, 12, "bold"), fg=C["text"], bg=C["card"]).pack(anchor="w")
        tk.Label(url_card,
                 text="Paste a Realm or realtor.ca link below",
                 font=(FF, 10), fg=C["muted"], bg=C["card"]).pack(anchor="w", pady=(1, 8))

        # Entry — bordered, white bg, DARK text
        entry_wrap = tk.Frame(url_card,
                              bg=C["blue"], padx=2, pady=2)   # blue border on focus
        entry_wrap.pack(fill="x")
        self.url_var = tk.StringVar()
        self.url_entry = tk.Entry(
            entry_wrap,
            textvariable=self.url_var,
            font=(FF, 11),
            fg=C["text"],               # ← always-dark text
            bg=C["white"],
            insertbackground=C["navy"],
            relief="flat", bd=0,
        )
        self.url_entry.pack(fill="x", ipady=10, padx=8)
        self.url_entry.bind("<Return>", lambda _: self._start())

        # ── CTA button (amber, stands out like the pin in the icon) ──
        btn_row = tk.Frame(self.root, bg=C["off_white"])
        btn_row.pack(pady=(0, 14))

        self.btn = tk.Button(
            btn_row,
            text="  Post Listing  →",
            command=self._start,
            font=(FF, 13, "bold"),
            fg=C["navy"],               # dark navy text on amber
            bg=C["amber"],
            activeforeground=C["navy"],
            activebackground=C["amber_h"],
            relief="flat", bd=0,
            cursor="hand2",
            padx=32, pady=12,
        )
        self.btn.pack()

        # ── Progress / log  (dark "terminal" style — matches icon bg) ──
        log_wrap = tk.Frame(self.root, bg=C["off_white"], padx=18, pady=0)
        log_wrap.pack(fill="both", expand=True)

        log_header = tk.Frame(log_wrap, bg=C["slate_lt"], padx=14, pady=6)
        log_header.pack(fill="x")

        tk.Label(log_header, text="▸  Progress",
                 font=(FF, 10, "bold"),
                 fg=C["yellow"], bg=C["slate_lt"]).pack(side="left")
        tk.Button(log_header, text="Clear",
                  command=self._clear,
                  font=(FF, 9), fg=C["muted"], bg=C["slate_lt"],
                  relief="flat", bd=0, cursor="hand2",
                  activeforeground=C["white"],
                  activebackground=C["slate_lt"]).pack(side="right")

        # dark log body
        log_body = tk.Frame(log_wrap, bg=C["slate"])
        log_body.pack(fill="both", expand=True)

        self.log = tk.Text(
            log_body,
            font=("Courier", 10),
            fg="#CBD5E1",               # light slate text on dark bg
            bg=C["slate"],
            insertbackground=C["white"],
            relief="flat", bd=0,
            state="disabled",
            padx=14, pady=10,
            wrap="word",
            height=13,
        )
        self.log.pack(side="left", fill="both", expand=True)

        sb = tk.Scrollbar(log_body, bg=C["slate"], troughcolor=C["slate_lt"],
                          relief="flat")
        sb.pack(side="right", fill="y")
        self.log.config(yscrollcommand=sb.set)
        sb.config(command=self.log.yview)

        # colour tags for log lines
        self.log.tag_config("ok",   foreground=C["green"])
        self.log.tag_config("err",  foreground=C["red"])
        self.log.tag_config("warn", foreground=C["yellow"])
        self.log.tag_config("dim",  foreground=C["muted"])
        self.log.tag_config("hdr",  foreground=C["blue_lt"])

        # ── Status bar ──
        tk.Frame(self.root, bg=C["border"], height=1).pack(fill="x")
        status_bar = tk.Frame(self.root, bg=C["navy"], pady=6)
        status_bar.pack(fill="x")
        self.status_var = tk.StringVar(value="Ready")
        tk.Label(status_bar, textvariable=self.status_var,
                 font=(FF, 10), fg="#93C5FD", bg=C["navy"]).pack()

        self.root.geometry("600x680")
        self.url_entry.focus_set()

    # ── Log helpers ──────────────────────────────────────────────────────────
    def _poll(self):
        try:
            while True:
                text = self._q.get_nowait()
                self._append(text)
        except queue.Empty:
            pass
        self.root.after(50, self._poll)          # 50 ms — snappy updates

    def _append(self, text: str):
        tag = ("ok"   if any(w in text for w in ("✅", "ready", "uploaded", "saved")) else
               "err"  if any(w in text for w in ("❌", "Error", "error")) else
               "warn" if "⚠" in text else
               "hdr"  if any(w in text for w in ("Fetching", "Downloading", "Opening", "Filling", "Saving")) else
               "dim"  if (text.strip().startswith("─") or text.strip().startswith("→")) else "")
        self.log.config(state="normal")
        self.log.insert("end", text, tag)
        self.log.see("end")
        self.log.config(state="disabled")

    def _clear(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    # ── Button logic ─────────────────────────────────────────────────────────
    def _start(self):
        if self._busy:
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("No URL", "Please paste a listing URL first.")
            return
        self._clear()
        self._busy = True
        self.btn.config(state="disabled", text="  Working…", bg=C["slate_lt"],
                        fg=C["white"])
        self.status_var.set("Running…")
        threading.Thread(target=self._worker, args=(url,), daemon=True).start()

    def _worker(self, url: str):
        old = sys.stdout
        sys.stdout = _Writer(self._q)
        result = "fail"
        try:
            result = _run_poster(url)
        except Exception as e:
            print(f"\n❌ Unexpected error: {e}")
        finally:
            sys.stdout = old
            self._busy = False
            ok = result == "success"
            lbl = ("✅  Saved as draft — open Facebook Marketplace → Drafts to publish."
                   if ok else "⚠️  Check the progress log for details.")
            self.root.after(0, lambda: (
                self.btn.config(state="normal", text="  Post Another  →",
                                bg=C["amber"], fg=C["navy"]),
                self.status_var.set(lbl),
            ))


# ── Posting logic ─────────────────────────────────────────────────────────────
def _run_poster(url: str) -> str:
    print("Fetching listing details…\n")
    try:
        import config, scraper
        from poster import (get_listing, fill_marketplace_form, save_draft,
                            log_result, cleanup, MARKETPLACE_CREATE_URL,
                            select_dropdown, open_named_combobox)
        from tracer import Tracer
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"❌ Import error: {e}\n"
              "Run: pip install -r requirements.txt && playwright install chromium")
        return "fail"

    data = get_listing(url, manual=False)
    if not data:
        print("❌ Could not get listing data.")
        log_result(url, "fail")
        return "fail"

    description = config.apply_branding(data.description)
    print("─────────────────────────────────────────")
    print(description[:280] + ("…" if len(description) > 280 else ""))
    print("─────────────────────────────────────────\n")

    print(f"Downloading {len(data.photos)} photos…")
    data.photo_paths = scraper.download_photos(
        data.photos, config.TEMP_PHOTO_DIR, limit=config.MAX_PHOTOS)
    print(f"✅ {len(data.photo_paths)} photos ready.\n")

    result = "fail"
    tracer = Tracer()   # records each step + dumps the page when something breaks
    page = None
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                config.USER_DATA_DIR, headless=config.HEADLESS,
                viewport={"width": 1280, "height": 900})
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            with tracer.step("Open Facebook / login check"):
                page.goto("https://www.facebook.com/", wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                if "login" in page.url or page.locator("input[name='email']").count() > 0:
                    print(">>> Please log into Facebook in the browser.")
                    _ask("Log into Facebook in the browser,\nthen click OK to continue.")

            with tracer.step("Open Marketplace + pick Home category"):
                print("Opening Facebook Marketplace…")
                page.goto(MARKETPLACE_CREATE_URL, wait_until="domcontentloaded")
                page.wait_for_timeout(2500)

                tile = page.get_by_text("Home for sale or rent", exact=False)
                if tile.count() == 0:
                    for lbl in ("Home for Sale or Rent", "Property for sale"):
                        tile = page.get_by_text(lbl, exact=False)
                        if tile.count(): break
                if tile.count():
                    tile.first.click(); page.wait_for_timeout(2500)
                else:
                    print("  ! Couldn't find the Home tile — capturing the page.")
                    tracer.snapshot_fields(page, "no_home_tile")
                    tracer.dump(page, "no_home_tile")
                    _ask("Click 'Home for sale or rent' in the browser, then click OK.")

            with tracer.step("Set Sale/Rent type"):
                sor = "For Rent" if data.listing_type == "rent" else "For Sale"
                page.wait_for_timeout(1000)
                ok = select_dropdown(page, ["Home for Sale or Rent", "Sale or Rent"], sor)
                if not ok:
                    # Open the listing-type combobox BY NAME (never the Search box).
                    if open_named_combobox(page, ["home for sale or rent", "sale or rent"]):
                        page.wait_for_timeout(800)
                        try:
                            page.get_by_role("option", name=sor).first.click()
                            ok = True
                        except Exception:
                            print(f"  ! Set '{sor}' manually in the browser.")
                    else:
                        print(f"  ! Set '{sor}' manually in the browser.")
                tracer.field("sale_or_rent", ok)
                page.wait_for_timeout(1500)

            print("Filling form fields…")
            fill_marketplace_form(page, data, description, tracer)

            # Save draft BEFORE closing the context — gives FB time to register the click.
            saved = False
            with tracer.step("Save draft"):
                print("Saving as draft…")
                saved = save_draft(page)
                page.wait_for_timeout(2000)   # extra buffer after save

            if saved:
                result = "success"
            ctx.close()

    except Exception as e:
        print(f"❌ Error during Facebook step: {e}")
        tracer.dump(page, "error")
    finally:
        tracer.summary()
        log_result(url, result)
        cleanup()

    return result


# ── Thread-safe dialog ────────────────────────────────────────────────────────
_ev = threading.Event()

def _ask(msg: str):
    _ev.clear()
    def _show():
        messagebox.showinfo("Action needed", msg)
        _ev.set()
    try:
        _root.after(0, _show)
    except Exception:
        _ev.set()
    _ev.wait(timeout=300)


# ── Entry point ───────────────────────────────────────────────────────────────
_root: tk.Tk = None  # type: ignore

if __name__ == "__main__":
    _root = tk.Tk()
    App(_root)
    _root.mainloop()
