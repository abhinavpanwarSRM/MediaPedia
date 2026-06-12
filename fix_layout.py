import re

path = 'templates/index.html'
src = open(path, encoding='utf-8').read()

# ── 1. Strip the old auth CSS from .navbar-auth block ────────────────────────
# We're moving auth out of the navbar, so remove those styles
src = re.sub(
    r'\s*\.navbar-scroll-zone \{.*?\}',
    '', src, count=1, flags=re.DOTALL
)
src = re.sub(
    r'\s*\.navbar-auth \{.*?\}',
    '', src, count=1, flags=re.DOTALL
)
src = re.sub(
    r'\s*\.navbar-auth a \{.*?\}',
    '', src, count=1, flags=re.DOTALL
)
src = re.sub(
    r'\s*\.navbar-auth a:hover \{ color: #fff; \}',
    '', src, count=1
)
src = re.sub(
    r'\s*\.navbar-auth \.auth-user \{.*?\}',
    '', src, count=1, flags=re.DOTALL
)
src = re.sub(
    r'\s*\.navbar-auth \.auth-reg \{.*?\}',
    '', src, count=1, flags=re.DOTALL
)

# ── 2. Add new CSS for the hero auth row + search bar ─────────────────────────
new_css = """
      /* ── Top scrolling bar ── */
      .navbar-scroll-zone {
        flex: 1;
        overflow: hidden;
        height: 100%;
        display: flex;
        align-items: center;
      }

      /* ── Hero auth row (above title) ── */
      .hero-auth {
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 1rem;
        margin-bottom: 1.2rem;
        flex-wrap: wrap;
      }
      .hero-auth a {
        color: #aaa;
        text-decoration: none;
        font-size: 0.85rem;
        transition: color 0.2s;
      }
      .hero-auth a:hover { color: #fff; }
      .hero-auth .auth-user { color: #e50914; font-weight: 700; font-size: 0.9rem; }
      .hero-auth .auth-reg  { color: #e50914; font-weight: 700; }
      .hero-auth .auth-sep  { color: #444; }

      /* ── Hero search bar (above Explore section) ── */
      .hero-search-wrap {
        width: 90%;
        max-width: 560px;
        margin: 2rem auto 0;
        position: relative;
      }
      .hero-search-wrap input {
        width: 100%;
        padding: 0.65rem 1rem 0.65rem 2.4rem;
        background: rgba(20,20,20,0.95);
        border: 1px solid rgba(255,255,255,0.15);
        border-radius: 6px;
        color: #fff;
        font-size: 0.9rem;
        outline: none;
        box-shadow: 0 4px 20px rgba(0,0,0,0.5);
        transition: border-color 0.2s;
      }
      .hero-search-wrap input:focus { border-color: #e50914; }
      .hero-search-icon {
        position: absolute;
        left: 0.7rem;
        top: 50%;
        transform: translateY(-50%);
        color: #888;
        pointer-events: none;
        font-size: 1rem;
      }
"""

# Insert before the closing </style> of the main style block (first </style>)
first_style_end = src.find('</style>')
src = src[:first_style_end] + new_css + src[first_style_end:]

# ── 3. Replace navbar HTML: remove auth from navbar, keep only scroll zone ────
old_navbar = re.compile(
    r'<div class="navbar-container">.*?</div>\s*</div>\s*</div>',
    re.DOTALL
)
m = old_navbar.search(src)
if m:
    # Extract the filter-tags inner HTML
    tags_m = re.search(r'(<div class="filter-tags">.*?</div></div>)', m.group(), re.DOTALL)
    tags_html = tags_m.group(1) if tags_m else '<div class="filter-tags"></div>'
    new_navbar = '''<div class="navbar-container">
      <div class="navbar-scroll-zone">''' + tags_html + '''</div>
    </div>'''
    src = src[:m.start()] + new_navbar + src[m.end():]
    print('navbar replaced')
else:
    print('navbar NOT FOUND')

# ── 4. Remove old standalone search bar div ───────────────────────────────────
old_search = re.compile(
    r'<!-- Global Search Bar -->\s*<div style="max-width:560px.*?</div>\s*</div>',
    re.DOTALL
)
m2 = old_search.search(src)
if m2:
    src = src[:m2.start()] + src[m2.end():]
    print('old search bar removed')
else:
    print('old search bar NOT FOUND')

# ── 5. Replace title-container: inject auth row inside it ─────────────────────
old_title = re.compile(
    r'<div class="title-container">\s*<h1',
    re.DOTALL
)
auth_row = '''{% if username %}
        <a href="/u/{{ username }}" class="auth-user">{{ username }}</a>
        <span class="auth-sep">|</span>
        <a href="/feed">Feed</a>
        <span class="auth-sep">|</span>
        <a href="/logout">Logout</a>
      {% else %}
        <a href="/feed">Feed</a>
        <span class="auth-sep">|</span>
        <a href="/login">Login</a>
        <span class="auth-sep">|</span>
        <a href="/register" class="auth-reg">Register</a>
      {% endif %}'''

new_title_start = '''<div class="title-container">
      <div class="hero-auth">
      ''' + auth_row + '''
      </div>
      <h1'''
src = old_title.sub(new_title_start, src, count=1)
print('title-container auth row injected')

# ── 6. Inject search bar after the subtitle, before end of title-container ────
# Find the subtitle div closing and insert the search bar after it
old_subtitle_close = re.compile(
    r'(<div class="subtitle">YOUR PERSONALIZED GUIDE</div>)',
    re.DOTALL
)
new_search_html = r'''\1

      <!-- Global Search Bar -->
      <div class="hero-search-wrap" style="position:relative;">
        <span class="hero-search-icon">&#128269;</span>
        <input id="global-search" type="text"
          placeholder="Search movies, series, artists, directors..."
          autocomplete="off"
          oninput="globalSearch(this.value)"
          onblur="setTimeout(()=>document.getElementById('search-dropdown').style.display='none',200)" />
        <div id="search-dropdown" style="position:absolute;width:100%;top:100%;left:0;display:none;background:rgba(18,18,18,0.98);border:1px solid rgba(255,255,255,0.1);border-radius:0 0 6px 6px;max-height:340px;overflow-y:auto;box-shadow:0 8px 24px rgba(0,0,0,0.6);z-index:200;"></div>
      </div>'''
src, n = old_subtitle_close.subn(new_search_html, src, count=1)
print('search bar injected after subtitle:', n, 'replacements')

open(path, 'w', encoding='utf-8').write(src)
print('Done — index.html saved')
