"""
tracer.py
---------
Lightweight execution tracer for the Facebook Marketplace form-fill.

Facebook obfuscates and frequently changes its create-listing page, so the
single most common failure is "nothing fills in anymore": a label our selectors
look for got renamed and every field quietly misses. This tracer makes that
visible. It records each step, whether it succeeded, and -- crucially -- dumps
exactly what the page looked like (screenshot + HTML + a list of every visible
field label) at the moments that matter, so you can see WHERE it broke and WHAT
the new labels are.

Everything is written under  _debug/run_YYYYmmdd_HHMMSS/ :
    trace.log         step-by-step log (also echoed to the terminal)
    NN_<tag>.png      full-page screenshot at each dump point
    NN_<tag>.html     page HTML at each dump point
    fields_<tag>.txt  every visible input / dropdown / button on the page

Nothing here ever raises out of a step -- a failed step is logged and the run
keeps going so you still get a partially-filled form to finish by hand.
"""

from __future__ import annotations

import os
import time
import traceback
from datetime import datetime


# JS that lists every *visible* field on the page along with the label text a
# human (and our selectors) would use to find it. This is what you read to fix
# LABELS in poster.py when Facebook renames something.
_JS_FIELD_SNAPSHOT = r"""
() => {
  const isVisible = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 1 && r.height > 1 &&
           s.visibility !== 'hidden' && s.display !== 'none';
  };
  const labelOf = el => {
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const parts = lb.split(/\s+/).map(id => {
        const n = document.getElementById(id);
        return n ? n.innerText.trim() : '';
      }).filter(Boolean);
      if (parts.length) return parts.join(' ');
    }
    if (el.id) {
      const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l) return l.innerText.trim();
    }
    const wrap = el.closest('label');
    if (wrap) return wrap.innerText.trim();
    return '';
  };
  const clip = (s, n) => (s || '').replace(/\s+/g, ' ').trim().slice(0, n);

  const inputs = [...document.querySelectorAll(
      'input, textarea, [contenteditable="true"]')]
    .filter(isVisible).map(el => ({
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || '',
      label: clip(labelOf(el), 70),
      placeholder: clip(el.getAttribute('placeholder'), 70),
    }));

  const dropdowns = [...document.querySelectorAll(
      '[role="combobox"], [role="listbox"], [aria-haspopup="listbox"], select')]
    .filter(isVisible)
    .map(el => clip(el.getAttribute('aria-label') || el.innerText, 70))
    .filter(Boolean);

  const buttons = [...document.querySelectorAll('[role="button"], button')]
    .filter(isVisible)
    .map(el => clip(el.getAttribute('aria-label') || el.innerText, 50))
    .filter(Boolean);

  return { inputs, dropdowns, buttons, url: location.href, title: document.title };
}
"""


class Tracer:
    """Records steps and dumps page state so failures are easy to locate."""

    def __init__(self, base_dir: str = "./_debug", enabled: bool = True):
        self.enabled = enabled
        self.steps: list[tuple] = []   # (name, status, secs, detail)
        self.fields: list[tuple] = []  # (field_name, ok)
        self._n = 0
        if enabled:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.dir = os.path.join(os.path.abspath(base_dir), f"run_{stamp}")
            os.makedirs(self.dir, exist_ok=True)
            self._log_path = os.path.join(self.dir, "trace.log")
        else:
            self.dir = ""
            self._log_path = ""

    # -- output -------------------------------------------------------------
    def log(self, line: str = "") -> None:
        print(line)
        if self.enabled:
            try:
                with open(self._log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass

    # -- step timing --------------------------------------------------------
    def step(self, name: str) -> "_Step":
        """Use as: `with tracer.step('Fill price'):` -- logs OK/FAIL + timing
        and swallows exceptions so the run continues."""
        return _Step(self, name)

    # -- per-field results --------------------------------------------------
    def field(self, name: str, ok: bool) -> bool:
        self.fields.append((name, bool(ok)))
        self.log(f"      [{'OK ' if ok else 'MISS'}] {name}")
        return ok

    # -- page captures ------------------------------------------------------
    def dump(self, page, tag: str) -> None:
        """Save a screenshot + HTML of the current page state."""
        if not self.enabled or page is None:
            return
        self._n += 1
        base = os.path.join(self.dir, f"{self._n:02d}_{_safe(tag)}")
        try:
            page.screenshot(path=base + ".png", full_page=True)
        except Exception as e:
            self.log(f"      (screenshot failed: {e})")
        try:
            with open(base + ".html", "w", encoding="utf-8") as f:
                f.write(page.content())
        except Exception as e:
            self.log(f"      (html dump failed: {e})")
        self.log(f"      -> snapshot saved: {os.path.basename(base)}.png/.html")

    def snapshot_fields(self, page, tag: str = "fields") -> dict:
        """List every visible field on the page (the key diagnostic for label
        drift) -- echo it and save it to fields_<tag>.txt."""
        if page is None:
            return {}
        try:
            info = page.evaluate(_JS_FIELD_SNAPSHOT)
        except Exception as e:
            self.log(f"      (field snapshot failed: {e})")
            return {}
        lines = [f"PAGE: {info.get('title','')}  <{info.get('url','')}>",
                 "--- visible inputs ---"]
        for it in info.get("inputs", []):
            lines.append(f"  {it['tag']}[{it['type']}]  "
                         f"label={it['label']!r}  placeholder={it['placeholder']!r}")
        lines.append("--- dropdowns / comboboxes ---")
        for it in info.get("dropdowns", []):
            lines.append(f"  {it!r}")
        lines.append("--- buttons ---")
        for it in info.get("buttons", []):
            lines.append(f"  {it!r}")
        text = "\n".join(lines)
        if self.enabled:
            try:
                with open(os.path.join(self.dir, f"fields_{_safe(tag)}.txt"),
                          "w", encoding="utf-8") as f:
                    f.write(text + "\n")
            except Exception:
                pass
        self.log(text)
        return info

    # -- final report -------------------------------------------------------
    def summary(self) -> None:
        self.log("\n" + "=" * 64)
        self.log("  FILL TRACE SUMMARY")
        self.log("=" * 64)
        for name, status, secs, detail in self.steps:
            extra = f"  {detail}" if detail else ""
            self.log(f"  {status:4}  {name:<34} {secs:5.1f}s{extra}")
        if self.fields:
            ok = sum(1 for _, o in self.fields if o)
            self.log(f"\n  Fields filled: {ok}/{len(self.fields)}")
            missed = [n for n, o in self.fields if not o]
            if missed:
                self.log("  NOT FILLED:    " + ", ".join(missed))
                self.log("  ^ Check the fields_*.txt / screenshots above to see the")
                self.log("    current labels, then update LABELS in poster.py.")
        if self.enabled:
            self.log(f"\n  Full trace + screenshots saved in:\n  {self.dir}")


class _Step:
    def __init__(self, tracer: Tracer, name: str):
        self.t = tracer
        self.name = name

    def __enter__(self):
        self.start = time.time()
        self.t.log(f"\n> {self.name} ...")
        return self

    def __exit__(self, exc_type, exc, tb):
        secs = time.time() - self.start
        if exc_type is None:
            self.t.steps.append((self.name, "OK", secs, ""))
            self.t.log(f"  done: {self.name} ({secs:.1f}s)")
        else:
            detail = f"{exc_type.__name__}: {exc}"
            self.t.steps.append((self.name, "FAIL", secs, detail))
            self.t.log(f"  FAILED: {self.name} ({secs:.1f}s) -- {detail}")
            for ln in traceback.format_exception(exc_type, exc, tb):
                self.t.log("    " + ln.rstrip())
        return True   # swallow -- best-effort fill keeps going


def _safe(tag: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in tag)[:40]
