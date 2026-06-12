import os, re

BASE = r'c:\Users\Abhinav\OneDrive\Documents\PROJECTS\MediaPedia'
issues = []

def warn(area, msg): issues.append(f'[ISSUE] {area}: {msg}')
def ok(area, msg): print(f'  [OK] {area}: {msg}')

app_src   = open(os.path.join(BASE,'app.py'),encoding='utf-8').read()
index_html= open(os.path.join(BASE,'templates','index.html'),encoding='utf-8').read()
movie_html= open(os.path.join(BASE,'templates','movie.html'),encoding='utf-8').read()
series_html=open(os.path.join(BASE,'templates','series.html'),encoding='utf-8').read()
era_html  = open(os.path.join(BASE,'templates','era.html'),encoding='utf-8').read()
artist_html=open(os.path.join(BASE,'templates','artist.html'),encoding='utf-8').read()
profile_html=open(os.path.join(BASE,'templates','profile.html'),encoding='utf-8').read()
pl_html   = open(os.path.join(BASE,'templates','playlist.html'),encoding='utf-8').read()

routes = re.findall(r"@app\.route\(['\"]([^'\"]+)['\"]", app_src)
print(f'\n=== ROUTES ({len(routes)}) ===')
for r in routes: print(f'  {r}')

print('\n=== CHECKS ===')

# auth: current_user defined before use
cu_def = app_src.find('def current_user()')
cu_first_use = app_src.find('current_user()')
if cu_def < cu_first_use: warn('auth','current_user() defined AFTER first use - will crash on startup')
else: ok('auth','current_user defined before use')

# home passes username
if 'username=current_user()' in app_src: ok('home','passes username to index.html')
else: warn('home','does not pass username to index.html')

# series passes mongo_connected and series_id
s_route = app_src.split('def series_detail')[1].split('\ndef ')[0]
if 'mongo_connected' in s_route: ok('series','passes mongo_connected')
else: warn('series','series_detail missing mongo_connected - comment form will be hidden')
if 'series_id=series_id' in s_route: ok('series','passes series_id')
else: warn('series','missing series_id param - Seen It breaks')

# series.html comment form
if "content_type: 'series'" in series_html: ok('series comments','content_type present')
else: warn('series comments',"content_type:'series' missing from POST body")
if 'mongo_connected' in series_html: ok('series comments','mongo_connected guard present')
else: warn('series comments','no mongo_connected guard - form always hidden')
if 'voteSeenIt' in series_html: ok('series seen-it','widget present')
else: warn('series seen-it','Seen It widget missing')

# movie.html
if 'content_type' in movie_html: ok('movie comments','content_type in form')
else: warn('movie comments','content_type missing')
if 'toggleReplyBox' in movie_html: ok('movie replies','reply JS present')
else: warn('movie replies','toggleReplyBox missing')
if 'openAddToList' in movie_html: ok('movie lists','Add to List button present')
else: warn('movie lists','Add to List button missing')

# era
if "item['Movie Name']" in era_html and 'item.get_Year' not in era_html: ok('era','JS bug fixed')
else: warn('era','item.get_Year bug still present')
if 'series only' in era_html: ok('era','decade label shows series only')
else: warn('era','decade label missing series-only note')

# global search
if 'global-search' in index_html: ok('global search','bar present')
else: warn('global search','bar missing')
if 'top:52px' in index_html: warn('global search','still fixed top:52px - overlaps navbar')
else: ok('global search','not using fixed top:52px')

# navbar auth
if 'navbar-auth' in index_html: ok('navbar','auth links in navbar')
else: warn('navbar','auth links missing')
if '<div id="preloader"' in index_html: ok('preloader','valid div')
else: warn('preloader','broken - missing opening <div tag')

# TMDB
if 'eyJhbGci' in index_html: warn('security','TMDB bearer token exposed in index.html')
else: ok('security','TMDB token not in index.html')
if '/api/tmdb/popular' in index_html: ok('tmdb','using server-side proxy')
else: warn('tmdb','not using proxy route')

# templates exist
for tpl in ['feed.html','login.html','register.html','playlist.html','list.html']:
    path = os.path.join(BASE,'templates',tpl)
    if os.path.exists(path): ok('templates',f'{tpl} exists')
    else: warn('templates',f'{tpl} MISSING')

# playlist route order - recommendations must be before <playlist_id>
rec_pos = app_src.find("'/api/playlists/recommendations'")
pid_pos = app_src.find("'/api/playlists/<playlist_id>'")
if 0 < rec_pos < pid_pos: ok('playlist routes','recommendations before <playlist_id>')
else: warn('playlist routes','/api/playlists/recommendations AFTER /<playlist_id> - Flask treats "recommendations" as an ID, returns 404')

# playlist.html API keys
if '{{ api_key_1 }}' in pl_html and '{{ api_key_2 }}' in pl_html: ok('playlist','API keys in template')
else: warn('playlist','API keys missing from playlist.html')

# profile playlists section
if 'playlists-container' in profile_html and 'createPlaylist' in profile_html: ok('profile','playlist section present')
else: warn('profile','playlist section missing')

# artist api keys passed from route
a_route = app_src.split('def artist_detail')[1].split('\ndef ')[0]
if 'api_key_1' in a_route: ok('artist','API keys passed to template')
else: warn('artist','API keys not passed to artist.html')

# game recommendations - sample crash safety
rg_src = app_src.split('def recommend_games')[1].split('\ndef ')[0]
if '.sample(n=5)' in rg_src: warn('games','recommend_games: sample(n=5) crashes if <5 diverse games')
else: ok('games','recommend_games sample safe')

gg_src = app_src.split('def get_game_recommendations')[1].split('\ndef ')[0]
if 'diverse_games.empty' in gg_src: ok('games','get_game_recommendations sample safe')
else: warn('games','get_game_recommendations: sample(n=1) crashes if no diverse game')

# /search uses Year column which doesn't exist
search_src = app_src.split('def search')[1].split('\ndef ')[0]
if "results['Year']" in search_src and 'Year column does not exist' not in search_src:
    warn('search',"/search filters on Year column which does not exist in movies.csv")
else: ok('search','no Year column issue')

# series_detail passes mongo_connected
s_detail = app_src.split('def series_detail')[1].split('\ndef ')[0]
if 'mongo_connected' in s_detail: ok('series','series_detail passes mongo_connected')
else: warn('series','series_detail missing mongo_connected - comment form will be hidden')

print(f'\n=== SUMMARY: {len(issues)} issues ===')
for i in issues: print(f'  {i}')
