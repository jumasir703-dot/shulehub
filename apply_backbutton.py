r"""
apply_backbutton.py - adds a "Back" button to every ShuleHub page except Home.

Run it from your ShuleHub folder (the one with app.py and templates):

    python apply_backbutton.py          # add the Back button
    python apply_backbutton.py --off    # take it out again

The button sits under the top bar, goes to the page you were on before, and takes you Home if
there is no earlier page (for example when ShuleHub has just been opened). It is hidden when
printing. Safe to run twice. A backup is saved once as base.html.bak_back.
"""
import os, re, shutil, sys

START, END = "<!-- shulehub-backbutton -->", "<!-- /shulehub-backbutton -->"
FRAGMENT = """{START}
<script>
  /* ShuleHub keeps its own list of the pages you opened in this window (it does not rely on the
     window's history, which some desktop windows ignore). The Back button uses that list. */
  (function(){
    var KEY = 'sh_stack', cur = location.pathname + location.search, stack = [];
    try { stack = JSON.parse(sessionStorage.getItem(KEY) || '[]'); } catch (e) {}
    if (!Array.isArray(stack)) stack = [];
    var n = stack.length, nav = (performance.getEntriesByType && performance.getEntriesByType('navigation')[0]) || {};
    if (n && stack[n - 1] === cur) { /* reload, or we arrived here with the Back button */ }
    else if (nav.type === 'back_forward' && n > 1 && stack[n - 2] === cur) { stack.pop(); }   /* Alt+Left, mouse back button */
    else { stack.push(cur); if (stack.length > 50) stack.shift(); }
    try { sessionStorage.setItem(KEY, JSON.stringify(stack)); } catch (e) {}
    window.shBack = function(){
      var s = [];
      try { s = JSON.parse(sessionStorage.getItem(KEY) || '[]'); } catch (e) {}
      if (s.length && s[s.length - 1] === cur) s.pop();
      var target = s.length ? s[s.length - 1] : "{{ url_for('home') }}";
      try { sessionStorage.setItem(KEY, JSON.stringify(s)); } catch (e) {}
      window.location.assign(target);
    };
  })();
</script>
{% if request.endpoint and request.endpoint != 'home' %}
<style>
  .backbar{ max-width:1180px; margin:0 auto 6px; padding:0 24px; }
  .backbar .back-btn{ display:inline-flex; align-items:center; gap:6px; cursor:pointer; font:inherit; font-weight:600;
    padding:8px 16px; border-radius:999px; border:1px solid var(--line,#d9d6c8); background:#fff;
    color:var(--green-900,#103524); box-shadow:0 2px 8px rgba(0,0,0,.08); position:relative; z-index:5; }
  .backbar .back-btn:hover{ filter:brightness(.97); }
  @media print{ .backbar{ display:none; } }
</style>
<div class="backbar no-print"><button type="button" class="back-btn" id="back-btn" onclick="window.shBack && window.shBack(); return false;" title="Go back to the previous page">&#8592; Back</button></div>
{% endif %}
{END}
""".replace("{START}", START).replace("{END}", END)


def main():
    path = next((p for p in (os.path.join("templates", "base.html"), os.path.join("ShuleHub", "templates", "base.html"))
                 if os.path.exists(p)), None)
    if not path:
        sys.exit("Run this from the ShuleHub folder (the one with app.py and the templates folder).\nCurrent folder: " + os.getcwd())
    t = open(path, encoding="utf-8", newline="").read()
    nl = "\r\n" if "\r\n" in t else "\n"
    clean = re.sub(re.escape(START) + r".*?" + re.escape(END) + r"\r?\n?", "", t, flags=re.S)
    if "--off" in sys.argv:
        if clean == t:
            return print("The Back button is not installed.")
        new, msg = clean, "Back button removed."
    else:
        m = re.search(r'^[ \t]*<main class="content">', clean, re.M)
        if not m:
            sys.exit('Could not find <main class="content"> in base.html. Send base.html to Claude.')
        new = clean[:m.start()] + FRAGMENT.replace("\n", nl) + clean[m.start():]
        msg = "Back button added." if clean == t else "Back button updated."
    if not os.path.exists(path + ".bak_back"):
        shutil.copyfile(path, path + ".bak_back")
    open(path, "w", encoding="utf-8", newline="").write(new)
    print(msg + " Close ShuleHub completely and open it again.")


main()
