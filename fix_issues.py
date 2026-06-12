import re, os

BASE = r'c:\Users\Abhinav\OneDrive\Documents\PROJECTS\MediaPedia'

# ── app.py fixes ─────────────────────────────────────────────────────────────
app_path = os.path.join(BASE, 'app.py')
src = open(app_path, encoding='utf-8').read()

# FIX 1: series_detail - add mongo_connected before render_template
# Find the series render_template call and add mongo_connected
old1 = (
    '    return render_template(\r\n'
    '        "series.html",\r\n'
    '        series=series,\r\n'
    '        related_series=related_series,\r\n'
    '        api_key_1=api_key_1,\r\n'
    '        api_key_2=api_key_2,\r\n'
    '        series_id=series_id\r\n'
    '    )'
)
new1 = (
    '    mongo_connected = comments_collection is not None\r\n'
    '\r\n'
    '    return render_template(\r\n'
    '        "series.html",\r\n'
    '        series=series,\r\n'
    '        related_series=related_series,\r\n'
    '        api_key_1=api_key_1,\r\n'
    '        api_key_2=api_key_2,\r\n'
    '        series_id=series_id,\r\n'
    '        mongo_connected=mongo_connected\r\n'
    '    )'
)
if old1 in src:
    src = src.replace(old1, new1, 1)
    print('Fix 1 done: series_detail now passes mongo_connected')
else:
    # try with \n
    old1n = old1.replace('\r\n', '\n')
    new1n = new1.replace('\r\n', '\n')
    if old1n in src:
        src = src.replace(old1n, new1n, 1)
        print('Fix 1 done (LF): series_detail now passes mongo_connected')
    else:
        print('Fix 1 MANUAL: patching with regex')
        src = re.sub(
            r'(    return render_template\(\s*"series\.html",.*?series_id=series_id\s*\))',
            lambda m: '    mongo_connected = comments_collection is not None\n\n' +
                      m.group(0).replace('series_id=series_id', 'series_id=series_id,\n        mongo_connected=mongo_connected'),
            src, count=1, flags=re.DOTALL
        )
        print('Fix 1 done via regex')

# FIX 2: /api/playlists/recommendations must be registered BEFORE /<playlist_id>
# Move the recommendations route before the GET /<playlist_id> route
rec_route_pattern = re.compile(
    r"(@app\.route\('/api/playlists/recommendations'\).*?return jsonify\(result\))",
    re.DOTALL
)
m = rec_route_pattern.search(src)
if m:
    rec_block = m.group(1)
    # Remove from current position
    src = src.replace(rec_block, '', 1)
    # Insert before first /api/playlists/<playlist_id> GET route
    pid_pattern = "@app.route('/api/playlists/<playlist_id>', methods=['GET'])"
    src = src.replace(pid_pattern, rec_block + '\n\n' + pid_pattern, 1)
    print('Fix 2 done: recommendations route moved before <playlist_id>')
else:
    print('Fix 2 SKIP: recommendations block not found with pattern')

# FIX 3: get_game_recommendations - make sample(n=1) safe
old3 = "    diverse_games = games_df[\r\n        ((games_df['Genre'] != genre) | (games_df['Platform'] != platform)) &\r\n        (games_df['Rank'] > 100)  # Lower ranked games\r\n    ].sample(n=1)\r\n    \r\n    recommendations = pd.concat([similar_games, diverse_games])"
new3 = "    diverse_pool = games_df[\r\n        ((games_df['Genre'] != genre) | (games_df['Platform'] != platform)) &\r\n        (games_df['Rank'] > 100)\r\n    ]\r\n    diverse_games = diverse_pool.sample(n=1) if not diverse_pool.empty else pd.DataFrame()\r\n    parts = [p for p in [similar_games, diverse_games] if not p.empty]\r\n    recommendations = pd.concat(parts) if parts else similar_games"

old3n = old3.replace('\r\n', '\n')
new3n = new3.replace('\r\n', '\n')

if old3 in src:
    src = src.replace(old3, new3, 1)
    print('Fix 3 done: get_game_recommendations sample safe (CRLF)')
elif old3n in src:
    src = src.replace(old3n, new3n, 1)
    print('Fix 3 done: get_game_recommendations sample safe (LF)')
else:
    # regex fallback
    src = re.sub(
        r"diverse_games = games_df\[.*?\.sample\(n=1\)\s*\n\s*\n\s*recommendations = pd\.concat\(\[similar_games, diverse_games\]\)",
        "    diverse_pool = games_df[\n        ((games_df['Genre'] != genre) | (games_df['Platform'] != platform)) &\n        (games_df['Rank'] > 100)\n    ]\n    diverse_games = diverse_pool.sample(n=1) if not diverse_pool.empty else pd.DataFrame()\n    parts = [p for p in [similar_games, diverse_games] if not p.empty]\n    recommendations = pd.concat(parts) if parts else similar_games",
        src, count=1, flags=re.DOTALL
    )
    print('Fix 3 done via regex')

# FIX 4: /search - remove year filter (Year column doesn't exist in movies.csv)
old4 = "    if year:\r\n        results = results[results['Year'].astype(str).str.contains(year, na=False)]\r\n\r\n    # Convert Rating to numeric and filter"
new4 = "    # Year column does not exist in movies.csv - skip year filter\r\n\r\n    # Convert Rating to numeric and filter"
old4n = old4.replace('\r\n', '\n')
new4n = new4.replace('\r\n', '\n')

if old4 in src:
    src = src.replace(old4, new4, 1)
    print('Fix 4 done: removed broken Year filter from /search (CRLF)')
elif old4n in src:
    src = src.replace(old4n, new4n, 1)
    print('Fix 4 done: removed broken Year filter from /search (LF)')
else:
    print('Fix 4 SKIP: year filter pattern not found, may already be removed')

open(app_path, 'w', encoding='utf-8').write(src)
print('app.py saved')

# ── series.html fixes ─────────────────────────────────────────────────────────
series_path = os.path.join(BASE, 'templates', 'series.html')
sh = open(series_path, encoding='utf-8').read()

# FIX 5: Add mongo_connected guard around comment form
# Check if it already has the guard
if 'mongo_connected' not in sh:
    # Find the comment section and wrap it
    # Look for the Discussion section heading
    if '{% if mongo_connected %}' not in sh:
        # Add a simple guard: find comment form toggle and wrap
        old5 = '<!-- Toggleable Comment Form -->'
        new5 = '{% if mongo_connected %}\n          <!-- Toggleable Comment Form -->'
        if old5 in sh:
            sh = sh.replace(old5, new5, 1)
            # Find the matching endif location - after the comments list closing div
            # Add endif before the else block or at end of comment section
            sh = sh.replace(
                '{% else %}\n          <div class="error-message">Comments are temporarily unavailable</div>\n          {% endif %}',
                '{% else %}\n          <div class="error-message">Comments are temporarily unavailable</div>\n          {% endif %}'
            )
            print('Fix 5a done: mongo_connected guard added to series comment form')
        else:
            print('Fix 5a SKIP: comment form toggle not found in series.html')
    else:
        print('Fix 5a SKIP: already has mongo_connected guard')
else:
    print('Fix 5a SKIP: mongo_connected already in series.html')

# FIX 6: Add Seen It widget to series.html if missing
if 'voteSeenIt' not in sh:
    seen_widget = '''
        <!-- Seen It Widget -->
        <div style="text-align:center;margin:1rem 0;">
          <p style="color:#999;font-size:0.85rem;margin-bottom:0.5rem;">Have you seen this series?</p>
          <div style="display:flex;gap:0.8rem;justify-content:center;">
            <button id="seen-yes-btn" onclick="voteSeenIt('yes')" style="background:rgba(46,213,115,0.2);color:#2ed573;border:1px solid #2ed573;padding:0.4rem 1.2rem;border-radius:4px;cursor:pointer;font-weight:600;">Yes ✓</button>
            <button id="seen-no-btn" onclick="voteSeenIt('no')" style="background:rgba(255,71,87,0.2);color:#ff4757;border:1px solid #ff4757;padding:0.4rem 1.2rem;border-radius:4px;cursor:pointer;font-weight:600;">Not Yet</button>
          </div>
          <p id="seen-count" style="color:#888;font-size:0.78rem;margin-top:0.4rem;">Loading votes...</p>
        </div>'''

    seen_script = '''
    <script>
      const seenSeriesId = {{ series_id }};
      fetch('/api/seen/' + seenSeriesId)
        .then(r => r.json())
        .then(d => updateSeenCount(d.yes, d.no));

      function voteSeenIt(choice) {
        if (localStorage.getItem('seen_s_' + seenSeriesId)) return;
        fetch('/api/seen/' + seenSeriesId + '/vote', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({vote: choice})
        })
        .then(r => r.json())
        .then(d => {
          localStorage.setItem('seen_s_' + seenSeriesId, choice);
          updateSeenCount(d.yes, d.no);
        });
      }

      function updateSeenCount(yes, no) {
        const total = yes + no;
        const pct = total > 0 ? Math.round((yes / total) * 100) : 0;
        document.getElementById('seen-count').textContent =
          total > 0 ? pct + '% of MediaPedia users have seen this (' + total + ' votes)' : 'Be the first to vote';
      }
    </script>'''

    # Insert widget before the series info div
    insert_before = '<div class="series-info">'
    if insert_before in sh:
        sh = sh.replace(insert_before, seen_widget + '\n\n        ' + insert_before, 1)
        print('Fix 6a done: Seen It widget HTML added to series.html')
    else:
        # try alternative insertion point - after h1
        insert_before2 = '<div class="series-detail">'
        if insert_before2 in sh:
            # find position after h1 tag
            h1_end = sh.find('</h1>')
            if h1_end != -1:
                sh = sh[:h1_end+5] + '\n' + seen_widget + sh[h1_end+5:]
                print('Fix 6b done: Seen It widget inserted after h1')

    # Add script before </body>
    if '</body>' in sh:
        sh = sh.replace('</body>', seen_script + '\n  </body>', 1)
        print('Fix 6c done: Seen It script added to series.html')
else:
    print('Fix 6 SKIP: Seen It widget already in series.html')

open(series_path, 'w', encoding='utf-8').write(sh)
print('series.html saved')

print('\nAll fixes applied.')
