import json
import math
import atexit
import logging
import requests as http_requests
from push_utils import init_push, send_push
from markupsafe import escape as _esc, Markup
from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from flask_socketio import SocketIO, join_room, leave_room, emit
import pandas as pd
import os
from pymongo import MongoClient, DESCENDING
from datetime import datetime, timezone
import uuid
from dotenv import load_dotenv
from flask_bcrypt import Bcrypt

load_dotenv()

logging.basicConfig(level=logging.ERROR)
log = logging.getLogger(__name__)

app = Flask(__name__)
import secrets as _secrets
app.secret_key = os.getenv("SECRET_KEY") or _secrets.token_hex(32)
from datetime import timedelta
app.permanent_session_lifetime = timedelta(days=30)
bcrypt = Bcrypt(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

MONGO_URI = os.getenv("MONGO_URI", "")
DB_NAME = "mediapedia"

client = None
try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client[DB_NAME]
    comments_collection = db.comments
    comments_collection.create_index("id")
    comments_collection.create_index("created_at")
    users_collection = db.users
    users_collection.create_index("username", unique=True)
    lists_collection = db.lists
    lists_collection.create_index("username")
    follows_collection = db.follows
    follows_collection.create_index([("follower", 1), ("following", 1)], unique=True)
    playlists_collection = db.playlists
    playlists_collection.create_index("username")
    messages_collection = db.messages
    messages_collection.create_index([("participants", 1)])
    messages_collection.create_index([("created_at", -1)])
    posts_collection = db.posts
    posts_collection.create_index([("username", 1), ("created_at", -1)])
    print("MongoDB connected successfully")
except Exception as e:
    log.error("MongoDB connection failed: %s", e)
    db = None
    comments_collection = None
    users_collection = None
    lists_collection = None
    follows_collection = None
    playlists_collection = None
    messages_collection = None
    posts_collection = None

def _close_mongo():
    if client is not None:
        try:
            client.close()
        except Exception as exc:
            log.error("Error closing MongoDB client: %s", exc)

atexit.register(_close_mongo)

# ===== Splitwise =====
from splitwise import splitwise_bp, init_splitwise
if db is not None:
    init_splitwise(db)
app.register_blueprint(splitwise_bp)

# ===== TMDB Poster Cache =====
poster_cache_collection = None
try:
    if db is not None:
        poster_cache_collection = db.poster_cache
        poster_cache_collection.create_index('title')
except Exception as e:
    log.error('Failed to init poster_cache: %s', e)

@app.route('/api/poster')
def get_poster():
    title = request.args.get('title', '').strip()
    year  = request.args.get('year', '').strip()
    kind  = request.args.get('kind', 'movie')   # 'movie' or 'tv'
    
    # ALWAYS use title search - works for ALL movies regardless of ID
    if not title:
        return jsonify({'poster': '', 'backdrop': ''})
    
    cache_key = f"{kind}:{title}:{year}"
    if poster_cache_collection is not None:
        cached = poster_cache_collection.find_one({'title': cache_key}, {'_id': 0, 'poster': 1, 'backdrop': 1})
        if cached and cached.get('poster'):
            return jsonify({'poster': cached.get('poster',''), 'backdrop': cached.get('backdrop','')})
    
    token = os.getenv('TMDB_TOKEN', '')
    if not token:
        return jsonify({'poster': '', 'backdrop': ''})
    
    try:
        endpoint = 'search/movie' if kind == 'movie' else 'search/tv'
        params = {'query': title, 'language': 'en-US'}
        if year:
            params['year' if kind == 'movie' else 'first_air_date_year'] = year
        
        resp = http_requests.get(
            f'https://api.themoviedb.org/3/{endpoint}',
            headers={'Authorization': f'Bearer {token}'},
            params=params, timeout=5
        )
        resp.raise_for_status()
        results = resp.json().get('results', [])
        
        poster = ''
        backdrop = ''
        
        if results:
            # Try to find the best match by title
            best_match = None
            title_lower = title.lower()
            
            # First, look for exact match
            for r in results:
                r_title = r.get('title', r.get('name', '')).lower()
                if r_title == title_lower:
                    best_match = r
                    break
            
            # If no exact match, try partial match
            if not best_match:
                for r in results:
                    r_title = r.get('title', r.get('name', '')).lower()
                    if title_lower in r_title or r_title in title_lower:
                        best_match = r
                        break
            
            # If still no match, use the first result
            if not best_match and results:
                best_match = results[0]
            
            if best_match:
                p = best_match.get('poster_path', '')
                b = best_match.get('backdrop_path', '')
                poster = f'https://image.tmdb.org/t/p/w342{p}' if p else ''
                backdrop = f'https://image.tmdb.org/t/p/original{b}' if b else ''
        
        if poster_cache_collection is not None:
            poster_cache_collection.update_one(
                {'title': cache_key},
                {'$set': {'title': cache_key, 'poster': poster, 'backdrop': backdrop}},
                upsert=True
            )
        return jsonify({'poster': poster, 'backdrop': backdrop})
        
    except http_requests.exceptions.RequestException as e:
        log.error('TMDB poster request error: %s', e)
        return jsonify({'poster': '', 'backdrop': ''})
    except Exception as e:
        log.error('TMDB poster error: %s', e)
        return jsonify({'poster': '', 'backdrop': ''})

@app.route('/api/artist_photo')
def get_artist_photo():
    name = request.args.get('name', '').strip()
    if not name:
        return jsonify({'photo': ''})
    cache_key = f'artist:{name}'
    if poster_cache_collection is not None:
        cached = poster_cache_collection.find_one({'title': cache_key}, {'_id': 0, 'photo': 1})
        if cached:
            return jsonify({'photo': cached.get('photo', '')})
    photo = ''
    try:
        # MusicBrainz: free, no key, 1 req/sec
        mb_resp = http_requests.get(
            'https://musicbrainz.org/ws/2/artist/',
            params={'query': f'artist:{name}', 'fmt': 'json', 'limit': 1},
            headers={'User-Agent': 'MediaPedia/1.0 (mediapedia@example.com)'},
            timeout=5
        )
        mb_resp.raise_for_status()
        artists = mb_resp.json().get('artists', [])
        if artists:
            rels = artists[0].get('relations', []) or []
            wikidata_url = next((r['url']['resource'] for r in rels if 'wikidata.org' in r.get('url', {}).get('resource', '')), None)
            if not wikidata_url:
                # fallback: try wikidata via artist name directly
                wd_resp = http_requests.get(
                    'https://www.wikidata.org/w/api.php',
                    params={'action': 'wbsearchentities', 'search': name, 'language': 'en', 'format': 'json', 'limit': 1},
                    timeout=5
                )
                wd_resp.raise_for_status()
                items = wd_resp.json().get('search', [])
                if items:
                    wikidata_url = f"https://www.wikidata.org/wiki/{items[0]['id']}"
            if wikidata_url:
                qid = wikidata_url.rstrip('/').split('/')[-1]
                wd_entity = http_requests.get(
                    f'https://www.wikidata.org/wiki/Special:EntityData/{qid}.json',
                    timeout=5
                )
                wd_entity.raise_for_status()
                entity = wd_entity.json().get('entities', {}).get(qid, {})
                # P18 = image property
                claims = entity.get('claims', {})
                p18 = claims.get('P18', [])
                if p18:
                    img_name = p18[0]['mainsnak']['datavalue']['value']
                    img_name_encoded = img_name.replace(' ', '_')
                    import hashlib
                    md5 = hashlib.md5(img_name_encoded.encode()).hexdigest()
                    photo = f'https://upload.wikimedia.org/wikipedia/commons/thumb/{md5[0]}/{md5[:2]}/{img_name_encoded}/300px-{img_name_encoded}'
    except Exception as e:
        log.error('Artist photo error for %s: %s', name, e)
    if poster_cache_collection is not None:
        poster_cache_collection.update_one(
            {'title': cache_key},
            {'$set': {'title': cache_key, 'photo': photo}},
            upsert=True
        )
    return jsonify({'photo': photo})

@app.route('/api/person_photo')
def get_person_photo():
    name = request.args.get('name', '').strip()
    if not name:
        return jsonify({'photo': ''})
    if poster_cache_collection is not None:
        cached = poster_cache_collection.find_one({'title': f'person:{name}'}, {'_id': 0, 'photo': 1})
        if cached:
            return jsonify({'photo': cached.get('photo', '')})
    token = os.getenv('TMDB_TOKEN', '')
    if not token:
        return jsonify({'photo': ''})
    try:
        resp = http_requests.get(
            'https://api.themoviedb.org/3/search/person',
            headers={'Authorization': f'Bearer {token}'},
            params={'query': name, 'language': 'en-US'}, timeout=5
        )
        resp.raise_for_status()
        results = resp.json().get('results', [])
        photo = ''
        if results:
            p = results[0].get('profile_path', '')
            photo = f'https://image.tmdb.org/t/p/w185{p}' if p else ''
        if poster_cache_collection is not None:
            poster_cache_collection.update_one(
                {'title': f'person:{name}'},
                {'$set': {'title': f'person:{name}', 'photo': photo}},
                upsert=True
            )
        return jsonify({'photo': photo})
    except Exception as e:
        log.error('TMDB person photo error: %s', e)
        return jsonify({'photo': ''})

# ===== TMDB Image Proxy =====
@app.route('/api/tmdb/img')
def tmdb_img_proxy():
    path = request.args.get('path', '').strip()
    if not path or not path.startswith('/'):
        return '', 400
    try:
        resp = http_requests.get(
            f'https://image.tmdb.org/t/p/w500{path}',
            timeout=8, stream=True
        )
        if resp.status_code != 200:
            return '', resp.status_code
        from flask import Response
        return Response(
            resp.content,
            content_type=resp.headers.get('Content-Type', 'image/jpeg'),
            headers={'Cache-Control': 'public, max-age=86400'}
        )
    except Exception as e:
        log.error('TMDB img proxy error: %s', e)
        return '', 502

# ===== TMDB Proxy (keeps token server-side) =====
@app.route("/api/tmdb/popular")
def tmdb_popular():
    token = os.getenv("TMDB_TOKEN", "")
    if not token:
        return jsonify({"error": "TMDB token not configured"}), 500
    try:
        resp = http_requests.get(
            "https://api.themoviedb.org/3/movie/popular",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=5
        )
        resp.raise_for_status()
        return jsonify(resp.json())
    except http_requests.RequestException as e:
        log.error("TMDB proxy error: %s", e)
        return jsonify({"error": "Failed to fetch TMDB data"}), 500

# ===== Safe float helper (guards NaN/Inf injection) =====
def _safe_float(value, default):
    try:
        v = float(value)
        return v if math.isfinite(v) else default
    except (ValueError, TypeError):
        return default

# ===== Comment Routes =====

@app.route("/api/comments/<int:id>", methods=["GET"])
def get_comments(id):
    """Get all comments for a specific movie"""
    if comments_collection is None:
        return jsonify({"error": "Database not connected"}), 500
    
    try:
        # Get comments sorted by newest first
        comments = list(comments_collection.find(
            {"id": id},
            {"_id": 0}  # Exclude MongoDB _id from response
        ).sort("created_at", -1).limit(100))
        
        # Convert datetime objects to strings for JSON
        for comment in comments:
            if "created_at" in comment:
                comment["created_at"] = comment["created_at"].isoformat()
        
        return jsonify(comments)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/comments", methods=["POST"])
def add_comment():
    """Add a new comment"""
    if comments_collection is None:
        return jsonify({"error": "Database not connected"}), 500
    
    try:
        data = request.json
        
        # Validate required fields
        if not data.get("id"):
            return jsonify({"error": "Movie ID required"}), 400
        if not data.get("username") or not data.get("text"):
            return jsonify({"error": "Username and comment text required"}), 400
        
        # Create comment document
        comment = {
            "comment_id": str(uuid.uuid4()),
            "id": data["id"],
            "content_type": data.get("content_type", "movie"),  # 'movie' or 'series'
            "username": data["username"][:50],
            "text": data["text"][:1000],
            "rating": min(5, max(1, int(data.get("rating", 5)))),
            "created_at": datetime.now(timezone.utc),
            "likes": 0,
            "replies": []
        }
        
        # Insert into MongoDB
        result = comments_collection.insert_one(comment)
        
        # Prepare response
        comment["_id"] = str(result.inserted_id)
        comment["created_at"] = comment["created_at"].isoformat()
        
        return jsonify(comment), 201
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/comments/<comment_id>/like", methods=["POST"])
def like_comment(comment_id):
    """Like/unlike a comment"""
    if comments_collection is None:
        return jsonify({"error": "Database not connected"}), 500
    
    try:
        # Increment likes count
        result = comments_collection.update_one(
            {"comment_id": comment_id},
            {"$inc": {"likes": 1}}
        )
        
        if result.modified_count == 0:
            return jsonify({"error": "Comment not found"}), 404
        
        # Get updated comment
        comment = comments_collection.find_one(
            {"comment_id": comment_id},
            {"_id": 0, "likes": 1}
        )
        
        # Notify comment author
        liker = current_user()
        if liker and comment.get('username') and liker != comment['username']:
            send_push(comment['username'], {
                'title': '❤️ Someone liked your review',
                'body': f'{liker} liked your review',
                'url': f'/{comment.get("content_type", "movie")}/{comment.get("id", "")}',
                'tag': f'like-{comment_id}'
            })
        return jsonify({"likes": comment.get("likes", 0)})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/comments/<comment_id>", methods=["DELETE"])
def delete_comment(comment_id):
    """Delete a comment (requires username verification)"""
    if comments_collection is None:
        return jsonify({"error": "Database not connected"}), 500
    
    try:
        username = (request.args.get("username") or "").strip()
        if not username:
            return jsonify({"error": "Username required"}), 400
        result = comments_collection.delete_one({
            "comment_id": comment_id,
            "username": username
        })
        
        if result.deleted_count == 0:
            return jsonify({"error": "Comment not found or unauthorized"}), 404
        
        return jsonify({"success": True})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/comments/<int:id>/stats", methods=["GET"])
def get_comment_stats(id):
    """Get comment statistics for a movie"""
    if comments_collection is None:
        return jsonify({"error": "Database not connected"}), 500
    
    try:
        # Get comment count
        total_comments = comments_collection.count_documents({"id": id})
        
        # Get average rating
        pipeline = [
            {"$match": {"id": id}},
            {"$group": {
                "_id": None,
                "avg_rating": {"$avg": "$rating"},
                "total_ratings": {"$sum": 1}
            }}
        ]
        
        result = list(comments_collection.aggregate(pipeline))
        
        stats = {
            "total_comments": total_comments,
            "avg_rating": round(result[0]["avg_rating"], 1) if result else 0,
            "total_ratings": result[0]["total_ratings"] if result else 0
        }
        
        return jsonify(stats)
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Load CSV (adjust path if needed)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE_DIR, "movies.csv")
df = pd.read_csv(CSV_PATH)

# Add ID column if missing
if 'ID' not in df.columns:
    df.insert(0, 'ID', range(1, len(df) + 1))

df = df.fillna("")

@app.route("/")
def home():
    return render_template("index.html", username=current_user())

# ===== Authors Choice Collection =====
ac_collection = None
try:
    if db is not None:
        ac_collection = db.authors_choice
        ac_collection.create_index([('type', 1), ('category', 1)])
except Exception as e:
    log.error('Failed to init authors_choice: %s', e)

@app.route('/authors_choice')
def authors_choice():
    author_avatar = ''
    if users_collection is not None:
        doc = users_collection.find_one({'username': 'abhinav'}, {'_id': 0, 'avatar_url': 1})
        if doc:
            author_avatar = doc.get('avatar_url', '')
    return render_template('authors_choice.html', username=current_user(), author_avatar=author_avatar)

@app.route('/api/authors_choice', methods=['GET'])
def ac_get():
    if ac_collection is None:
        return jsonify([])
    items = list(ac_collection.find({}, {'_id': 0}).sort([('type', 1), ('category', 1), ('title', 1)]))
    return jsonify(items)

@app.route('/api/authors_choice', methods=['POST'])
def ac_add():
    if current_user() != 'abhinav':
        return jsonify({'error': 'Unauthorized'}), 403
    if ac_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    data = request.json or {}
    item_type = data.get('type', '').strip()   # 'movie' or 'series'
    category  = data.get('category', '').strip() # 'english','hindi','anime','korean','series'
    title     = data.get('title', '').strip()
    item_id   = int(data.get('id', 0))
    if not item_type or not title or not item_id:
        return jsonify({'error': 'type, title and id required'}), 400
    ac_collection.update_one(
        {'type': item_type, 'id': item_id},
        {'$set': {'type': item_type, 'category': category, 'title': title, 'id': item_id}},
        upsert=True
    )
    return jsonify({'success': True}), 201

@app.route('/api/authors_choice/<int:item_id>', methods=['DELETE'])
def ac_remove(item_id):
    if current_user() != 'abhinav':
        return jsonify({'error': 'Unauthorized'}), 403
    if ac_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    item_type = request.args.get('type', '')
    ac_collection.delete_one({'id': item_id, 'type': item_type})
    return jsonify({'success': True})

@app.route('/api/ac_search')
def ac_search():
    q = request.args.get('q', '').strip().lower()
    kind = request.args.get('type', 'movie')  # 'movie' or 'series'
    if not q or len(q) < 2:
        return jsonify([])
    if kind == 'series':
        hits = series_df[series_df['Title'].str.lower().str.contains(q, na=False)].head(8)
        return jsonify([{'id': int(r['ID']), 'title': r['Title']} for _, r in hits.iterrows()])
    else:
        hits = df[df['Movie Name'].str.lower().str.contains(q, na=False)].head(8)
        return jsonify([{'id': int(r['ID']), 'title': r['Movie Name']} for _, r in hits.iterrows()])

# ===== Custom Error Handler =====
@app.errorhandler(404)
def not_found_error(error):
    return render_template('404.html'), 404

@app.route("/movie/<int:movie_id>")
def movie_detail(movie_id):
    movie = df[df['ID'] == movie_id].to_dict(orient="records")
    if not movie:
        return render_template('404.html'), 404
    movie = movie[0]
    genre = movie.get('Genre', '').lower()

    same_genre_movies = df[df['Genre'].str.lower().str.contains(genre, na=False) & (df['ID'] != movie_id)]
    related_movies = same_genre_movies.sample(n=min(10, len(same_genre_movies))) \
                      .to_dict(orient='records')
    
    api_key_1 = os.getenv("YOUTUBE_API_KEY_1", "")
    api_key_2 = os.getenv("YOUTUBE_API_KEY_2", "")
    api_key_3 = os.getenv("YOUTUBE_API_KEY_3", "")
    api_key_4 = os.getenv("YOUTUBE_API_KEY_4", "")
    api_key_5 = os.getenv("YOUTUBE_API_KEY_5", "")

    # Check if MongoDB is connected
    mongo_connected = comments_collection is not None

    return render_template(
        "movie.html",
        movie=movie,
        related_movies=related_movies,
        api_key_1=api_key_1,
        api_key_2=api_key_2,
        api_key_3=api_key_3,
        api_key_4=api_key_4,
        api_key_5=api_key_5,
        mongo_connected=mongo_connected,
        movie_id=movie_id,
        username=current_user()
    )

@app.route("/search")
def search():
    query = request.args.get("query", "").lower()
    genre = request.args.get("genre", "").lower()
    actor = request.args.get("actor", "").lower()
    director = request.args.get("director", "").lower()

    min_rating = _safe_float(request.args.get("min_rating", 0) or 0, 0)

    max_rating = _safe_float(request.args.get("max_rating", 10) or 10, 10)

    results = df.copy()

    year = request.args.get("year", "").strip()

    if query:
        results = results[results['Movie Name'].str.lower().str.contains(query, na=False)]

    if genre:
        results = results[results['Genre'].str.lower().str.contains(genre, na=False)]

    if actor:
        results = results[results['Stars'].str.lower().str.contains(actor, na=False)]

    if director:
        results = results[results['Directors'].str.lower().str.contains(director, na=False)]

    # Year column does not exist in movies.csv - skip year filter

    # Convert Rating to numeric and filter
    results['Rating'] = pd.to_numeric(results['Rating'], errors='coerce').fillna(0)
    results = results[(results['Rating'] >= min_rating) & (results['Rating'] <= max_rating)]

    # Instead of IMDb link, give our own detail page link
    results['DetailLink'] = results['ID'].apply(lambda x: f"/movie/{x}")

    # Limit to 500 results
    results = results.head(500)

    return jsonify(results.to_dict(orient="records"))

# ===== Load Series CSV =====
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERIES_PATH = os.path.join(BASE_DIR, "series.csv")
series_df = pd.read_csv(SERIES_PATH)

# Add ID column if missing
if 'ID' not in series_df.columns:
    series_df.insert(0, 'ID', range(1, len(series_df) + 1))

series_df = series_df.fillna("")

# ===== Series Detail Page =====
@app.route("/series/<int:series_id>")
def series_detail(series_id):
    series = series_df[series_df['ID'] == series_id].to_dict(orient="records")
    if not series:
        return render_template('404.html'), 404
    series = series[0]

    genre = series.get('Genres', '').lower()

    same_genre_series = series_df[
        series_df['Genres'].str.lower().str.contains(genre, na=False) &
        (series_df['ID'] != series_id)
    ]
    related_series = same_genre_series.sample(n=min(10, len(same_genre_series))) \
                                      .to_dict(orient='records')

    # Pass YouTube API keys for trailer loading
    api_key_1 = os.getenv("YOUTUBE_API_KEY_1", "")
    api_key_2 = os.getenv("YOUTUBE_API_KEY_2", "")
    api_key_3 = os.getenv("YOUTUBE_API_KEY_3", "")
    api_key_4 = os.getenv("YOUTUBE_API_KEY_4", "")
    api_key_5 = os.getenv("YOUTUBE_API_KEY_5", "")

    mongo_connected = comments_collection is not None

    return render_template(
        "series.html",
        series=series,
        related_series=related_series,
        api_key_1=api_key_1,
        api_key_2=api_key_2,
        api_key_3=api_key_3,
        api_key_4=api_key_4,
        api_key_5=api_key_5,
        series_id=series_id,
        mongo_connected=mongo_connected,
        username=current_user()
    )

# ===== Series Search =====
@app.route("/search_series")
def search_series():
    query = request.args.get("query", "").strip().lower()
    genre = request.args.get("genre", "").strip().lower()
    actor = request.args.get("actor", "").strip().lower()
    year = request.args.get("year", "").strip()
    min_rating = _safe_float(request.args.get("min_rating", 0) or 0, 0)
    max_rating = _safe_float(request.args.get("max_rating", 10) or 10, 10)

    results = series_df.copy()

    if query:
        results = results[results['Title'].str.lower().str.contains(query, na=False)]
    if genre:
        results = results[results['Genres'].str.lower().str.replace(" ", "").str.contains(genre.replace(" ", ""), na=False)]
    if actor:
        results = results[results['Actors'].str.lower().str.contains(actor, na=False)]
    if year:
        results = results[results['Years'].astype(str).str.contains(year, na=False)]

    results['Rating'] = pd.to_numeric(results['Rating'], errors='coerce').fillna(0)
    results = results[(results['Rating'] >= min_rating) & (results['Rating'] <= max_rating)]

    results['DetailLink'] = results['ID'].apply(lambda x: f"/series/{x}")

    # Limit to 500 results
    results = results.head(500)

    return jsonify(results.to_dict(orient="records"))

# ===== Load Artist CSV =====
ARTIST_PATH = os.path.join(BASE_DIR, "artists.csv")
artists_df = pd.read_csv(ARTIST_PATH)

if 'ID' not in artists_df.columns:
    artists_df.insert(0, 'ID', range(1, len(artists_df) + 1))

artists_df = artists_df.fillna("")

# ===== Artist Search =====
@app.route("/search_artist")
def search_artist():
    name = request.args.get("name", "").strip().lower()
    genre = request.args.get("genre", "").strip().lower()
    country = request.args.get("country", "").strip().lower()

    results = artists_df.copy()

    if name:
        results = results[results['artist_name'].str.lower().str.contains(name, na=False)]
    if genre:
        results = results[results['artist_genre'].str.lower().str.contains(genre, na=False)]
    if country:
        results = results[results['country'].str.lower().str.contains(country, na=False)]

    # Limit to 500 results
    results = results.head(500)

    results['DetailLink'] = results['ID'].apply(lambda x: f"/artist/{x}")

    return jsonify(results.to_dict(orient="records"))


# ===== Artist Detail Page =====
@app.route("/artist/<int:artist_id>")
def artist_detail(artist_id):
    artist = artists_df[artists_df['ID'] == artist_id].to_dict(orient="records")
    if not artist:
        return render_template('404.html'), 404
    artist = artist[0]

    genre = artist.get('artist_genre', '').lower()

    # Find related artists by genre
    same_genre = artists_df[
        artists_df['artist_genre'].str.lower().str.contains(genre, na=False) &
        (artists_df['ID'] != artist_id)
    ]
    related_artists = same_genre.sample(n=min(10, len(same_genre))).to_dict(orient='records')

    # YouTube API keys for video embedding
    api_key_1 = os.getenv("YOUTUBE_API_KEY_1", "")
    api_key_2 = os.getenv("YOUTUBE_API_KEY_2", "")
    api_key_3 = os.getenv("YOUTUBE_API_KEY_3", "")
    api_key_4 = os.getenv("YOUTUBE_API_KEY_4", "")
    api_key_5 = os.getenv("YOUTUBE_API_KEY_5", "")

    return render_template(
        "artist.html",
        artist=artist,
        related_artists=related_artists,
        api_key_1=api_key_1,
        api_key_2=api_key_2,
        api_key_3=api_key_3,
        api_key_4=api_key_4,
        api_key_5=api_key_5,
        username=current_user()
    )

# ===== Random Artists =====
@app.route("/random_artists")
def random_artists():
    random_list = artists_df.sample(n=min(10, len(artists_df))).to_dict(orient="records")
    return jsonify(random_list)


# ===== Load Games CSV =====
GAMES_PATH = os.path.join(BASE_DIR, "games.csv")
games_df = pd.read_csv(GAMES_PATH)

# Add ID column if missing
if 'ID' not in games_df.columns:
    games_df.insert(0, 'ID', range(1, len(games_df) + 1))

games_df = games_df.fillna("")

# ===== Game Detail Page =====
@app.route("/game/<int:game_id>")
def game_detail(game_id):
    game = games_df[games_df['ID'] == game_id].to_dict(orient="records")
    if not game:
        return render_template('404.html'), 404
    game = game[0]

    # Get recommendations with 9:1 ratio
    recommendations = get_game_recommendations(game_id)

    # YouTube API keys for video embedding
    api_key_1 = os.getenv("YOUTUBE_API_KEY_1", "")
    api_key_2 = os.getenv("YOUTUBE_API_KEY_2", "")
    api_key_3 = os.getenv("YOUTUBE_API_KEY_3", "")
    api_key_4 = os.getenv("YOUTUBE_API_KEY_4", "")
    api_key_5 = os.getenv("YOUTUBE_API_KEY_5", "")

    return render_template(
        "game.html",
        game=game,
        recommendations=recommendations,
        api_key_1=api_key_1,
        api_key_2=api_key_2,
        api_key_3=api_key_3,
        api_key_4=api_key_4,
        api_key_5=api_key_5,
        username=current_user()
    )


def get_game_recommendations(game_id):
    current_game = games_df[games_df['ID'] == game_id].iloc[0]
    genre = current_game['Genre']
    platform = current_game['Platform']
    
    # Get top 9 similar games (same genre/platform, high sales)
    similar_games = games_df[
        (games_df['Genre'] == genre) & 
        (games_df['Platform'] == platform) & 
        (games_df['ID'] != game_id)
    ].sort_values('Global_Sales', ascending=False).head(9)
    
    # Get 1 diverse recommendation (different genre/platform, lower rank)
    diverse_games = games_df[
        ((games_df['Genre'] != genre) | (games_df['Platform'] != platform)) &
        (games_df['Rank'] > 100)
    ]
    if not diverse_games.empty:
        recommendations = pd.concat([similar_games, diverse_games.sample(n=1)])
    else:
        recommendations = similar_games
    return recommendations.to_dict(orient='records')

# ===== Game Search =====
@app.route("/search_games")
def search_games():
    name = request.args.get("name", "").strip().lower()
    platform = request.args.get("platform", "").strip()
    year = request.args.get("year", "").strip()
    genre = request.args.get("genre", "").strip()
    publisher = request.args.get("publisher", "").strip()
    min_sales = _safe_float(request.args.get("min_sales", 0) or 0, 0)
    max_sales = _safe_float(request.args.get("max_sales", 100) or 100, 100)

    results = games_df.copy()

    if name:
        results = results[results['Name'].str.lower().str.contains(name, na=False)]
    if platform:
        results = results[results['Platform'].str.contains(platform, na=False)]
    if year:
        results = results[results['Year'].astype(str).str.contains(year, na=False)]
    if genre:
        results = results[results['Genre'].str.contains(genre, na=False)]
    if publisher:
        results = results[results['Publisher'].str.contains(publisher, na=False)]

    # Filter by sales
    results = results[
        (results['Global_Sales'] >= min_sales) & 
        (results['Global_Sales'] <= max_sales)
    ]

    # Add detail link
    results['DetailLink'] = results['ID'].apply(lambda x: f"/game/{x}")

    # Limit to 500 results
    results = results.head(500)

    return jsonify(results.to_dict(orient="records"))

# ===== Game Recommendations =====
@app.route("/recommend_games")
def recommend_games():
    # Get base recommendations (top sellers + some diverse picks)
    top_games = games_df.sort_values('Global_Sales', ascending=False).head(45)
    diverse_pool = games_df[
        (games_df['Rank'] > 100) &
        ~games_df['Genre'].isin(top_games['Genre'].unique())
    ]
    diverse_games = diverse_pool.sample(n=min(5, len(diverse_pool))) if not diverse_pool.empty else pd.DataFrame()
    
    recommendations = pd.concat([top_games, diverse_games]).sample(frac=1)  # Shuffle
    recommendations['DetailLink'] = recommendations['ID'].apply(lambda x: f"/game/{x}")
    
    # Limit to 500 results
    recommendations = recommendations.head(500)
    
    return jsonify(recommendations.to_dict(orient="records"))

# ===== Director Page =====
@app.route("/director/<path:director_name>")
def director_page(director_name):
    director_name = Markup(_esc(director_name))
    name_lower = director_name.lower()
    movies = df[df['Directors'].str.lower().str.contains(name_lower, na=False)].to_dict(orient='records')
    if not movies:
        return render_template('404.html'), 404
    movies.sort(key=lambda x: float(x.get('Rating') or 0), reverse=True)
    genres = {}
    for m in movies:
        for g in str(m.get('Genre', '')).split(','):
            g = g.strip()
            if g:
                genres[g] = genres.get(g, 0) + 1
    signature_genres = sorted(genres, key=genres.get, reverse=True)[:3]
    avg_rating = round(sum(float(m.get('Rating') or 0) for m in movies) / len(movies), 1) if movies else 0
    return render_template('director.html', director_name=director_name, movies=movies,
                           signature_genres=signature_genres, avg_rating=avg_rating, total=len(movies),
                           username=current_user())

# ===== Actor Page =====
@app.route("/actor/<path:actor_name>")
def actor_page(actor_name):
    actor_name = Markup(_esc(actor_name))
    name_lower = actor_name.lower()
    movies = df[df['Stars'].str.lower().str.contains(name_lower, na=False)].to_dict(orient='records')
    series = series_df[series_df['Actors'].str.lower().str.contains(name_lower, na=False)].to_dict(orient='records')
    if not movies and not series:
        return render_template('404.html'), 404
    movies.sort(key=lambda x: float(x.get('Rating') or 0), reverse=True)
    series.sort(key=lambda x: float(x.get('Rating') or 0), reverse=True)
    return render_template('actor.html', actor_name=actor_name, movies=movies[:20], series=series[:10],
                           username=current_user())

# ===== Franchise Tracker =====
@app.route("/franchise/<path:franchise_name>")
def franchise_page(franchise_name):
    franchise_name = Markup(_esc(franchise_name))
    name_lower = franchise_name.lower()
    franchise_movies = df[df['Movie Name'].str.lower().str.contains(name_lower, na=False)]\
        .sort_values('Rating', ascending=False).to_dict(orient='records')
    if not franchise_movies:
        return render_template('404.html'), 404
    return render_template('franchise.html', franchise_name=franchise_name, movies=franchise_movies,
                           username=current_user())

@app.route("/api/franchises")
def get_franchises():
    sequel_keywords = ['2', '3', '4', '5', 'Part II', 'Part III', 'Returns', 'Rises',
                       'Reloaded', 'Revolution', 'Resurrection', 'Legacy', 'Begins',
                       'Strikes Back', 'Revenge', 'Rise of', 'Dawn of', 'Age of']
    franchise_map = {}
    for _, row in df.iterrows():
        title = str(row.get('Movie Name', ''))
        for kw in sequel_keywords:
            if kw.lower() in title.lower():
                base = title.lower().replace(kw.lower(), '').strip(' :-').strip().title()
                if len(base) > 2:
                    franchise_map.setdefault(base, []).append({
                        'ID': row['ID'], 'title': title,
                        'rating': row.get('Rating', ''), 'year': row.get('Year', '')
                    })
    result = [{'name': k, 'count': len(v), 'movies': v} for k, v in franchise_map.items()]
    result.sort(key=lambda x: x['count'], reverse=True)
    return jsonify(result[:30])

# ===== Era Explorer =====
@app.route("/era")
def era_explorer():
    return render_template('era.html')

@app.route("/api/era")
def era_movies():
    decade = request.args.get('decade', '')
    mood = request.args.get('mood', '').lower()
    content = request.args.get('content', 'movies')
    mood_genre_map = {
        'happy': ['Comedy', 'Animation', 'Family', 'Musical'],
        'thrilling': ['Action', 'Thriller', 'Crime', 'Mystery'],
        'emotional': ['Drama', 'Romance', 'Biography'],
        'scary': ['Horror', 'Mystery', 'Thriller'],
        'inspiring': ['Biography', 'Sport', 'History', 'Drama'],
        'adventurous': ['Adventure', 'Action', 'Sci-Fi', 'Fantasy'],
    }
    target_genres = mood_genre_map.get(mood, [])
    if content == 'series':
        results = series_df.copy()
        if decade:
            start_year = int(decade)
            mask = series_df['Years'].astype(str).str[:4].str.match(r'\d{4}')
            results = results[mask]
            results = results[pd.to_numeric(results['Years'].astype(str).str[:4], errors='coerce').between(start_year, start_year + 9)]
        if target_genres:
            results = results[results['Genres'].str.lower().apply(lambda g: any(t.lower() in g for t in target_genres))]
        results['Rating'] = pd.to_numeric(results['Rating'], errors='coerce').fillna(0)
        results = results.sort_values('Rating', ascending=False).head(50)
        results['DetailLink'] = results['ID'].apply(lambda x: f"/series/{x}")
        return jsonify(results.to_dict(orient='records'))
    else:
        results = df.copy()
        # movies.csv has no Year column â€” decade filter skipped for movies
        if target_genres:
            results = results[results['Genre'].str.lower().apply(lambda g: any(t.lower() in g for t in target_genres))]
        results['Rating'] = pd.to_numeric(results['Rating'], errors='coerce').fillna(0)
        results = results.sort_values('Rating', ascending=False).head(50)
        results['DetailLink'] = results['ID'].apply(lambda x: f"/movie/{x}")
        return jsonify(results.to_dict(orient='records'))

# ===== Complete the Vibe =====
@app.route("/api/complete_vibe")
def complete_vibe():
    movie_id = request.args.get('movie_id', type=int)
    if not movie_id:
        return jsonify({'error': 'movie_id required'}), 400
    movie = df[df['ID'] == movie_id]
    if movie.empty:
        return jsonify({'error': 'Movie not found'}), 404
    movie = movie.iloc[0]
    genres = [g.strip().lower() for g in str(movie.get('Genre', '')).split(',')]
    series_match = series_df[series_df['Genres'].str.lower().apply(
        lambda g: any(genre in g for genre in genres)
    )].sort_values('Rating', ascending=False).head(1)
    genre_music_map = {
        'action': 'Hip-Hop', 'drama': 'R&B', 'comedy': 'Pop', 'thriller': 'Electronic',
        'romance': 'Pop', 'sci-fi': 'Electronic', 'horror': 'Rock', 'adventure': 'Rock',
        'crime': 'Hip-Hop', 'biography': 'R&B', 'history': 'Classical', 'war': 'Rock'
    }
    music_genre = next((genre_music_map[g] for g in genres if g in genre_music_map), 'Pop')
    artist_pool = artists_df[artists_df['artist_genre'].str.lower().str.contains(music_genre.lower(), na=False)]
    artist_match = artist_pool.sample(n=min(1, len(artist_pool))) if not artist_pool.empty else artist_pool
    game_genre_map = {
        'action': 'Action', 'adventure': 'Adventure', 'comedy': 'Misc',
        'thriller': 'Action', 'sci-fi': 'Shooter', 'horror': 'Action',
        'crime': 'Action', 'drama': 'Role-Playing', 'romance': 'Misc'
    }
    game_genre = next((game_genre_map[g] for g in genres if g in game_genre_map), 'Action')
    game_match = games_df[games_df['Genre'].str.lower().str.contains(game_genre.lower(), na=False)]\
        .sort_values('Global_Sales', ascending=False).head(1)
    return jsonify({
        'movie': {'id': int(movie['ID']), 'title': movie['Movie Name'], 'genre': movie['Genre']},
        'series': series_match[['ID', 'Title', 'Genres', 'Rating']].to_dict(orient='records'),
        'artist': artist_match[['ID', 'artist_name', 'artist_genre']].to_dict(orient='records') if not artist_match.empty else [],
        'game': game_match[['ID', 'Name', 'Genre', 'Global_Sales']].to_dict(orient='records')
    })

# ===== Seen It Votes =====
seen_collection = None
try:
    if db is not None:
        seen_collection = db.seen_votes
        seen_collection.create_index("content_id")
except Exception as e:
    log.error("Failed to init seen_votes collection: %s", e)
    seen_collection = None

@app.route("/api/seen/<int:content_id>", methods=["GET"])
def get_seen(content_id):
    if seen_collection is None:
        return jsonify({'yes': 0, 'no': 0})
    doc = seen_collection.find_one({'content_id': content_id}, {'_id': 0})
    if not doc:
        return jsonify({'yes': 0, 'no': 0})
    return jsonify({'yes': doc.get('yes', 0), 'no': doc.get('no', 0)})

@app.route("/api/seen/<int:content_id>/vote", methods=["POST"])
def vote_seen(content_id):
    if seen_collection is None:
        return jsonify({'error': 'DB not connected'}), 500
    vote = request.json.get('vote')
    if vote not in ['yes', 'no']:
        return jsonify({'error': 'invalid vote'}), 400
    seen_collection.update_one({'content_id': content_id}, {'$inc': {vote: 1}}, upsert=True)
    doc = seen_collection.find_one({'content_id': content_id}, {'_id': 0})
    return jsonify({'yes': doc.get('yes', 0), 'no': doc.get('no', 0)})

# ===== User Profile =====
@app.route("/u/<username>")
def user_profile(username):
    if comments_collection is None:
        return render_template('profile.html', username=username, comments=[],
                               total_likes=0, avg_rating=0, followers=0, following=0,
                               is_following=False, viewer=current_user(), user_lists=[])
    
    # Check if follows_collection exists by comparing to None, not using bool()
    follows_exists = follows_collection is not None
    lists_exists = lists_collection is not None
    
    user_comments = list(comments_collection.find(
        {'username': username}, {'_id': 0}
    ).sort('created_at', -1).limit(50))
    
    for c in user_comments:
        if 'created_at' in c:
            c['created_at'] = _to_ist_str(c['created_at'])
        ctype = c.get('content_type', 'movie')
        cid = c.get('id')
        if ctype == 'movie':
            row = df[df['ID'] == cid]
            c['content_title'] = row.iloc[0]['Movie Name'] if not row.empty else ''
            c['content_url'] = f"/movie/{cid}"
        else:
            row = series_df[series_df['ID'] == cid]
            c['content_title'] = row.iloc[0]['Title'] if not row.empty else ''
            c['content_url'] = f"/series/{cid}"
    
    total_likes = sum(c.get('likes', 0) for c in user_comments)
    avg_rating = round(sum(c.get('rating', 0) for c in user_comments) / len(user_comments), 1) if user_comments else 0
    
    # Fixed: Compare with None instead of using truth value testing
    followers = follows_collection.count_documents({'following': username}) if follows_exists else 0
    following = follows_collection.count_documents({'follower': username}) if follows_exists else 0
    
    viewer = current_user()
    is_following = False
    if viewer and follows_exists:
        is_following = follows_collection.find_one({'follower': viewer, 'following': username}) is not None
    
    user_lists = list(lists_collection.find({'username': username}, {'_id': 0})) if lists_exists else []

    bio = ''
    avatar_url = ''
    birthdate = ''
    if users_collection is not None:
        user_doc = users_collection.find_one({'username': username}, {'_id': 0, 'bio': 1, 'avatar_url': 1, 'birthdate': 1})
        if user_doc:
            bio = user_doc.get('bio', '')
            avatar_url = user_doc.get('avatar_url', '')
            birthdate = user_doc.get('birthdate', '')

    return render_template('profile.html', username=username, comments=user_comments,
                           total_likes=total_likes, avg_rating=avg_rating,
                           followers=followers, following=following,
                           is_following=is_following, viewer=viewer,
                           user_lists=user_lists, bio=bio, avatar_url=avatar_url,
                           birthdate=birthdate)

# ===== Hot Takes Feed =====
@app.route("/api/hot_takes")
def hot_takes():
    if comments_collection is None:
        return jsonify([])
    hot = list(comments_collection.find(
        {'rating': {'$in': [1, 5]}}, {'_id': 0}
    ).sort('likes', -1).limit(20))
    for c in hot:
        if 'created_at' in c:
            c['created_at'] = c['created_at'].isoformat()
    return jsonify(hot)

# ===== Oscars Room =====
OSCARS_WINNERS = [
    {'year': 2024, 'title': 'Oppenheimer', 'director': 'Christopher Nolan'},
    {'year': 2023, 'title': 'Everything Everywhere All at Once', 'director': 'Daniel Kwan, Daniel Scheinert'},
    {'year': 2022, 'title': 'CODA', 'director': 'Sian Heder'},
    {'year': 2021, 'title': 'Nomadland', 'director': 'ChloÃ© Zhao'},
    {'year': 2020, 'title': 'Parasite', 'director': 'Bong Joon-ho'},
    {'year': 2019, 'title': 'Green Book', 'director': 'Peter Farrelly'},
    {'year': 2018, 'title': 'The Shape of Water', 'director': 'Guillermo del Toro'},
    {'year': 2017, 'title': 'Moonlight', 'director': 'Barry Jenkins'},
    {'year': 2016, 'title': 'Spotlight', 'director': 'Tom McCarthy'},
    {'year': 2015, 'title': 'Birdman', 'director': 'Alejandro G. Inarritu'},
    {'year': 2014, 'title': '12 Years a Slave', 'director': 'Steve McQueen'},
    {'year': 2013, 'title': 'Argo', 'director': 'Ben Affleck'},
    {'year': 2012, 'title': 'The Artist', 'director': 'Michel Hazanavicius'},
    {'year': 2011, 'title': "The King's Speech", 'director': 'Tom Hooper'},
    {'year': 2010, 'title': 'The Hurt Locker', 'director': 'Kathryn Bigelow'},
    {'year': 2009, 'title': 'Slumdog Millionaire', 'director': 'Danny Boyle'},
    {'year': 2008, 'title': 'No Country for Old Men', 'director': 'Ethan Coen, Joel Coen'},
    {'year': 2007, 'title': 'The Departed', 'director': 'Martin Scorsese'},
    {'year': 2006, 'title': 'Crash', 'director': 'Paul Haggis'},
    {'year': 2005, 'title': 'Million Dollar Baby', 'director': 'Clint Eastwood'},
    {'year': 2004, 'title': 'The Lord of the Rings: The Return of the King', 'director': 'Peter Jackson'},
    {'year': 2003, 'title': 'Chicago', 'director': 'Rob Marshall'},
    {'year': 2002, 'title': 'A Beautiful Mind', 'director': 'Ron Howard'},
    {'year': 2001, 'title': 'Gladiator', 'director': 'Ridley Scott'},
    {'year': 2000, 'title': 'American Beauty', 'director': 'Sam Mendes'},
    {'year': 1999, 'title': 'Shakespeare in Love', 'director': 'John Madden'},
    {'year': 1998, 'title': 'Titanic', 'director': 'James Cameron'},
    {'year': 1997, 'title': 'The English Patient', 'director': 'Anthony Minghella'},
    {'year': 1996, 'title': 'Braveheart', 'director': 'Mel Gibson'},
    {'year': 1995, 'title': 'Forrest Gump', 'director': 'Robert Zemeckis'},
    {'year': 1994, 'title': "Schindler's List", 'director': 'Steven Spielberg'},
    {'year': 1993, 'title': 'Unforgiven', 'director': 'Clint Eastwood'},
    {'year': 1992, 'title': 'The Silence of the Lambs', 'director': 'Jonathan Demme'},
    {'year': 1991, 'title': 'Dances with Wolves', 'director': 'Kevin Costner'},
    {'year': 1990, 'title': 'Driving Miss Daisy', 'director': 'Bruce Beresford'},
]

@app.route("/oscars")
def oscars_room():
    enriched = []
    for w in OSCARS_WINNERS:
        match = df[df['Movie Name'].str.lower().str.contains(w['title'].lower(), na=False)]
        movie_id = int(match.iloc[0]['ID']) if not match.empty else None
        enriched.append({**w, 'movie_id': movie_id})
    return render_template('oscars.html', winners=enriched, username=current_user())

# ===== Random Movies (dedicated endpoint) =====
@app.route("/random_movies")
def random_movies():
    pool = df[pd.to_numeric(df['Rating'], errors='coerce') >= 7]
    sample = pool.sample(n=min(10, len(pool))).to_dict(orient='records')
    for m in sample:
        m['DetailLink'] = f"/movie/{m['ID']}"
    return jsonify(sample)

# ===== Global Search =====
@app.route("/api/global_search")
def global_search():
    query = request.args.get("q", "").strip().lower()
    if not query or len(query) < 2:
        return jsonify([])
    results = [
        {'type': 'movie', 'id': int(m['ID']), 'title': m['Movie Name'],
         'sub': m.get('Genre', ''), 'url': f"/movie/{m['ID']}"}
        for _, m in df[df['Movie Name'].str.lower().str.contains(query, na=False)].head(5).iterrows()
    ] + [
        {'type': 'series', 'id': int(s['ID']), 'title': s['Title'],
         'sub': s.get('Genres', ''), 'url': f"/series/{s['ID']}"}
        for _, s in series_df[series_df['Title'].str.lower().str.contains(query, na=False)].head(5).iterrows()
    ] + [
        {'type': 'artist', 'id': int(a['ID']), 'title': a['artist_name'],
         'sub': a.get('artist_genre', ''), 'url': f"/artist/{a['ID']}"}
        for _, a in artists_df[artists_df['artist_name'].str.lower().str.contains(query, na=False)].head(3).iterrows()
    ]
    dir_hits = df[df['Directors'].str.lower().str.contains(query, na=False)]
    if not dir_hits.empty:
        dirs = set()
        for d_str in dir_hits['Directors'].dropna():
            for d in str(d_str).strip("[]").replace("'", "").split(","):
                d = d.strip()
                if d and query in d.lower():
                    dirs.add(d)
        results += [{'type': 'director', 'id': 0, 'title': d,
                     'sub': 'Director', 'url': f"/director/{d}"} for d in list(dirs)[:2]]
    if users_collection is not None:
        user_hits = list(users_collection.find(
            {'username': {'$regex': query, '$options': 'i'}},
            {'_id': 0, 'username': 1, 'bio': 1}
        ).limit(3))
        results += [{'type': 'user', 'id': 0, 'title': u['username'],
                     'sub': u.get('bio', '') or 'MediaPedia user', 'url': f"/u/{u['username']}"}
                    for u in user_hits]
    return jsonify(results[:15])

# ===== IST timezone helper =====
from datetime import timezone, timedelta
_IST = timedelta(hours=5, minutes=30)

def _to_ist_str(ts):
    """Convert a UTC ISO string or datetime to IST ISO string (YYYY-MM-DDTHH:MM:SS)."""
    if not ts:
        return ''
    try:
        if isinstance(ts, datetime):
            dt = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(ts).replace('Z', ''))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return (dt + _IST).strftime('%Y-%m-%dT%H:%M:%S')
    except Exception:
        return str(ts)

# ===== Auth helper =====
def current_user():
    return session.get('username')

# ===== Register =====
@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user():
        return redirect('/')
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        if not username or not password:
            return render_template('register.html', error='All fields required')
        if len(username) < 3 or len(username) > 30:
            return render_template('register.html', error='Username must be 3â€“30 characters')
        if users_collection is None:
            return render_template('register.html', error='Database unavailable')
        if users_collection.find_one({'username': username}):
            return render_template('register.html', error='Username already taken')
        hashed = bcrypt.generate_password_hash(password).decode('utf-8')
        users_collection.insert_one({
            'username': username,
            'password': hashed,
            'created_at': datetime.now(timezone.utc),
            'bio': ''
        })
        session['username'] = username
        session.permanent = True
        return redirect('/')
    return render_template('register.html', error=None)

# ===== Login =====
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user():
        return redirect('/')
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        if users_collection is None:
            return render_template('login.html', error='Database unavailable')
        user = users_collection.find_one({'username': username})
        if not user or not bcrypt.check_password_hash(user['password'], password):
            return render_template('login.html', error='Invalid username or password')
        session['username'] = username
        session.permanent = True
        return redirect('/')
    return render_template('login.html', error=None)

# ===== Logout =====
@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect('/')

# ===== Activity Feed =====
@app.route('/feed')
def feed():
    username = current_user()
    if comments_collection is None:
        return render_template('feed.html', comments=[], username=username)
    # If logged in, show followed users' activity first, then global
    if username and follows_collection is not None:
        following = [f['following'] for f in follows_collection.find({'follower': username})]
        if following:
            priority = list(comments_collection.find(
                {'username': {'$in': following}}, {'_id': 0}
            ).sort('created_at', DESCENDING).limit(30))
            others = list(comments_collection.find(
                {'username': {'$nin': following + [username]}}, {'_id': 0}
            ).sort('created_at', DESCENDING).limit(20))
            comments = priority + others
        else:
            comments = list(comments_collection.find({}, {'_id': 0}).sort('created_at', DESCENDING).limit(50))
    else:
        comments = list(comments_collection.find({}, {'_id': 0}).sort('created_at', DESCENDING).limit(50))
    for c in comments:
        if 'created_at' in c:
            c['created_at'] = _to_ist_str(c['created_at'])
        # Attach movie/series title
        ctype = c.get('content_type', 'movie')
        cid = c.get('id')
        if ctype == 'movie':
            row = df[df['ID'] == cid]
            c['content_title'] = row.iloc[0]['Movie Name'] if not row.empty else 'Unknown'
            c['content_url'] = f"/movie/{cid}"
        else:
            row = series_df[series_df['ID'] == cid]
            c['content_title'] = row.iloc[0]['Title'] if not row.empty else 'Unknown'
            c['content_url'] = f"/series/{cid}"
    return render_template('feed.html', comments=comments, username=username)

# ===== Follow / Unfollow =====
@app.route('/api/follow/<target>', methods=['POST'])
def follow_user(target):
    username = current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    if username == target:
        return jsonify({'error': 'Cannot follow yourself'}), 400
    if follows_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    existing = follows_collection.find_one({'follower': username, 'following': target})
    if existing:
        follows_collection.delete_one({'follower': username, 'following': target})
        return jsonify({'status': 'unfollowed'})
    follows_collection.insert_one({'follower': username, 'following': target, 'created_at': datetime.now(timezone.utc)})
    send_push(target, {
        'title': f'👤 New Follower',
        'body': f'{username} started following you',
        'url': f'/u/{username}',
        'tag': f'follow-{username}'
    })
    return jsonify({'status': 'followed'})

# ===== User Lists =====
@app.route('/api/lists', methods=['GET'])
def get_my_lists():
    username = current_user()
    if not username or lists_collection is None:
        return jsonify([])
    lists = list(lists_collection.find({'username': username}, {'_id': 0}))
    return jsonify(lists)

@app.route('/api/lists', methods=['POST'])
def create_list():
    username = current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    if lists_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    data = request.json
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'List name required'}), 400
    list_id = str(uuid.uuid4())[:8]
    lists_collection.insert_one({
        'list_id': list_id,
        'username': username,
        'name': name,
        'items': [],
        'created_at': datetime.now(timezone.utc)
    })
    return jsonify({'list_id': list_id, 'name': name}), 201

@app.route('/api/lists/<list_id>/add', methods=['POST'])
def add_to_list(list_id):
    username = current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    if lists_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    data = request.json
    item = {
        'content_type': data.get('content_type', 'movie'),
        'content_id': data.get('content_id'),
        'title': data.get('title', ''),
        'added_at': datetime.now(timezone.utc).isoformat()
    }
    result = lists_collection.update_one(
        {'list_id': list_id, 'username': username},
        {'$addToSet': {'items': item}}
    )
    if result.matched_count == 0:
        return jsonify({'error': 'List not found'}), 404
    return jsonify({'success': True})

@app.route('/api/lists/<list_id>/remove', methods=['POST'])
def remove_from_list(list_id):
    username = current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    if lists_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    data = request.json
    lists_collection.update_one(
        {'list_id': list_id, 'username': username},
        {'$pull': {'items': {'content_id': data.get('content_id'), 'content_type': data.get('content_type')}}}
    )
    return jsonify({'success': True})

@app.route('/api/lists/<list_id>', methods=['DELETE'])
def delete_list(list_id):
    username = current_user()
    if not username or lists_collection is None:
        return jsonify({'error': 'Unauthorized'}), 401
    lists_collection.delete_one({'list_id': list_id, 'username': username})
    return jsonify({'success': True})

@app.route('/api/lists/<list_id>/rename', methods=['POST'])
def rename_list(list_id):
    username = current_user()
    if not username or lists_collection is None:
        return jsonify({'error': 'Unauthorized'}), 401
    name = (request.json or {}).get('name', '').strip()
    if not name:
        return jsonify({'error': 'Name required'}), 400
    lists_collection.update_one(
        {'list_id': list_id, 'username': username},
        {'$set': {'name': name}}
    )
    return jsonify({'success': True})

@app.route('/api/lists/<list_id>/reorder', methods=['POST'])
def reorder_list(list_id):
    username = current_user()
    if not username or lists_collection is None:
        return jsonify({'error': 'Unauthorized'}), 401
    items = (request.json or {}).get('items', [])
    lists_collection.update_one(
        {'list_id': list_id, 'username': username},
        {'$set': {'items': items}}
    )
    return jsonify({'success': True})

@app.route('/list/<list_id>')
def view_list(list_id):
    if lists_collection is None:
        return render_template('404.html'), 404
    lst = lists_collection.find_one({'list_id': list_id}, {'_id': 0})
    if not lst:
        return render_template('404.html'), 404
    lst['created_at'] = lst['created_at'].isoformat() if isinstance(lst.get('created_at'), datetime) else ''
    return render_template('list.html', lst=lst, username=current_user())

# ===== Threaded Replies =====
@app.route('/api/comments/<comment_id>/reply', methods=['POST'])
def reply_to_comment(comment_id):
    if comments_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    username = current_user() or request.json.get('username', 'Anonymous')
    text = request.json.get('text', '').strip()
    if not text:
        return jsonify({'error': 'Reply text required'}), 400
    reply = {
        'reply_id': str(uuid.uuid4()),
        'username': username[:50],
        'text': text[:500],
        'created_at': datetime.now(timezone.utc).isoformat(),
        'likes': 0
    }
    result = comments_collection.update_one(
        {'comment_id': comment_id},
        {'$push': {'replies': reply}}
    )
    if result.matched_count == 0:
        return jsonify({'error': 'Comment not found'}), 404
    # Notify original comment author
    original = comments_collection.find_one({'comment_id': comment_id}, {'_id': 0, 'username': 1, 'content_type': 1, 'id': 1})
    if original and original.get('username') and original['username'] != username:
        send_push(original['username'], {
            'title': '💬 New reply to your review',
            'body': f'{username}: {text[:80]}',
            'url': f'/{original.get("content_type", "movie")}/{original.get("id", "")}',
            'tag': f'reply-{comment_id}'
        })
    return jsonify(reply), 201

# ===== Followers / Following Lists =====
@app.route('/api/followers/<target_username>')
def get_followers(target_username):
    if follows_collection is None:
        return jsonify([])
    viewer = current_user()
    follower_names = [f['follower'] for f in follows_collection.find({'following': target_username})]
    # For each follower, check if viewer follows them back
    result = []
    for name in follower_names:
        is_following_back = False
        if viewer and viewer != name:
            is_following_back = follows_collection.find_one({'follower': viewer, 'following': name}) is not None
        bio = ''
        avatar_url = ''
        if users_collection is not None:
            u = users_collection.find_one({'username': name}, {'bio': 1, 'avatar_url': 1, '_id': 0})
            if u:
                bio = u.get('bio', '')
                avatar_url = u.get('avatar_url', '')
        result.append({'username': name, 'is_following': is_following_back, 'bio': bio, 'avatar_url': avatar_url})
    return jsonify(result)

@app.route('/api/following/<target_username>')
def get_following(target_username):
    if follows_collection is None:
        return jsonify([])
    viewer = current_user()
    following_names = [f['following'] for f in follows_collection.find({'follower': target_username})]
    result = []
    for name in following_names:
        is_following_back = False
        if viewer and viewer != name:
            is_following_back = follows_collection.find_one({'follower': viewer, 'following': name}) is not None
        bio = ''
        avatar_url = ''
        if users_collection is not None:
            u = users_collection.find_one({'username': name}, {'bio': 1, 'avatar_url': 1, '_id': 0})
            if u:
                bio = u.get('bio', '')
                avatar_url = u.get('avatar_url', '')
        result.append({'username': name, 'is_following': is_following_back, 'bio': bio, 'avatar_url': avatar_url})
    return jsonify(result)

@app.route('/api/users/discover')
def discover_users():
    """Return active users the viewer doesn't follow yet, ordered by review count."""
    viewer = current_user()
    if comments_collection is None or users_collection is None:
        return jsonify([])
    # Get users already followed
    already_following = set()
    if viewer and follows_collection is not None:
        already_following = {f['following'] for f in follows_collection.find({'follower': viewer})}
    # Aggregate most active users
    pipeline = [
        {'$group': {'_id': '$username', 'review_count': {'$sum': 1}}},
        {'$sort': {'review_count': -1}},
        {'$limit': 30}
    ]
    active = list(comments_collection.aggregate(pipeline))
    result = []
    for u in active:
        name = u['_id']
        if not name or name == viewer or name in already_following:
            continue
        bio = ''
        avatar_url = ''
        if users_collection is not None:
            doc = users_collection.find_one({'username': name}, {'bio': 1, 'avatar_url': 1, '_id': 0})
            if doc:
                bio = doc.get('bio', '')
                avatar_url = doc.get('avatar_url', '')
        result.append({'username': name, 'review_count': u['review_count'], 'bio': bio, 'avatar_url': avatar_url})
        if len(result) >= 8:
            break
    return jsonify(result)

# ===== Birthdate =====
@app.route('/api/profile/birthdate', methods=['POST'])
def update_birthdate():
    username = current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    if users_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    bd = (request.json or {}).get('birthdate', '').strip()  # format MM-DD
    import re
    if bd and not re.match(r'^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$', bd):
        return jsonify({'error': 'Invalid format, use MM-DD'}), 400
    users_collection.update_one({'username': username}, {'$set': {'birthdate': bd}})
    return jsonify({'success': True})

@app.route('/api/birthday/trigger', methods=['POST'])
def birthday_trigger():
    """Called when a user loads any page. Sends birthday DMs to mutuals if today is their birthday."""
    username = current_user()
    if not username or users_collection is None or messages_collection is None:
        return jsonify({'ok': False})
    user_doc = users_collection.find_one({'username': username}, {'_id': 0, 'birthdate': 1, 'last_birthday_dm_date': 1})
    if not user_doc or not user_doc.get('birthdate'):
        return jsonify({'ok': False})
    today = datetime.now().strftime('%m-%d')
    if user_doc.get('birthdate') != today:
        return jsonify({'ok': False})
    today_full = datetime.now().strftime('%Y-%m-%d')
    if user_doc.get('last_birthday_dm_date') == today_full:
        return jsonify({'ok': True, 'already_sent': True})
    # Get mutuals
    if follows_collection is None:
        return jsonify({'ok': False})
    following = {f['following'] for f in follows_collection.find({'follower': username})}
    followers = {f['follower'] for f in follows_collection.find({'following': username})}
    mutuals = list(following & followers)
    if not mutuals:
        users_collection.update_one({'username': username}, {'$set': {'last_birthday_dm_date': today_full}})
        return jsonify({'ok': True, 'sent': 0})
    msg_text = f"🎂 It's {username}'s birthday today!"
    sent = 0
    for mutual in mutuals:
        msg = {
            'msg_id': str(uuid.uuid4())[:12],
            'sender': 'MediaPedia',
            'text': msg_text,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'read': False
        }
        key = sorted(['MediaPedia', mutual])
        messages_collection.update_one(
            {'participants': key},
            {
                '$push': {'messages': msg},
                '$set': {'updated_at': datetime.now(timezone.utc)},
                '$setOnInsert': {'participants': key}
            },
            upsert=True
        )
        sent += 1
    users_collection.update_one({'username': username}, {'$set': {'last_birthday_dm_date': today_full}})
    return jsonify({'ok': True, 'sent': sent})

# ===== Bio Update =====
@app.route('/api/profile/bio', methods=['POST'])
def update_bio():
    username = current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    if users_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    bio = (request.json or {}).get('bio', '').strip()[:200]
    users_collection.update_one({'username': username}, {'$set': {'bio': bio}})
    return jsonify({'success': True})

# ===== Image Storage (MongoDB) =====
images_collection = None
try:
    if db is not None:
        images_collection = db.images
        images_collection.create_index('image_id', unique=True)
except Exception as e:
    log.error('Failed to init images collection: %s', e)

ALLOWED_EXT = {'jpg', 'jpeg', 'png', 'gif', 'webp'}
EXT_MIME = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'gif': 'image/gif', 'webp': 'image/webp'}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB

MAX_IMG_DIMENSION = 1080  # max width or height in pixels

def _compress_image(data, ext):
    """Resize to max 1080px on longest side and convert to JPEG (or keep PNG if transparent)."""
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(data))
        # Convert palette/RGBA to RGB for JPEG, keep PNG for transparency
        has_alpha = img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info)
        # Resize if needed
        w, h = img.size
        if max(w, h) > MAX_IMG_DIMENSION:
            ratio = MAX_IMG_DIMENSION / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        if has_alpha or ext == 'png':
            img = img.convert('RGBA')
            img.save(buf, format='PNG', optimize=True)
            return buf.getvalue(), 'image/png'
        else:
            img = img.convert('RGB')
            img.save(buf, format='JPEG', quality=82, optimize=True)
            return buf.getvalue(), 'image/jpeg'
    except Exception as e:
        log.error('Image compression failed: %s', e)
        return data, EXT_MIME.get(ext, 'image/jpeg')

@app.route('/api/upload', methods=['POST'])
def upload_file():
    username = current_user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    if images_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'error': 'No file'}), 400
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
    if ext not in ALLOWED_EXT:
        return jsonify({'error': 'Only jpg/png/gif/webp allowed'}), 400
    data = f.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return jsonify({'error': 'File too large (max 5 MB)'}), 400
    data, mime = _compress_image(data, ext)
    image_id = uuid.uuid4().hex[:16]
    images_collection.insert_one({
        'image_id': image_id,
        'data': data,
        'mime': mime,
        'uploaded_by': username,
        'created_at': datetime.now(timezone.utc)
    })
    return jsonify({'url': f'/api/img/{image_id}'}), 201

@app.route('/api/img/<image_id>')
def serve_image(image_id):
    if images_collection is None:
        return '', 503
    doc = images_collection.find_one({'image_id': image_id}, {'data': 1, 'mime': 1, '_id': 0})
    if not doc:
        return '', 404
    from flask import Response
    return Response(
        doc['data'],
        content_type=doc.get('mime', 'image/jpeg'),
        headers={'Cache-Control': 'public, max-age=31536000'}
    )

# ===== Avatar Update =====
@app.route('/api/profile/avatar', methods=['POST'])
def update_avatar():
    username = current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    if users_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    url = (request.json or {}).get('url', '').strip()[:500]
    if url and not url.startswith(('http://', 'https://', '/api/img/')):
        return jsonify({'error': 'Invalid URL'}), 400
    users_collection.update_one({'username': username}, {'$set': {'avatar_url': url}})
    return jsonify({'success': True, 'avatar_url': url})

# ===== Posts (mutual-only wall) =====
@app.route('/api/posts/<target_username>', methods=['GET'])
def get_posts(target_username):
    try:
        viewer = current_user()
        if posts_collection is None:
            return jsonify([]), 500
        # owner can always see their own posts
        if viewer != target_username:
            if not viewer:
                return jsonify({'error': 'Login required'}), 401
            if not are_mutual_followers(viewer, target_username):
                return jsonify({'error': 'mutuals_only'}), 403
        posts = list(posts_collection.find(
            {'username': target_username}, {'_id': 0}
        ).sort('created_at', -1).limit(50))
        for p in posts:
            if 'created_at' in p:
                p['created_at'] = _to_ist_str(p['created_at'])
        return jsonify(posts)
    except Exception as e:
        log.error('get_posts error: %s', e)
        return jsonify({'error': str(e)}), 500

@app.route('/api/posts', methods=['POST'])
def create_post():
    try:
        username = current_user()
        if not username:
            return jsonify({'error': 'Login required'}), 401
        if posts_collection is None:
            return jsonify({'error': 'DB unavailable'}), 500
        data = request.json or {}
        text = data.get('text', '').strip()[:500]
        media_url = data.get('media_url', '').strip()[:500]
        if not text and not media_url:
            return jsonify({'error': 'Post needs text or media'}), 400
        if media_url and not media_url.startswith(('http://', 'https://', '/api/img/')):
            return jsonify({'error': 'Invalid URL'}), 400
        post = {
            'post_id': str(uuid.uuid4())[:10],
            'username': username,
            'text': text,
            'media_url': media_url,
            'created_at': datetime.now(timezone.utc),
            'likes': []
        }
        posts_collection.insert_one(post)
        post.pop('_id', None)
        post['created_at'] = _to_ist_str(post['created_at'])
        return jsonify(post), 201
    except Exception as e:
        log.error('create_post error: %s', e)
        return jsonify({'error': str(e)}), 500

@app.route('/api/posts/<post_id>/like', methods=['POST'])
def like_post(post_id):
    username = current_user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    if posts_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    post = posts_collection.find_one({'post_id': post_id}, {'_id': 0, 'likes': 1})
    if not post:
        return jsonify({'error': 'Not found'}), 404
    likes = post.get('likes', [])
    if username in likes:
        posts_collection.update_one({'post_id': post_id}, {'$pull': {'likes': username}})
        new_likes = [l for l in likes if l != username]
        return jsonify({'liked': False, 'count': len(new_likes), 'likers': new_likes})
    posts_collection.update_one({'post_id': post_id}, {'$addToSet': {'likes': username}})
    new_likes = likes + [username]
    return jsonify({'liked': True, 'count': len(new_likes), 'likers': new_likes})

@app.route('/api/posts/<post_id>/comments', methods=['GET'])
def get_post_comments(post_id):
    if posts_collection is None:
        return jsonify([]), 500
    post = posts_collection.find_one({'post_id': post_id}, {'_id': 0, 'post_comments': 1})
    if not post:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(post.get('post_comments', []))

@app.route('/api/posts/<post_id>/comments', methods=['POST'])
def add_post_comment(post_id):
    username = current_user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    if posts_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    text = (request.json or {}).get('text', '').strip()[:300]
    if not text:
        return jsonify({'error': 'Text required'}), 400
    comment = {
        'comment_id': str(uuid.uuid4())[:10],
        'username': username,
        'text': text,
        'created_at': datetime.now(timezone.utc).isoformat()
    }
    posts_collection.update_one({'post_id': post_id}, {'$push': {'post_comments': comment}})
    return jsonify(comment), 201

@app.route('/api/feed/mutuals')
def mutual_feed():
    username = current_user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    if posts_collection is None or follows_collection is None:
        return jsonify([])
    mutuals = get_mutual_followers(username)
    feed_users = mutuals + [username]
    posts = list(posts_collection.find(
        {'username': {'$in': feed_users}}, {'_id': 0}
    ).sort('created_at', -1).limit(40))
    for p in posts:
        if 'created_at' in p:
            p['created_at'] = _to_ist_str(p['created_at'])
    return jsonify(posts)

@app.route('/api/posts/<post_id>', methods=['DELETE'])
def delete_post(post_id):
    username = current_user()
    if not username or posts_collection is None:
        return jsonify({'error': 'Unauthorized'}), 401
    posts_collection.delete_one({'post_id': post_id, 'username': username})
    return jsonify({'success': True})

@app.route('/api/posts/<post_id>', methods=['PATCH'])
def edit_post(post_id):
    username = current_user()
    if not username or posts_collection is None:
        return jsonify({'error': 'Unauthorized'}), 401
    text = (request.json or {}).get('text', '').strip()[:500]
    posts_collection.update_one({'post_id': post_id, 'username': username}, {'$set': {'text': text}})
    return jsonify({'success': True})

# ===== Playlist Routes =====

@app.route('/api/playlists', methods=['GET'])
def get_playlists():
    username = current_user()
    if not username or playlists_collection is None:
        return jsonify([])
    playlists = list(playlists_collection.find({'username': username}, {'_id': 0}))
    for pl in playlists:
        if isinstance(pl.get('created_at'), datetime):
            pl['created_at'] = pl['created_at'].isoformat()
    return jsonify(playlists)

@app.route('/api/playlists/user/<target_username>', methods=['GET'])
def get_user_playlists(target_username):
    if playlists_collection is None:
        return jsonify([])
    playlists = list(playlists_collection.find({'username': target_username}, {'_id': 0}))
    for pl in playlists:
        if isinstance(pl.get('created_at'), datetime):
            pl['created_at'] = pl['created_at'].isoformat()
    return jsonify(playlists)

@app.route('/api/playlists', methods=['POST'])
def create_playlist():
    username = current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    if playlists_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    data = request.json
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Playlist name required'}), 400
    playlist_id = str(uuid.uuid4())[:10]
    doc = {
        'playlist_id': playlist_id,
        'username': username,
        'name': name,
        'songs': [],
        'created_at': datetime.now(timezone.utc)
    }
    playlists_collection.insert_one(doc)
    return jsonify({'playlist_id': playlist_id, 'name': name, 'songs': []}), 201

@app.route('/api/playlists/recommendations')
def playlist_recommendations():
    playlist_id = request.args.get('playlist_id', '')
    username = current_user()
    songs = []
    if playlist_id and playlists_collection is not None:
        pl = playlists_collection.find_one({'playlist_id': playlist_id}, {'_id': 0, 'songs': 1})
        if pl:
            songs = pl.get('songs', [])
    elif username and playlists_collection is not None:
        for pl in playlists_collection.find({'username': username}, {'_id': 0, 'songs': 1}):
            songs.extend(pl.get('songs', []))
    seen_ids = set()
    in_playlist = []
    existing_names = set()
    genre_counts = {}
    for song in songs:
        aname = song.get('artist_name', '').strip()
        aid = song.get('artist_id', 0)
        if not aname:
            continue
        existing_names.add(aname.lower())
        row = None
        if aid and aid != 0:
            r = artists_df[artists_df['ID'] == aid]
            if not r.empty:
                row = r.iloc[0]
        if row is None:
            r = artists_df[artists_df['artist_name'].str.lower() == aname.lower()]
            if r.empty:
                r = artists_df[artists_df['artist_name'].str.lower().str.contains(aname.lower(), na=False)]
            if not r.empty:
                row = r.iloc[0]
        if row is not None:
            rid = int(row['ID'])
            if rid not in seen_ids:
                seen_ids.add(rid)
                entry = row.to_dict()
                entry['in_csv'] = True
                in_playlist.append(entry)
            for g in str(row.get('artist_genre', '')).split(','):
                g = g.strip().lower()
                if g:
                    genre_counts[g] = genre_counts.get(g, 0) + 1
        else:
            key = aname.lower()
            if key not in {a.get('artist_name', '').lower() for a in in_playlist}:
                in_playlist.append({'artist_name': aname, 'artist_genre': '', 'ID': 0, 'in_csv': False})
    seen_n = set()
    deduped = []
    for a in in_playlist:
        n = a.get('artist_name', '').lower()
        if n not in seen_n:
            seen_n.add(n)
            deduped.append(a)
    in_playlist = deduped
    suggested = []
    if genre_counts:
        top_genres = sorted(genre_counts, key=genre_counts.get, reverse=True)[:3]
        mask = artists_df['artist_genre'].str.lower().apply(
            lambda g: any(tg in g for tg in top_genres)
        )
        candidates = artists_df[mask]
        candidates = candidates[~candidates['artist_name'].str.lower().isin(existing_names)]
        candidates = candidates[~candidates['ID'].isin(seen_ids)]
        if not candidates.empty:
            suggested = candidates.sample(n=min(8, len(candidates))).to_dict(orient='records')
    else:
        s = artists_df.sample(n=min(8, len(artists_df))).to_dict(orient='records')
        suggested = [a for a in s if a.get('artist_name', '').lower() not in existing_names]
    return jsonify({'in_playlist': in_playlist, 'suggested': suggested})

@app.route('/api/playlists/<playlist_id>', methods=['GET'])
def get_playlist(playlist_id):
    if playlists_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    pl = playlists_collection.find_one({'playlist_id': playlist_id}, {'_id': 0})
    if not pl:
        return jsonify({'error': 'Not found'}), 404
    if isinstance(pl.get('created_at'), datetime):
        pl['created_at'] = pl['created_at'].isoformat()
    return jsonify(pl)

@app.route('/api/playlists/<playlist_id>/rename', methods=['POST'])
def rename_playlist(playlist_id):
    username = current_user()
    if not username or playlists_collection is None:
        return jsonify({'error': 'Unauthorized'}), 401
    name = request.json.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Name required'}), 400
    playlists_collection.update_one(
        {'playlist_id': playlist_id, 'username': username},
        {'$set': {'name': name}}
    )
    return jsonify({'success': True})

@app.route('/api/playlists/<playlist_id>', methods=['DELETE'])
def delete_playlist(playlist_id):
    username = current_user()
    if not username or playlists_collection is None:
        return jsonify({'error': 'Unauthorized'}), 401
    playlists_collection.delete_one({'playlist_id': playlist_id, 'username': username})
    return jsonify({'success': True})

@app.route('/api/playlists/<playlist_id>/collaborators', methods=['GET'])
def get_collaborators(playlist_id):
    if playlists_collection is None:
        return jsonify([]), 500
    pl = playlists_collection.find_one({'playlist_id': playlist_id}, {'_id': 0, 'collaborators': 1})
    return jsonify(pl.get('collaborators', []) if pl else [])

@app.route('/api/playlists/<playlist_id>/collaborators', methods=['POST'])
def add_collaborator(playlist_id):
    username = current_user()
    if not username or playlists_collection is None:
        return jsonify({'error': 'Unauthorized'}), 401
    target = (request.json or {}).get('username', '').strip()
    if not target:
        return jsonify({'error': 'Username required'}), 400
    if target == username:
        return jsonify({'error': 'Cannot add yourself'}), 400
    if users_collection is not None and not users_collection.find_one({'username': target}):
        return jsonify({'error': 'User not found'}), 404
    result = playlists_collection.update_one(
        {'playlist_id': playlist_id, 'username': username},
        {'$addToSet': {'collaborators': target}}
    )
    if result.matched_count == 0:
        return jsonify({'error': 'Playlist not found or not owner'}), 404
    return jsonify({'success': True})

@app.route('/api/playlists/<playlist_id>/collaborators/<target>', methods=['DELETE'])
def remove_collaborator(playlist_id, target):
    username = current_user()
    if not username or playlists_collection is None:
        return jsonify({'error': 'Unauthorized'}), 401
    playlists_collection.update_one(
        {'playlist_id': playlist_id, 'username': username},
        {'$pull': {'collaborators': target}}
    )
    return jsonify({'success': True})

@app.route('/api/playlists/<playlist_id>/songs', methods=['POST'])
def add_song(playlist_id):
    username = current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    if playlists_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    pl = playlists_collection.find_one({'playlist_id': playlist_id}, {'_id': 0, 'username': 1, 'collaborators': 1})
    if not pl:
        return jsonify({'error': 'Playlist not found'}), 404
    if pl['username'] != username and username not in pl.get('collaborators', []):
        return jsonify({'error': 'Not authorized'}), 403
    data = request.json
    artist_name = data.get('artist_name', '').strip()
    artist_id = data.get('artist_id', 0)
    if artist_name:
        match = artists_df[artists_df['artist_name'].str.lower() == artist_name.lower()]
        if match.empty:
            match = artists_df[artists_df['artist_name'].str.lower().str.contains(artist_name.lower(), na=False)]
        if not match.empty:
            artist_id = int(match.iloc[0]['ID'])
    song = {
        'song_id': str(uuid.uuid4())[:8],
        'song_title': data.get('song_title', '').strip(),
        'artist_name': artist_name,
        'artist_id': artist_id,
        'youtube_query': data.get('youtube_query', '').strip(),
        'added_by': username,
        'added_at': datetime.now(timezone.utc).isoformat()
    }
    if not song['song_title'] or not song['artist_name']:
        return jsonify({'error': 'Song title and artist required'}), 400
    playlists_collection.update_one({'playlist_id': playlist_id}, {'$push': {'songs': song}})
    return jsonify(song), 201

@app.route('/api/playlists/<playlist_id>/songs/<song_id>', methods=['DELETE'])
def remove_song(playlist_id, song_id):
    username = current_user()
    if not username or playlists_collection is None:
        return jsonify({'error': 'Unauthorized'}), 401
    pl = playlists_collection.find_one({'playlist_id': playlist_id}, {'_id': 0, 'username': 1, 'collaborators': 1, 'songs': 1})
    if not pl:
        return jsonify({'error': 'Not found'}), 404
    is_owner = pl['username'] == username
    is_collab = username in pl.get('collaborators', [])
    if not is_owner and not is_collab:
        return jsonify({'error': 'Not authorized'}), 403
    # Collaborators can only remove their own songs
    if not is_owner:
        song = next((s for s in pl.get('songs', []) if s['song_id'] == song_id), None)
        if song and song.get('added_by') != username:
            return jsonify({'error': 'Can only remove your own songs'}), 403
    playlists_collection.update_one(
        {'playlist_id': playlist_id},
        {'$pull': {'songs': {'song_id': song_id}}}
    )
    return jsonify({'success': True})

# ===== Liked Songs =====
liked_collection = None
try:
    if db is not None:
        liked_collection = db.liked_songs
        liked_collection.create_index([('username', 1), ('song_id', 1)], unique=True)
except Exception as e:
    log.error("Failed to init liked_songs collection: %s", e)
    liked_collection = None

# ===== Push Subscriptions Collection =====
push_subs_collection = None
try:
    if db is not None:
        push_subs_collection = db.push_subscriptions
        push_subs_collection.create_index([('username', 1)], unique=True)
except Exception as e:
    log.error("Failed to init push_subscriptions collection: %s", e)
    push_subs_collection = None

VAPID_PUBLIC_KEY = os.getenv('VAPID_PUBLIC_KEY', '')
VAPID_PRIVATE_KEY = os.getenv('VAPID_PRIVATE_KEY', '')
VAPID_CLAIMS_EMAIL = os.getenv('VAPID_CLAIMS_EMAIL', 'mailto:admin@mediapedia.app')

if push_subs_collection is not None:
    init_push(push_subs_collection, VAPID_PRIVATE_KEY, VAPID_CLAIMS_EMAIL)

@app.route('/api/liked_songs', methods=['GET'])
def get_liked_songs():
    username = current_user()
    if not username or liked_collection is None:
        return jsonify([])
    liked = list(liked_collection.find({'username': username}, {'_id': 0, 'song_id': 1}))
    return jsonify([l['song_id'] for l in liked])

@app.route('/api/liked_songs/<song_id>', methods=['POST'])
def toggle_liked_song(song_id):
    username = current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    if liked_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    existing = liked_collection.find_one({'username': username, 'song_id': song_id})
    if existing:
        liked_collection.delete_one({'username': username, 'song_id': song_id})
        return jsonify({'liked': False})
    data = request.json or {}
    liked_collection.insert_one({
        'username': username, 'song_id': song_id,
        'song_title': data.get('song_title', ''),
        'artist_name': data.get('artist_name', ''),
        'youtube_query': data.get('youtube_query', ''),
        'added_at': datetime.now(timezone.utc).isoformat()
    })
    return jsonify({'liked': True})

@app.route('/api/playlists/<playlist_id>/reorder', methods=['POST'])
def reorder_songs(playlist_id):
    username = current_user()
    if not username or playlists_collection is None:
        return jsonify({'error': 'Unauthorized'}), 401
    songs = request.json.get('songs', [])
    playlists_collection.update_one(
        {'playlist_id': playlist_id, 'username': username},
        {'$set': {'songs': songs}}
    )
    return jsonify({'success': True})



@app.route('/playlist/<playlist_id>')
def view_playlist(playlist_id):
    if playlists_collection is None:
        return render_template('404.html'), 404
    pl = playlists_collection.find_one({'playlist_id': playlist_id}, {'_id': 0})
    if not pl:
        return render_template('404.html'), 404
    if isinstance(pl.get('created_at'), datetime):
        pl['created_at'] = pl['created_at'].isoformat()
    api_key_1 = os.getenv("YOUTUBE_API_KEY_1", "")
    api_key_2 = os.getenv("YOUTUBE_API_KEY_2", "")
    api_key_3 = os.getenv("YOUTUBE_API_KEY_3", "")
    api_key_4 = os.getenv("YOUTUBE_API_KEY_4", "")
    api_key_5 = os.getenv("YOUTUBE_API_KEY_5", "")

    return render_template('playlist.html', pl=pl, username=current_user(),
                           api_key_1=api_key_1, api_key_2=api_key_2, api_key_3=api_key_3, api_key_4=api_key_4, api_key_5=api_key_5)

# ===== Messaging =====
@app.route('/messages')
def inbox():
    username = current_user()
    if not username:
        return redirect('/login')
    if messages_collection is None:
        return render_template('messages.html', conversations=[], username=username)

    # Find all conversations involving this user
    raw = list(messages_collection.find(
        {'participants': username}, {'_id': 0}
    ).sort('updated_at', -1))

    seen = {}
    for conv in raw:
        other = next((p for p in conv['participants'] if p != username), None)
        if not other or other in seen:
            continue
        last_msg = conv['messages'][-1] if conv.get('messages') else None
        unread = sum(1 for m in conv.get('messages', [])
                     if m['sender'] != username and not m.get('read'))
        seen[other] = {'other_user': other, 'last_message': last_msg, 'unread_count': unread}

    return render_template('messages.html', conversations=list(seen.values()), username=username)


@app.route('/messages/<other_user>')
def conversation(other_user):
    username = current_user()
    if not username:
        return redirect('/login')
    if username == other_user:
        return redirect('/messages')

    # Mark messages from other_user as read
    if messages_collection is not None:
        key = sorted([username, other_user])
        messages_collection.update_one(
            {'participants': key},
            {'$set': {'messages.$[elem].read': True}},
            array_filters=[{'elem.sender': other_user, 'elem.read': False}]
        )
        conv = messages_collection.find_one({'participants': key}, {'_id': 0})
        msgs = conv.get('messages', []) if conv else []
        # Convert UTC timestamps to IST for display
        from datetime import timezone, timedelta
        IST = timedelta(hours=5, minutes=30)
        for m in msgs:
            ts = m.get('created_at', '')
            if ts and 'T' in str(ts):
                try:
                    dt = datetime.fromisoformat(str(ts).replace('Z',''))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    m['created_at'] = (dt + IST).strftime('%Y-%m-%dT%H:%M:%S')
                except Exception:
                    pass
    else:
        msgs = []

    return render_template('conversation.html', username=username,
                           other_user=other_user, messages=msgs,
                           api_key_1=os.getenv('YOUTUBE_API_KEY_1',''),
                           api_key_2=os.getenv('YOUTUBE_API_KEY_2',''),
                           api_key_3=os.getenv('YOUTUBE_API_KEY_3',''),
                           api_key_4=os.getenv('YOUTUBE_API_KEY_4',''),
                           api_key_5=os.getenv('YOUTUBE_API_KEY_5',''))


@app.route('/api/messages/send', methods=['POST'])
def send_message():
    username = current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    if messages_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500

    data = request.json or {}
    to = data.get('to', '').strip()
    text = data.get('text', '').strip()[:1000]
    media_url = data.get('media_url', '').strip()[:500]

    if not to or (not text and not media_url):
        return jsonify({'error': 'Recipient and message or image required'}), 400
    if to == username:
        return jsonify({'error': 'Cannot message yourself'}), 400

    # Verify recipient exists
    if users_collection is not None:
        if not users_collection.find_one({'username': to}):
            return jsonify({'error': 'User not found'}), 404

    if media_url and not media_url.startswith(('http://', 'https://', '/api/img/')):
        return jsonify({'error': 'Invalid media URL'}), 400

    # Love Letter fields
    love_letter = data.get('love_letter')
    if love_letter:
        photo_url = love_letter.get('photo_url', '').strip()[:500]
        if photo_url and not photo_url.startswith(('http://', 'https://', '/api/img/')):
            return jsonify({'error': 'Invalid photo URL'}), 400
        love_letter = {
            'photo_url': photo_url,
            'song_query': love_letter.get('song_query', '').strip()[:200],
            'song_video_id': love_letter.get('song_video_id', '').strip()[:20],
            'song_title': love_letter.get('song_title', '').strip()[:200],
            'song_thumbnail': love_letter.get('song_thumbnail', '').strip()[:300],
            'song_start': max(0, int(love_letter.get('song_start', 0) or 0)),
            'note': love_letter.get('note', '').strip()[:500],
        }

    msg = {
        'msg_id': str(uuid.uuid4())[:12],
        'sender': username,
        'text': text,
        'media_url': media_url,
        'love_letter': love_letter,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'read': False
    }

    key = sorted([username, to])
    messages_collection.update_one(
        {'participants': key},
        {
            '$push': {'messages': msg},
            '$set': {'updated_at': datetime.now(timezone.utc)},
            '$setOnInsert': {'participants': key}
        },
        upsert=True
    )
    send_push(to, {
        'title': f'💬 {username}',
        'body': text[:80] if text else ('💌 Love Letter' if love_letter else '📷 Photo'),
        'url': f'/messages/{username}',
        'tag': f'dm-{username}'
    })
    return jsonify(msg), 201


@app.route('/api/messages/<msg_id>', methods=['DELETE'])
def delete_message(msg_id):
    username = current_user()
    if not username or messages_collection is None:
        return jsonify({'error': 'Unauthorized'}), 401
    messages_collection.update_one(
        {'messages.msg_id': msg_id, 'messages.sender': username},
        {'$pull': {'messages': {'msg_id': msg_id, 'sender': username}}}
    )
    return jsonify({'success': True})


@app.route('/api/messages/poll')
def poll_messages():
    username = current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    if messages_collection is None:
        return jsonify({'messages': []})

    other = request.args.get('with', '').strip()
    after_id = request.args.get('after', '').strip()

    if not other:
        return jsonify({'messages': []})

    key = sorted([username, other])
    conv = messages_collection.find_one({'participants': key}, {'_id': 0, 'messages': 1})
    if not conv:
        return jsonify({'messages': []})

    msgs = conv.get('messages', [])
    if after_id:
        # Return only messages after the given msg_id
        ids = [m['msg_id'] for m in msgs]
        if after_id in ids:
            msgs = msgs[ids.index(after_id) + 1:]
        else:
            msgs = []

    # Mark polled messages as read
    messages_collection.update_one(
        {'participants': key},
        {'$set': {'messages.$[elem].read': True}},
        array_filters=[{'elem.sender': other, 'elem.read': False}]
    )

    return jsonify({'messages': msgs})


@app.route('/api/messages/unread_count')
def unread_count():
    username = current_user()
    if not username or messages_collection is None:
        return jsonify({'count': 0})
    convs = list(messages_collection.find({'participants': username}, {'_id': 0, 'messages': 1}))
    count = sum(
        1 for conv in convs
        for m in conv.get('messages', [])
        if m['sender'] != username and not m.get('read')
    )
    return jsonify({'count': count})

# Add these helper functions after current_user() and before the party routes

def get_mutual_followers(username):
    """Get list of users who follow the given user AND are followed back"""
    if follows_collection is None:
        return []
    
    following = {f['following'] for f in follows_collection.find({'follower': username})}
    followers = {f['follower'] for f in follows_collection.find({'following': username})}
    return list(following & followers)

def are_mutual_followers(user1, user2):
    """Check if two users follow each other"""
    if follows_collection is None or user1 == user2:
        return False
    
    user1_follows_user2 = follows_collection.find_one({'follower': user1, 'following': user2}) is not None
    user2_follows_user1 = follows_collection.find_one({'follower': user2, 'following': user1}) is not None
    
    return user1_follows_user2 and user2_follows_user1

# ===== Party Collection =====
party_collection = None
try:
    if db is not None:
        party_collection = db.parties
        party_collection.create_index('party_id', unique=True)
except Exception as e:
    log.error("Failed to init parties collection: %s", e)
    party_collection = None

# ===== Party Invites Collection =====
party_invites_collection = None
try:
    if db is not None:
        party_invites_collection = db.party_invites
        party_invites_collection.create_index([('to_user', 1), ('dismissed', 1)])
except Exception as e:
    log.error("Failed to init party_invites collection: %s", e)
    party_invites_collection = None

# ===== Party HTTP Routes =====
@app.route('/api/mutual_followers')
def get_mutual_followers_api():
    username = current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    
    mutuals = get_mutual_followers(username)
    
    # Get bio info for each
    result = []
    for m in mutuals:
        bio = ''
        if users_collection is not None:
            user = users_collection.find_one({'username': m}, {'bio': 1})
            if user:
                bio = user.get('bio', '')
        result.append({'username': m, 'bio': bio})
    
    return jsonify(result)

@app.route('/party')
def party_lobby():
    username = current_user()
    if not username:
        return redirect('/login')
    return render_template('party.html', username=username,
                           api_key_1=os.getenv('YOUTUBE_API_KEY_1', ''),
                           api_key_2=os.getenv('YOUTUBE_API_KEY_2', ''),
                           api_key_3=os.getenv('YOUTUBE_API_KEY_3', ''),
                           api_key_4=os.getenv('YOUTUBE_API_KEY_4', ''),
                           api_key_5=os.getenv('YOUTUBE_API_KEY_5', ''))

@app.route('/party/<party_id>')
def party_room(party_id):
    username = current_user()
    if not username:
        return redirect('/login')
    if party_collection is None:
        return render_template('404.html'), 404
    party = party_collection.find_one({'party_id': party_id}, {'_id': 0})
    if not party:
        return render_template('404.html'), 404
    return render_template('party.html', username=username, party=party,
                           api_key_1=os.getenv('YOUTUBE_API_KEY_1', ''),
                           api_key_2=os.getenv('YOUTUBE_API_KEY_2', ''),
                           api_key_3=os.getenv('YOUTUBE_API_KEY_3', ''),
                           api_key_4=os.getenv('YOUTUBE_API_KEY_4', ''),
                           api_key_5=os.getenv('YOUTUBE_API_KEY_5', ''))

@app.route('/api/party/create', methods=['POST'])
def create_party():
    username = current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    if party_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    data = request.json or {}
    party_id = str(uuid.uuid4())[:8]
    # Get mutual followers only
    mutuals = get_mutual_followers(username)
    party = {
        'party_id': party_id,
        'name': data.get('name', f"{username}'s Party")[:60],
        'host': username,
        'members': [username],
        'invited': mutuals,
        'allowed_users': mutuals + [username],  # Add this field
        'queue': [],
        'current_index': -1,
        'state': 'stopped',
        'position': 0.0,
        'updated_at': datetime.now(timezone.utc).isoformat()
    }
    party_collection.insert_one(party)
    return jsonify({'party_id': party_id}), 201

@app.route('/api/party/<party_id>/invite', methods=['POST'])
def invite_to_party(party_id):
    """Invite a mutual follower to the party"""
    username = current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401

    target = (request.json or {}).get('username', '').strip()
    if not target:
        return jsonify({'error': 'Username required'}), 400

    if not are_mutual_followers(username, target):
        return jsonify({'error': 'Can only invite mutual followers'}), 403

    if party_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500

    party = party_collection.find_one({'party_id': party_id})
    if not party:
        return jsonify({'error': 'Party not found'}), 404

    if party.get('host') != username:
        return jsonify({'error': 'Only host can invite'}), 403

    # Add to allowed_users so they can join
    party_collection.update_one(
        {'party_id': party_id},
        {'$addToSet': {'invited': target, 'allowed_users': target}}
    )

    # Persist invite in DB so it survives across page navigations
    if party_invites_collection is not None:
        party_invites_collection.update_one(
            {'party_id': party_id, 'to_user': target},
            {'$set': {
                'party_id': party_id,
                'party_name': party.get('name', "Party"),
                'invited_by': username,
                'to_user': target,
                'dismissed': False,
                'created_at': datetime.now(timezone.utc).isoformat()
            }},
            upsert=True
        )

    # Send push notification (works even if user is offline/phone locked)
    send_push(target, {
        'title': '🎉 Party Invite!',
        'body': f'{username} invited you to join "{party.get("name", "Party")}"',
        'url': f'/party/{party_id}',
        'tag': f'party-invite-{party_id}',
        'party_id': party_id,
        'actions': [
            {'action': 'join', 'title': 'Join Party'},
            {'action': 'dismiss', 'title': 'Dismiss'}
        ]
    })

    return jsonify({'success': True, 'party_name': party.get('name', 'Party')})


@app.route('/api/party/pending_invites')
def pending_invites():
    """Return undismissed party invites for the logged-in user."""
    username = current_user()
    if not username:
        return jsonify([]), 401
    if party_invites_collection is None:
        return jsonify([])
    invites = list(party_invites_collection.find(
        {'to_user': username, 'dismissed': False},
        {'_id': 0, 'party_id': 1, 'party_name': 1, 'invited_by': 1}
    ))
    return jsonify(invites)


@app.route('/api/party/dismiss_invite', methods=['POST'])
def dismiss_invite():
    """Mark an invite as dismissed."""
    username = current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    party_id = (request.json or {}).get('party_id', '')
    if party_invites_collection is not None and party_id:
        party_invites_collection.update_one(
            {'party_id': party_id, 'to_user': username},
            {'$set': {'dismissed': True}}
        )
    return jsonify({'success': True})

@app.route('/api/party/<party_id>', methods=['GET'])
def get_party(party_id):
    if party_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    party = party_collection.find_one({'party_id': party_id}, {'_id': 0})
    if not party:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(party)

@app.route('/api/party/<party_id>/state', methods=['GET'])
def get_party_state(party_id):
    if party_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    party = party_collection.find_one({'party_id': party_id}, {'_id': 0})
    if not party:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'state': party.get('state', 'stopped'), 'position': _live_position(party)})

@app.route('/api/party/<party_id>/end', methods=['POST'])
def end_party(party_id):
    username = current_user()
    if not username or party_collection is None:
        return jsonify({'error': 'Unauthorized'}), 401
    party_collection.delete_one({'party_id': party_id, 'host': username})
    return jsonify({'success': True})

# ===== SocketIO Connect — join personal room for invite notifications =====
@socketio.on('connect')
def on_connect():
    username = session.get('username')
    if username:
        join_room(username)  # personal room so party_invite events are delivered

# ===== SocketIO Party Events =====
@socketio.on('join_party')
def on_join_party(data):
    party_id = data.get('party_id')
    username = session.get('username')
    if not party_id or not username or party_collection is None:
        emit('error', {'msg': 'Invalid request'})
        return
    
    party = party_collection.find_one({'party_id': party_id}, {'_id': 0})
    if not party:
        emit('error', {'msg': 'Party not found'})
        return
    
    # Check if user is allowed to join
    host = party.get('host')
    invited = party.get('invited', [])
    allowed_users = party.get('allowed_users', [])
    
    # Host can always join
    if username == host:
        pass
    # Check if user is mutual follower or invited
    elif username not in allowed_users and username not in invited:
        # Check if they're a mutual follower (in case the list wasn't updated)
        if not are_mutual_followers(host, username):
            emit('error', {'msg': 'You must be mutual followers to join this party'})
            return
        # Add to allowed_users
        party_collection.update_one(
            {'party_id': party_id},
            {'$addToSet': {'allowed_users': username}}
        )
    
    join_room(party_id)
    party_collection.update_one({'party_id': party_id}, {'$addToSet': {'members': username}})
    updated = party_collection.find_one({'party_id': party_id}, {'_id': 0})
    # Send full state only to the joining socket
    updated['position'] = _live_position(updated)
    
    # Remove sensitive info before sending
    if 'allowed_users' in updated:
        del updated['allowed_users']
    if 'invited' in updated:
        del updated['invited']
    
    emit('party_state', updated)
    # Notify everyone else about the new member + updated member list
    emit('member_joined', {'username': username, 'members': updated.get('members', [])}, to=party_id)

@socketio.on('invite_to_party')
def on_invite_to_party(data):
    party_id = data.get('party_id')
    target_username = data.get('username')
    inviter = session.get('username')  # trust session, not client payload

    if not party_id or not target_username or not inviter:
        return

    # Verify mutual followers
    if not are_mutual_followers(inviter, target_username):
        emit('error', {'msg': 'Can only invite mutual followers'})
        return

    if party_collection is None:
        return

    party = party_collection.find_one({'party_id': party_id}, {'_id': 0})
    if not party:
        return

    # Persist invite so the user sees it even if not currently online
    if party_invites_collection is not None:
        party_invites_collection.update_one(
            {'party_id': party_id, 'to_user': target_username},
            {'$set': {
                 'party_id': party_id,
                 'party_name': party.get('name', 'Party'),
                 'invited_by': inviter,
                 'to_user': target_username,
                 'dismissed': False,
                 'created_at': datetime.now(timezone.utc).isoformat()
            }},
            upsert=True
        )

    # Also deliver real-time notification if the user is online
    emit('party_invite', {
        'party_id': party_id,
        'party_name': party.get('name', 'Party'),
        'invited_by': inviter
    }, to=target_username)

    # Push notification (works even when app is closed / phone locked)
    send_push(target_username, {
        'title': '🎉 Party Invite!',
        'body': f'{inviter} invited you to join "{party.get("name", "Party")}"',
        'url': f'/party/{party_id}',
        'tag': f'party-invite-{party_id}',
        'party_id': party_id,
        'actions': [
            {'action': 'join', 'title': 'Join Party'},
            {'action': 'dismiss', 'title': 'Dismiss'}
        ]
    })

@socketio.on('disconnect')
def on_disconnect():
    username = session.get('username')
    if not username or party_collection is None:
        return
    # Remove user from all parties they were in
    parties = list(party_collection.find({'members': username}, {'_id': 0, 'party_id': 1, 'members': 1}))
    for party in parties:
        party_id = party['party_id']
        party_collection.update_one({'party_id': party_id}, {'$pull': {'members': username}})
        updated = party_collection.find_one({'party_id': party_id}, {'_id': 0, 'members': 1})
        members = updated.get('members', []) if updated else []
        emit('member_left', {'username': username, 'members': members}, to=party_id)

@socketio.on('leave_party')
def on_leave_party(data):
    party_id = data.get('party_id')
    username = session.get('username')
    if not party_id or not username:
        return
    leave_room(party_id)
    if party_collection is not None:
        party_collection.update_one({'party_id': party_id}, {'$pull': {'members': username}})
        updated = party_collection.find_one({'party_id': party_id}, {'_id': 0})
        members = updated.get('members', []) if updated else []
    else:
        members = []
    emit('member_left', {'username': username, 'members': members}, to=party_id)

def _live_position(party):
    """Compute current playback position accounting for elapsed time."""
    pos = float(party.get('position', 0))
    if party.get('state') == 'playing' and party.get('played_at'):
        try:
            played_at = datetime.fromisoformat(party['played_at'])
            elapsed = (datetime.now(timezone.utc) - played_at).total_seconds()
            pos = pos + max(0, elapsed)
        except (ValueError, OSError) as e:
            log.error("_live_position parse error: %s", e)
    return pos

@socketio.on('party_play')
def on_party_play(data):
    party_id = data.get('party_id')
    position = float(data.get('position', 0))
    username = session.get('username')
    now = datetime.now(timezone.utc).isoformat()
    if party_collection is not None:
        party_collection.update_one({'party_id': party_id},
            {'$set': {'state': 'playing', 'position': position,
                      'played_at': now, 'updated_at': now}})
    emit('sync_play', {'position': position, 'by': username}, to=party_id)

@socketio.on('party_pause')
def on_party_pause(data):
    party_id = data.get('party_id')
    position = float(data.get('position', 0))
    username = session.get('username')
    now = datetime.now(timezone.utc).isoformat()
    if party_collection is not None:
        party_collection.update_one({'party_id': party_id},
            {'$set': {'state': 'paused', 'position': position,
                      'played_at': None, 'updated_at': now}})
    emit('sync_pause', {'position': position, 'by': username}, to=party_id)

@socketio.on('party_seek')
def on_party_seek(data):
    party_id = data.get('party_id')
    position = float(data.get('position', 0))
    username = session.get('username')
    now = datetime.now(timezone.utc).isoformat()
    if party_collection is not None:
        party_collection.update_one({'party_id': party_id},
            {'$set': {'position': position, 'played_at': now, 'updated_at': now}})
    emit('sync_seek', {'position': position, 'by': username}, to=party_id)

@socketio.on('party_change_song')
def on_party_change_song(data):
    party_id = data.get('party_id')
    index = int(data.get('index', 0))
    username = session.get('username')
    now = datetime.now(timezone.utc).isoformat()
    if party_collection is not None:
        party_collection.update_one({'party_id': party_id},
            {'$set': {'current_index': index, 'state': 'playing', 'position': 0,
                      'played_at': now, 'updated_at': now}})
    emit('sync_change_song', {'index': index, 'by': username, 'position': 0, 'paused': False}, to=party_id)

@socketio.on('party_add_song')
def on_party_add_song(data):
    party_id = data.get('party_id')
    song = data.get('song')
    username = session.get('username')
    if not song or not party_id or party_collection is None:
        return
    song['added_by'] = username
    song['song_id'] = song.get('song_id') or str(uuid.uuid4())[:8]
    party_collection.update_one({'party_id': party_id}, {'$push': {'queue': song}})
    party = party_collection.find_one({'party_id': party_id}, {'_id': 0, 'queue': 1, 'current_index': 1})
    if party and party.get('current_index', -1) == -1:
        idx = len(party['queue']) - 1
        now = datetime.now(timezone.utc).isoformat()
        party_collection.update_one({'party_id': party_id},
            {'$set': {'current_index': idx, 'state': 'playing', 'position': 0, 'played_at': now}})
        emit('sync_change_song', {'index': idx, 'by': username}, to=party_id)
    emit('song_added', {'song': song, 'by': username}, to=party_id)

@socketio.on('party_add_song_next')
def on_party_add_song_next(data):
    party_id = data.get('party_id')
    song = data.get('song')
    username = session.get('username')
    if not song or not party_id or party_collection is None:
        return
    song['added_by'] = username
    song['song_id'] = song.get('song_id') or str(uuid.uuid4())[:8]
    party = party_collection.find_one({'party_id': party_id}, {'_id': 0, 'queue': 1, 'current_index': 1})
    if not party:
        return
    cur = party.get('current_index', -1)
    queue = party.get('queue', [])
    insert_at = cur + 1  # right after current song
    queue.insert(insert_at, song)
    party_collection.update_one({'party_id': party_id}, {'$set': {'queue': queue}})
    if cur == -1:
        now = datetime.now(timezone.utc).isoformat()
        party_collection.update_one({'party_id': party_id},
            {'$set': {'current_index': 0, 'state': 'playing', 'position': 0, 'played_at': now}})
        emit('sync_change_song', {'index': 0, 'by': username}, to=party_id)
    emit('song_added', {'song': song, 'by': username, 'insert_next': True, 'insert_at': insert_at}, to=party_id)

@socketio.on('party_remove_song')
def on_party_remove_song(data):
    party_id = data.get('party_id')
    song_id = data.get('song_id')
    username = session.get('username')
    if party_collection is not None:
        # Get current state before removal to detect if playing song was removed
        party = party_collection.find_one({'party_id': party_id}, {'_id': 0, 'queue': 1, 'current_index': 1})
        party_collection.update_one({'party_id': party_id},
            {'$pull': {'queue': {'song_id': song_id}}})
        if party:
            cur = party.get('current_index', -1)
            old_queue = party.get('queue', [])
            removed_idx = next((i for i, s in enumerate(old_queue) if s.get('song_id') == song_id), -1)
            new_len = len(old_queue) - 1
            now = datetime.now(timezone.utc).isoformat()
            if removed_idx != -1 and removed_idx == cur:
                # Currently playing song was removed — advance to next or stop
                new_idx = cur if cur < new_len else (new_len - 1)
                if new_len > 0:
                    party_collection.update_one({'party_id': party_id},
                        {'$set': {'current_index': new_idx, 'state': 'playing',
                                  'position': 0, 'played_at': now}})
                    emit('sync_change_song', {'index': new_idx, 'by': username,
                                              'position': 0, 'paused': False}, to=party_id)
                else:
                    party_collection.update_one({'party_id': party_id},
                        {'$set': {'current_index': -1, 'state': 'stopped', 'position': 0}})
            elif removed_idx != -1 and removed_idx < cur:
                # A song before the current one was removed — shift index down
                party_collection.update_one({'party_id': party_id},
                    {'$set': {'current_index': cur - 1}})
    emit('song_removed', {'song_id': song_id, 'by': username}, to=party_id)

@socketio.on('party_chat')
def on_party_chat(data):
    party_id = data.get('party_id')
    username = session.get('username')
    text = (data.get('text') or '').strip()[:300]
    if not text or not party_id:
        return
    msg = {'username': username, 'text': text,
           'ts': datetime.now(timezone.utc).isoformat()}
    emit('chat_message', msg, to=party_id)

    # Push to offline party members
    if party_collection is not None:
        party = party_collection.find_one({'party_id': party_id}, {'_id': 0, 'members': 1, 'name': 1, 'allowed_users': 1})
        if party:
            all_users = list(set(party.get('members', []) + party.get('allowed_users', [])))
            for u in all_users:
                if u != username:
                    send_push(u, {
                        'title': f'💬 {username} in party chat',
                        'body': text[:80],
                        'url': f'/party/{party_id}',
                        'tag': f'party-chat-{party_id}'
                    })

@socketio.on('party_request_sync')
def on_party_request_sync(data):
    """Member tapped Sync button — re-broadcast live state to that socket only."""
    party_id = data.get('party_id')
    if party_collection is None:
        return
    party = party_collection.find_one({'party_id': party_id}, {'_id': 0})
    if not party:
        return
    emit('request_sync', {}, to=party_id)  # prompt host to re-broadcast

@socketio.on('request_sync')
def on_request_sync(data):
    """New joiner requests live state; respond only to that socket with live position."""
    party_id = data.get('party_id')
    if party_collection is None:
        return
    party = party_collection.find_one({'party_id': party_id}, {'_id': 0})
    if not party:
        return
    # Compute live position so the joiner seeks to the right timestamp
    party['position'] = _live_position(party)
    emit('party_state', party)  # emit only to requesting socket (no room broadcast)


# ===== PWA Routes =====
@app.route('/sw.js')
def service_worker():
    from flask import send_from_directory, make_response
    resp = make_response(send_from_directory('static', 'sw.js'))
    resp.headers['Service-Worker-Allowed'] = '/'
    resp.headers['Content-Type'] = 'application/javascript'
    return resp

@app.route('/offline')
def offline_page():
    from flask import send_from_directory
    return send_from_directory('static', 'offline.html')

# ===== Push Subscription Routes =====
@app.route('/api/push/debug')
def push_debug():
    username = current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    if push_subs_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    doc = push_subs_collection.find_one({'username': username}, {'_id': 0})
    if not doc:
        return jsonify({'subscribed': False, 'username': username})
    sub = doc.get('subscription', {})
    return jsonify({
        'subscribed': True,
        'username': username,
        'endpoint_start': sub.get('endpoint', '')[:60]
    })

@app.route('/api/push/test')
def push_test():
    username = current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    if push_subs_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    doc = push_subs_collection.find_one({'username': username}, {'_id': 0, 'subscription': 1})
    if not doc:
        return jsonify({'error': 'No subscription found'}), 404
    if not VAPID_PRIVATE_KEY:
        return jsonify({'error': 'VAPID_PRIVATE_KEY not set'}), 500
    send_push(username, {
        'title': '🎉 Test Notification',
        'body': 'MediaPedia push is working!',
        'url': '/party',
        'tag': 'test'
    })
    return jsonify({'sent': True, 'to': username})

@app.route('/api/push/vapid_public_key')
def vapid_public_key():
    return jsonify({'key': VAPID_PUBLIC_KEY})

@app.route('/api/push/subscribe', methods=['POST'])
def push_subscribe():
    username = current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    if push_subs_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    sub = request.json
    if not sub or not sub.get('endpoint'):
        return jsonify({'error': 'Invalid subscription'}), 400
    push_subs_collection.update_one(
        {'username': username},
        {'$set': {'username': username, 'subscription': sub}},
        upsert=True
    )
    return jsonify({'success': True})

@app.route('/api/push/unsubscribe', methods=['POST'])
def push_unsubscribe():
    username = current_user()
    if not username or push_subs_collection is None:
        return jsonify({'error': 'Unauthorized'}), 401
    push_subs_collection.delete_one({'username': username})
    return jsonify({'success': True})


# ===== KING OF CARDS =====

KOC_USERNAMES = {'abhinav', 'A3h1', 'Akhil', 'sahil', 'shruti', 'utkarsh'}

KOC_DISPLAY = {
    'abhinav':  'ABHINAV PANWAR',
    'A3h1': 'ABHISHEK SEHRAWAT',
    'Akhil':    'AKHIL PANWAR',
    'sahil':    'SAHIL PANWAR',
    'shruti':   'SHRUTI SEHRAWAT',
    'utkarsh':  'UTKARSH PANWAR',
}

koc_tournaments_collection = None
try:
    if db is not None:
        koc_tournaments_collection = db.koc_tournaments
        koc_tournaments_collection.create_index('edition', unique=True)
except Exception as e:
    log.error('Failed to init koc_tournaments: %s', e)

def _koc_leaderboard():
    if koc_tournaments_collection is None:
        return []
    tournaments = list(koc_tournaments_collection.find({}, {'_id': 0}))
    stats = {}
    tournament_winners = {}
    for t in tournaments:
        winner = t.get('winner_username')
        if winner:
            tournament_winners[winner] = tournament_winners.get(winner, 0) + 1
        for p in t.get('players', []):
            u = p.get('username', '')
            if u not in stats:
                stats[u] = {
                    'username': u,
                    'display_name': KOC_DISPLAY.get(u, p.get('display_name', u)),
                    'editions_played': 0, 'game_wins': 0,
                    'total_points': 0, 'tournament_wins': 0
                }
            stats[u]['editions_played'] += 1
            stats[u]['total_points'] += p.get('total', 0)
            for game in ('monopoly', 'bluff', 'spoon', 'uno'):
                if p.get('r1_' + game) is not None:
                    if p.get('r1_' + game, 0) >= 2: stats[u]['game_wins'] += 1
                    if p.get('r2_' + game, 0) >= 2: stats[u]['game_wins'] += 1
                else:
                    if p.get(game, 0) >= 2: stats[u]['game_wins'] += 1
    for u, tw in tournament_winners.items():
        if u in stats:
            stats[u]['tournament_wins'] = tw
    return sorted(stats.values(), key=lambda x: (-x['total_points'], -x['game_wins']))

@app.route('/kingofcards')
def kingofcards():
    username = current_user()
    is_koc = username in KOC_USERNAMES if username else False
    tournaments = []
    current_champion = None
    reign_days = 0
    latest_edition = 0
    if koc_tournaments_collection is not None:
        raw = list(koc_tournaments_collection.find({}, {'_id': 0}).sort('edition', 1))
        for t in raw:
            winner_u = t.get('winner_username', '')
            t['winner_display'] = KOC_DISPLAY.get(winner_u, winner_u)
            tournaments.append(t)
        if tournaments:
            last = tournaments[-1]
            latest_edition = last.get('edition', 0)
            current_champion = last.get('winner_display', '')
            # Find streak start: walk back while winner is the same
            champ_u = last.get('winner_username', '')
            streak_start = last
            for t in reversed(tournaments):
                if t.get('winner_username', '') == champ_u:
                    streak_start = t
                else:
                    break
            try:
                played = datetime.fromisoformat(streak_start.get('played_on', ''))
                reign_days = (datetime.now() - played).days
            except Exception:
                reign_days = 0
    leaderboard = _koc_leaderboard()
    # form guide last 5
    form_guide = {}
    last5 = tournaments[-5:] if len(tournaments) >= 5 else tournaments
    for u in KOC_USERNAMES:
        form_guide[u] = []
    for t in last5:
        winner_u = t.get('winner_username', '')
        ps = sorted(t.get('players', []), key=lambda x: -x.get('total', 0))
        positions = {p.get('username',''): i+1 for i, p in enumerate(ps)}
        for u in KOC_USERNAMES:
            pos = positions.get(u)
            if pos == 1: form_guide[u].append('W')
            elif pos is not None: form_guide[u].append('L')
    return render_template('kingofcards.html',
        username=username, is_koc_player=is_koc,
        tournaments=tournaments, leaderboard=leaderboard,
        current_champion=current_champion, reign_days=reign_days,
        latest_edition=latest_edition, form_guide=form_guide)

@app.route('/kingofcards/<int:edition>')
def koc_edition(edition):
    username = current_user()
    if koc_tournaments_collection is None:
        return render_template('404.html'), 404
    t = koc_tournaments_collection.find_one({'edition': edition}, {'_id': 0})
    if not t:
        return render_template('404.html'), 404
    winner_u = t.get('winner_username', '')
    t['winner_display'] = KOC_DISPLAY.get(winner_u, winner_u)
    players_sorted = sorted(t.get('players', []), key=lambda x: -x.get('total', 0))
    # generate story
    story = ''
    if players_sorted:
        winner = players_sorted[0]
        runner_up = players_sorted[1] if len(players_sorted) > 1 else None
        wname = KOC_DISPLAY.get(winner.get('username',''), winner.get('username','')).split()[0]
        margin = winner.get('total',0) - (runner_up.get('total',0) if runner_up else 0)
        games = ['monopoly','bluff','spoon','uno']
        best_game = max(games, key=lambda g: winner.get(g, 0))
        game_spreads = {g: max((p.get(g,0) for p in players_sorted), default=0) - min((p.get(g,0) for p in players_sorted), default=0) for g in games}
        closest_game = min(game_spreads, key=game_spreads.get)
        if margin == 0:
            story = f"In a stunning Edition {edition}, {wname} claimed the crown in a tiebreaker after finishing level on points."
        elif margin == 1:
            rname = KOC_DISPLAY.get(runner_up.get('username',''), runner_up.get('username','')).split()[0] if runner_up else 'the field'
            story = f"Edition {edition} went down to the wire — {wname} edged out {rname} by just 1 point to claim the title. {wname} was dominant in {best_game.capitalize()}."
        else:
            story = f"Edition {edition} belonged to {wname}, who dominated and won by {margin} points. The {closest_game.capitalize()} game was the tensest of the day."
    return render_template('koc_edition.html', tournament=t,
                           players_sorted=players_sorted, username=username, story=story)

@app.route('/kingofcards/live/<session_id>')
def koc_live_page(session_id):
    username = current_user()
    if koc_live_collection is None:
        return render_template('404.html'), 404
    doc = koc_live_collection.find_one({'session_id': session_id}, {'_id': 0})
    if not doc:
        return render_template('404.html'), 404
    is_koc = username in KOC_USERNAMES if username else False
    return render_template('koc_live.html', session_id=session_id,
                           username=username, is_koc_player=is_koc)

@app.route('/api/koc/tournaments', methods=['GET'])
def koc_get_tournaments():
    if koc_tournaments_collection is None:
        return jsonify([])
    data = list(koc_tournaments_collection.find({}, {'_id': 0}).sort('edition', 1))
    return jsonify(data)

@app.route('/api/koc/tournaments', methods=['POST'])
def koc_add_tournament():
    username = current_user()
    if not username or username not in KOC_USERNAMES:
        return jsonify({'error': 'Unauthorized'}), 403
    if koc_tournaments_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    data = request.json or {}
    players = data.get('players', [])
    if not players:
        return jsonify({'error': 'Players data required'}), 400
    last = koc_tournaments_collection.find_one(sort=[('edition', -1)])
    edition = (last['edition'] + 1) if last else 1
    def _wins(p):
        return sum(
            (1 if p.get('r1_'+g, p.get(g,0)) >= 2 else 0) +
            (1 if p.get('r2_'+g, 0) >= 2 else 0)
            for g in ('monopoly', 'bluff', 'spoon', 'uno')
        )
    winner = max(players, key=lambda p: (p.get('total', 0), _wins(p)))
    doc = {
        'edition': edition,
        'played_on': data.get('played_on', ''),
        'winner_username': winner.get('username', ''),
        'players': players,
        'added_by': username,
        'created_at': datetime.now(timezone.utc).isoformat()
    }
    koc_tournaments_collection.insert_one(doc)
    return jsonify({'edition': edition, 'winner': winner.get('username', '')}), 201

@app.route('/api/koc/leaderboard')
def koc_leaderboard_api():
    return jsonify(_koc_leaderboard())

@app.route('/api/koc/tournaments/<int:edition>', methods=['PUT'])
def koc_edit_tournament(edition):
    username = current_user()
    if not username or username not in KOC_USERNAMES:
        return jsonify({'error': 'Unauthorized'}), 403
    if koc_tournaments_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    data = request.json or {}
    players = data.get('players', [])
    if not players:
        return jsonify({'error': 'Players required'}), 400
    def _wins(p):
        return sum(
            (1 if p.get('r1_'+g, p.get(g,0)) >= 2 else 0) +
            (1 if p.get('r2_'+g, 0) >= 2 else 0)
            for g in ('monopoly', 'bluff', 'spoon', 'uno')
        )
    winner = max(players, key=lambda p: (p.get('total',0), _wins(p)))
    koc_tournaments_collection.update_one(
        {'edition': edition},
        {'$set': {
            'played_on': data.get('played_on',''),
            'players': players,
            'winner_username': winner.get('username',''),
            'updated_by': username,
            'updated_at': datetime.now(timezone.utc).isoformat()
        }}
    )
    return jsonify({'success': True, 'winner': winner.get('username','')})

@app.route('/api/koc/tournaments/<int:edition>', methods=['DELETE'])
def koc_delete_tournament(edition):
    username = current_user()
    if not username or username not in KOC_USERNAMES:
        return jsonify({'error': 'Unauthorized'}), 403
    if koc_tournaments_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    koc_tournaments_collection.delete_one({'edition': edition})
    return jsonify({'success': True})

@app.route('/api/koc/h2h')
def koc_h2h():
    p1 = request.args.get('p1', '').strip()
    p2 = request.args.get('p2', '').strip()
    if not p1 or not p2 or koc_tournaments_collection is None:
        return jsonify({'error': 'p1 and p2 required'}), 400
    tournaments = list(koc_tournaments_collection.find({}, {'_id': 0}).sort('edition', 1))
    p1_wins, p2_wins, draws = 0, 0, 0
    editions = []
    for t in tournaments:
        pd1 = next((p for p in t.get('players',[]) if p.get('username')==p1), None)
        pd2 = next((p for p in t.get('players',[]) if p.get('username')==p2), None)
        if not pd1 or not pd2:
            continue
        s1, s2 = pd1.get('total',0), pd2.get('total',0)
        result = 'p1' if s1>s2 else ('p2' if s2>s1 else 'draw')
        if result=='p1': p1_wins+=1
        elif result=='p2': p2_wins+=1
        else: draws+=1
        editions.append({'edition':t['edition'],'played_on':t.get('played_on',''),
                         'p1_score':s1,'p2_score':s2,'result':result})
    return jsonify({
        'p1': p1, 'p1_display': KOC_DISPLAY.get(p1,p1), 'p1_wins': p1_wins,
        'p2': p2, 'p2_display': KOC_DISPLAY.get(p2,p2), 'p2_wins': p2_wins,
        'draws': draws, 'editions': editions
    })

# KOC Live tracker collection
koc_live_collection = None
try:
    if db is not None:
        koc_live_collection = db.koc_live
        koc_live_collection.create_index('session_id', unique=True)
except Exception as e:
    log.error('Failed to init koc_live: %s', e)

@app.route('/api/koc/live', methods=['POST'])
def koc_live_start():
    username = current_user()
    if not username or username not in KOC_USERNAMES:
        return jsonify({'error': 'Unauthorized'}), 403
    if koc_live_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    session_id = str(uuid.uuid4())[:8]
    scores = {}
    for u in KOC_USERNAMES:
        scores[u] = {'r1_monopoly':0,'r1_bluff':0,'r1_spoon':0,'r1_uno':0,
                     'r2_monopoly':0,'r2_bluff':0,'r2_spoon':0,'r2_uno':0,'total':0}
    koc_live_collection.insert_one({
        'session_id': session_id,
        'started_by': username,
        'started_at': datetime.now(timezone.utc).isoformat(),
        'current_game': 'monopoly',
        'round': 1,
        'scores': scores,
        'active': True
    })
    # Notify all KOC players
    for u in KOC_USERNAMES:
        if u != username:
            send_push(u, {
                'title': '🃏 KOC Live Started!',
                'body': f'{username} started a live KOC session',
                'url': f'/kingofcards/live/{session_id}',
                'tag': f'koc-live-{session_id}'
            })
    return jsonify({'session_id': session_id}), 201

@app.route('/api/koc/live/<session_id>', methods=['GET'])
def koc_live_get(session_id):
    if koc_live_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    doc = koc_live_collection.find_one({'session_id': session_id}, {'_id': 0})
    if not doc:
        return jsonify({'error': 'Session not found'}), 404
    return jsonify(doc)

@app.route('/api/koc/live/<session_id>', methods=['PUT'])
def koc_live_update(session_id):
    username = current_user()
    if not username or username not in KOC_USERNAMES:
        return jsonify({'error': 'Unauthorized'}), 403
    if koc_live_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    data = request.json or {}
    update = {}
    if 'scores' in data: update['scores'] = data['scores']
    if 'current_game' in data: update['current_game'] = data['current_game']
    if 'round' in data: update['round'] = data['round']
    if 'current_round' in data: update['current_round'] = data['current_round']
    if data.get('completed'): update['completed'] = True
    update['updated_at'] = datetime.now(timezone.utc).isoformat()
    koc_live_collection.update_one({'session_id': session_id}, {'$set': update})
    # Send push to all KOC players when game completes
    if data.get('completed') and data.get('winner_username'):
        winner_u = data['winner_username']
        winner_display = KOC_DISPLAY.get(winner_u, winner_u)
        for u in KOC_USERNAMES:
            send_push(u, {
                'title': f'{winner_display} wins the KOC session!',
                'body': 'All 8 games complete. Save as edition now.',
                'url': f'/kingofcards/live/{session_id}',
                'tag': f'koc-complete-{session_id}'
            })
    doc = koc_live_collection.find_one({'session_id': session_id}, {'_id': 0})
    return jsonify(doc)

@app.route('/api/koc/live/<session_id>/end', methods=['POST'])
def koc_live_end(session_id):
    username = current_user()
    if not username or username not in KOC_USERNAMES:
        return jsonify({'error': 'Unauthorized'}), 403
    if koc_live_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    koc_live_collection.update_one({'session_id': session_id}, {'$set': {'active': False}})
    return jsonify({'success': True})

@app.route('/api/koc/live/<session_id>/save_as_edition', methods=['POST'])
def koc_live_save_as_edition(session_id):
    """Save a completed live session as a new official edition."""
    username = current_user()
    if not username or username not in KOC_USERNAMES:
        return jsonify({'error': 'Unauthorized'}), 403
    if koc_live_collection is None or koc_tournaments_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    doc = koc_live_collection.find_one({'session_id': session_id}, {'_id': 0})
    if not doc:
        return jsonify({'error': 'Session not found'}), 404
    played_on = (request.json or {}).get('played_on', datetime.now().strftime('%Y-%m-%d'))
    scores = doc.get('scores', {})
    players = []
    for u, s in scores.items():
        r1m = s.get('r1_monopoly', 0) or 0
        r2m = s.get('r2_monopoly', 0) or 0
        r1b = s.get('r1_bluff', 0) or 0
        r2b = s.get('r2_bluff', 0) or 0
        r1s = s.get('r1_spoon', 0) or 0
        r2s = s.get('r2_spoon', 0) or 0
        r1u = s.get('r1_uno', 0) or 0
        r2u = s.get('r2_uno', 0) or 0
        mono = r1m + r2m or s.get('monopoly', 0) or 0
        blff = r1b + r2b or s.get('bluff', 0) or 0
        spn  = r1s + r2s or s.get('spoon', 0) or 0
        uno  = r1u + r2u or s.get('uno', 0) or 0
        total = mono + blff + spn + uno
        if total > 0:
            players.append({
                'username': u,
                'display_name': KOC_DISPLAY.get(u, u),
                'r1_monopoly': r1m, 'r2_monopoly': r2m, 'monopoly': mono,
                'r1_bluff':    r1b, 'r2_bluff':    r2b, 'bluff':    blff,
                'r1_spoon':    r1s, 'r2_spoon':    r2s, 'spoon':    spn,
                'r1_uno':      r1u, 'r2_uno':      r2u, 'uno':      uno,
                'total': total
            })
    if not players:
        return jsonify({'error': 'No scores recorded'}), 400
    def _wins(p):
        return sum(
            (1 if p.get('r1_'+g, 0) >= 2 else 0) +
            (1 if p.get('r2_'+g, 0) >= 2 else 0)
            for g in ('monopoly', 'bluff', 'spoon', 'uno')
        )
    winner = max(players, key=lambda p: (p.get('total',0), _wins(p)))
    last = koc_tournaments_collection.find_one(sort=[('edition', -1)])
    edition = (last['edition'] + 1) if last else 1
    koc_tournaments_collection.insert_one({
        'edition': edition, 'played_on': played_on,
        'winner_username': winner['username'],
        'players': players, 'added_by': username,
        'from_live': session_id,
        'created_at': datetime.now(timezone.utc).isoformat()
    })
    koc_live_collection.update_one({'session_id': session_id}, {'$set': {'active': False, 'saved_as_edition': edition}})
    # Notify all KOC players
    for u in KOC_USERNAMES:
        if u != username:
            send_push(u, {
                'title': '👑 KOC Edition ' + str(edition) + ' saved!',
                'body': f'{KOC_DISPLAY.get(winner["username"], winner["username"])} wins Edition {edition}!',
                'url': f'/kingofcards/{edition}',
                'tag': f'koc-edition-{edition}'
            })
    return jsonify({'edition': edition, 'winner': winner['username']}), 201

@app.route('/api/koc/game_heatmap')
def koc_game_heatmap():
    """Per-player per-game total points across all editions."""
    if koc_tournaments_collection is None:
        return jsonify({})
    tournaments = list(koc_tournaments_collection.find({}, {'_id': 0}))
    heatmap = {u: {'monopoly':0,'bluff':0,'spoon':0,'uno':0} for u in KOC_USERNAMES}
    for t in tournaments:
        for p in t.get('players', []):
            u = p.get('username','')
            if u in heatmap:
                for g in ('monopoly','bluff','spoon','uno'):
                    heatmap[u][g] += p.get(g, 0)
    result = []
    for u, scores in heatmap.items():
        result.append({'username': u, 'display': KOC_DISPLAY.get(u,u), **scores,
                       'best_game': max(scores, key=scores.get) if any(scores.values()) else None})
    return jsonify(result)

@app.route('/api/koc/push_status')
def koc_push_status():
    """Return which KOC players have push subscribed."""
    username = current_user()
    if not username or username not in KOC_USERNAMES:
        return jsonify({'error': 'Unauthorized'}), 403
    if push_subs_collection is None:
        return jsonify([])
    result = []
    for u in KOC_USERNAMES:
        subscribed = push_subs_collection.find_one({'username': u}) is not None
        result.append({'username': u, 'display': KOC_DISPLAY.get(u,u), 'subscribed': subscribed})
    return jsonify(result)

# KOC Group Chat collection
koc_chat_collection = None
try:
    if db is not None:
        koc_chat_collection = db.koc_chat
        koc_chat_collection.create_index('created_at')
except Exception as e:
    log.error('Failed to init koc_chat: %s', e)

@app.route('/api/koc/chat', methods=['GET'])
def koc_chat_get():
    username = current_user()
    if not username or username not in KOC_USERNAMES:
        return jsonify({'error': 'Unauthorized'}), 403
    if koc_chat_collection is None:
        return jsonify([])
    msgs = list(koc_chat_collection.find({}, {'_id': 0}).sort('created_at', -1).limit(50))
    msgs.reverse()
    return jsonify(msgs)

@app.route('/api/koc/chat', methods=['POST'])
def koc_chat_post():
    username = current_user()
    if not username or username not in KOC_USERNAMES:
        return jsonify({'error': 'Unauthorized'}), 403
    if koc_chat_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    text = (request.json or {}).get('text', '').strip()[:500]
    if not text:
        return jsonify({'error': 'Message required'}), 400
    msg = {
        'msg_id': str(uuid.uuid4())[:10],
        'username': username,
        'display': KOC_DISPLAY.get(username, username),
        'text': text,
        'created_at': datetime.now(timezone.utc).isoformat()
    }
    koc_chat_collection.insert_one(msg)
    # Push to all other KOC players
    for u in KOC_USERNAMES:
        if u != username:
            send_push(u, {
                'title': f'👑 KOC Chat — {KOC_DISPLAY.get(username,username).split()[0]}',
                'body': text[:80],
                'url': '/kingofcards',
                'tag': 'koc-chat'
            })
    return jsonify(msg), 201

@app.route('/api/koc/player/<target_username>')
def koc_player_stats(target_username):
    if koc_tournaments_collection is None or target_username not in KOC_USERNAMES:
        return jsonify({'is_koc_player': False})
    tournaments = list(koc_tournaments_collection.find({}, {'_id': 0}).sort('edition', 1))
    editions_played, total_points, game_wins, tournament_wins = 0, 0, 0, 0
    best_game_counts = {'monopoly': 0, 'bluff': 0, 'spoon': 0, 'uno': 0}
    history = []
    for t in tournaments:
        pd = next((p for p in t.get('players', []) if p.get('username') == target_username), None)
        if not pd:
            continue
        editions_played += 1
        pts = pd.get('total', 0)
        total_points += pts
        for g in ('monopoly', 'bluff', 'spoon', 'uno'):
            if pd.get('r1_' + g) is not None:
                if pd.get('r1_' + g, 0) >= 2: game_wins += 1; best_game_counts[g] += 1
                if pd.get('r2_' + g, 0) >= 2: game_wins += 1; best_game_counts[g] += 1
            else:
                if pd.get(g, 0) >= 2: game_wins += 1; best_game_counts[g] += 1
        if t.get('winner_username') == target_username:
            tournament_wins += 1
        history.append({
            'edition': t['edition'], 'played_on': t.get('played_on', ''),
            'points': pts, 'won': t.get('winner_username') == target_username
        })
    best_game = max(best_game_counts, key=best_game_counts.get) if any(best_game_counts.values()) else None
    return jsonify({
        'is_koc_player': True,
        'display_name': KOC_DISPLAY.get(target_username, target_username),
        'editions_played': editions_played, 'total_points': total_points,
        'game_wins': game_wins, 'tournament_wins': tournament_wins,
        'best_game': best_game, 'history': history
    })

# ===== KOC Reactions =====
koc_reactions_collection = None
try:
    if db is not None:
        koc_reactions_collection = db.koc_reactions
        koc_reactions_collection.create_index([('edition', 1), ('username', 1)], unique=True)
except Exception as e:
    log.error('Failed to init koc_reactions: %s', e)

@app.route('/api/koc/reactions', methods=['GET'])
def koc_reactions_get():
    edition = request.args.get('edition', type=int)
    if not edition:
        return jsonify({'counts': {}, 'mine': []})
    username = current_user()
    if koc_reactions_collection is None:
        return jsonify({'counts': {}, 'mine': []})
    docs = list(koc_reactions_collection.find({'edition': edition}, {'_id': 0}))
    counts = {}
    mine = []
    for d in docs:
        for r in d.get('reactions', []):
            counts[r] = counts.get(r, 0) + 1
        if username and d.get('username') == username:
            mine = d.get('reactions', [])
    return jsonify({'counts': counts, 'mine': mine})

@app.route('/api/koc/reactions', methods=['POST'])
def koc_reactions_post():
    username = current_user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    if koc_reactions_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    data = request.json or {}
    edition = data.get('edition')
    reaction = data.get('reaction')
    if not edition or not reaction:
        return jsonify({'error': 'edition and reaction required'}), 400
    doc = koc_reactions_collection.find_one({'edition': edition, 'username': username})
    reactions = doc.get('reactions', []) if doc else []
    if reaction in reactions:
        reactions.remove(reaction)
    else:
        reactions.append(reaction)
    koc_reactions_collection.update_one(
        {'edition': edition, 'username': username},
        {'$set': {'reactions': reactions}},
        upsert=True
    )
    return jsonify({'success': True})

# ===== KOC Comments =====
koc_comments_collection = None
try:
    if db is not None:
        koc_comments_collection = db.koc_comments
        koc_comments_collection.create_index([('edition', 1), ('created_at', 1)])
except Exception as e:
    log.error('Failed to init koc_comments: %s', e)

@app.route('/api/koc/comments', methods=['GET'])
def koc_comments_get():
    edition = request.args.get('edition', type=int)
    if not edition or koc_comments_collection is None:
        return jsonify([])
    comments = list(koc_comments_collection.find({'edition': edition}, {'_id': 0}).sort('created_at', 1))
    return jsonify(comments)

@app.route('/api/koc/comments', methods=['POST'])
def koc_comments_post():
    username = current_user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    if koc_comments_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    data = request.json or {}
    edition = data.get('edition')
    text = (data.get('text') or '').strip()[:500]
    if not edition or not text:
        return jsonify({'error': 'edition and text required'}), 400
    comment = {
        'edition': edition,
        'username': username,
        'text': text,
        'created_at': datetime.now(timezone.utc).isoformat()
    }
    koc_comments_collection.insert_one(comment)
    return jsonify(comment), 201

# ===== KOC Predict =====
koc_predict_collection = None
try:
    if db is not None:
        koc_predict_collection = db.koc_predictions
        koc_predict_collection.create_index('username', unique=True)
except Exception as e:
    log.error('Failed to init koc_predictions: %s', e)

@app.route('/api/koc/predict', methods=['GET'])
def koc_predict_get():
    if koc_predict_collection is None:
        return jsonify({'tally': {}})
    docs = list(koc_predict_collection.find({}, {'_id': 0, 'prediction': 1}))
    tally = {}
    for d in docs:
        p = d.get('prediction')
        if p:
            tally[p] = tally.get(p, 0) + 1
    return jsonify({'tally': tally})

@app.route('/api/koc/predict', methods=['POST'])
def koc_predict_post():
    username = current_user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    if koc_predict_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    prediction = (request.json or {}).get('prediction', '').strip()
    if not prediction:
        return jsonify({'error': 'prediction required'}), 400
    koc_predict_collection.update_one(
        {'username': username},
        {'$set': {'username': username, 'prediction': prediction}},
        upsert=True
    )
    docs = list(koc_predict_collection.find({}, {'_id': 0, 'prediction': 1}))
    tally = {}
    for d in docs:
        p = d.get('prediction')
        if p:
            tally[p] = tally.get(p, 0) + 1
    return jsonify({'tally': tally})

# ===== KOC Next Edition =====
koc_next_edition_collection = None
try:
    if db is not None:
        koc_next_edition_collection = db.koc_next_edition
except Exception as e:
    log.error('Failed to init koc_next_edition: %s', e)

@app.route('/api/koc/next_edition', methods=['GET'])
def koc_next_edition_get():
    if koc_next_edition_collection is None:
        return jsonify({})
    doc = koc_next_edition_collection.find_one({}, {'_id': 0})
    return jsonify(doc or {})

@app.route('/api/koc/next_edition', methods=['POST'])
def koc_next_edition_post():
    username = current_user()
    if not username or username not in KOC_USERNAMES:
        return jsonify({'error': 'Unauthorized'}), 403
    if koc_next_edition_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    data = request.json or {}
    action = data.get('action', 'propose')
    if action == 'propose':
        date_str = data.get('date', '').strip()
        if not date_str:
            return jsonify({'error': 'date required'}), 400
        # Reset: new proposal clears all votes
        doc = {
            'proposed_date': date_str,
            'proposed_by': username,
            'proposed_at': datetime.now(timezone.utc).isoformat(),
            'agreed': [username]
        }
        koc_next_edition_collection.replace_one({}, doc, upsert=True)
        # Notify others
        for u in KOC_USERNAMES:
            if u != username:
                send_push(u, {
                    'title': 'KOC Next Edition Proposed',
                    'body': f'{KOC_DISPLAY.get(username, username)} proposed {date_str} for the next edition. Agree in the app!',
                    'url': '/kingofcards',
                    'tag': 'koc-next-edition'
                })
        return jsonify(doc)
    elif action == 'agree':
        doc = koc_next_edition_collection.find_one({}, {'_id': 0})
        if not doc:
            return jsonify({'error': 'No date proposed yet'}), 404
        agreed = doc.get('agreed', [])
        if username not in agreed:
            agreed.append(username)
            koc_next_edition_collection.update_one({}, {'$set': {'agreed': agreed}})
        doc['agreed'] = agreed
        # If all 6 agreed, push everyone
        if set(agreed) >= KOC_USERNAMES:
            for u in KOC_USERNAMES:
                send_push(u, {
                    'title': 'KOC Date Confirmed!',
                    'body': f'All players agreed: next edition on {doc["proposed_date"]}',
                    'url': '/kingofcards',
                    'tag': 'koc-next-edition-confirmed'
                })
        return jsonify(doc)
    return jsonify({'error': 'Unknown action'}), 400

@app.route('/api/koc/next_edition', methods=['DELETE'])
def koc_next_edition_delete():
    username = current_user()
    if not username or username not in KOC_USERNAMES:
        return jsonify({'error': 'Unauthorized'}), 403
    if koc_next_edition_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    koc_next_edition_collection.delete_many({})
    return jsonify({'success': True})



# ===== KOC All-Time Records =====
@app.route('/api/koc/records')
def koc_records():
    if koc_tournaments_collection is None:
        return jsonify({})
    tournaments = list(koc_tournaments_collection.find({}, {'_id': 0}).sort('edition', 1))
    if not tournaments:
        return jsonify({})

    biggest_margin = {'margin': 0, 'winner': '', 'edition': 0}
    highest_score = {'score': 0, 'player': '', 'edition': 0}
    most_dominant = {'game_wins': 0, 'player': '', 'edition': 0}
    perfect_editions = []  # won all 8 games
    winless_streaks = {u: {'cur': 0, 'best': 0} for u in KOC_USERNAMES}

    for t in tournaments:
        players = sorted(t.get('players', []), key=lambda x: -x.get('total', 0))
        if len(players) >= 2:
            margin = players[0].get('total', 0) - players[1].get('total', 0)
            if margin > biggest_margin['margin']:
                biggest_margin = {'margin': margin, 'winner': KOC_DISPLAY.get(players[0].get('username',''), players[0].get('username','')), 'edition': t['edition']}
        for p in players:
            if p.get('total', 0) > highest_score['score']:
                highest_score = {'score': p['total'], 'player': KOC_DISPLAY.get(p.get('username',''), p.get('username','')), 'edition': t['edition']}
            gw = sum(1 for g in ('monopoly','bluff','spoon','uno')
                     for rk in ('r1_','r2_') if p.get(rk+g, p.get(g,0) if rk=='r1_' else 0) >= 2)
            if gw > most_dominant['game_wins']:
                most_dominant = {'game_wins': gw, 'player': KOC_DISPLAY.get(p.get('username',''), p.get('username','')), 'edition': t['edition']}
            if gw == 8:
                perfect_editions.append({'edition': t['edition'], 'player': KOC_DISPLAY.get(p.get('username',''), p.get('username',''))})
        winner_u = t.get('winner_username', '')
        for u in KOC_USERNAMES:
            if u == winner_u:
                winless_streaks[u]['cur'] = 0
            else:
                winless_streaks[u]['cur'] += 1
                if winless_streaks[u]['cur'] > winless_streaks[u]['best']:
                    winless_streaks[u]['best'] = winless_streaks[u]['cur']

    longest_winless = max(winless_streaks.items(), key=lambda x: x[1]['best'])

    return jsonify({
        'biggest_margin': biggest_margin,
        'highest_score': highest_score,
        'most_dominant': most_dominant,
        'perfect_editions': perfect_editions,
        'longest_winless': {'player': KOC_DISPLAY.get(longest_winless[0], longest_winless[0]), 'streak': longest_winless[1]['best']},
        'total_editions': len(tournaments),
        'first_champion': KOC_DISPLAY.get(tournaments[0].get('winner_username',''), '') if tournaments else ''
    })

# ===== KOC Form Guide (last 5 editions per player) =====
@app.route('/api/koc/form')
def koc_form():
    if koc_tournaments_collection is None:
        return jsonify({})
    tournaments = list(koc_tournaments_collection.find({}, {'_id': 0}).sort('edition', 1))
    last5 = tournaments[-5:] if len(tournaments) >= 5 else tournaments
    form = {u: [] for u in KOC_USERNAMES}
    for t in last5:
        winner_u = t.get('winner_username', '')
        players_sorted = sorted(t.get('players', []), key=lambda x: -x.get('total', 0))
        positions = {p.get('username',''): i+1 for i, p in enumerate(players_sorted)}
        for u in KOC_USERNAMES:
            pos = positions.get(u)
            if pos == 1: form[u].append('W')
            elif pos is not None: form[u].append('L')
    return jsonify(form)

# ===== KOC Rivalry =====
@app.route('/api/koc/rivalry')
def koc_rivalry():
    if koc_tournaments_collection is None:
        return jsonify({})
    tournaments = list(koc_tournaments_collection.find({}, {'_id': 0}))
    players = list(KOC_USERNAMES)
    best = {'score': -1, 'p1': '', 'p2': '', 'p1_wins': 0, 'p2_wins': 0, 'draws': 0}
    for i in range(len(players)):
        for j in range(i+1, len(players)):
            p1, p2 = players[i], players[j]
            p1w = p2w = draws = 0
            for t in tournaments:
                pd1 = next((p for p in t.get('players',[]) if p.get('username')==p1), None)
                pd2 = next((p for p in t.get('players',[]) if p.get('username')==p2), None)
                if not pd1 or not pd2: continue
                s1, s2 = pd1.get('total',0), pd2.get('total',0)
                if s1 > s2: p1w += 1
                elif s2 > s1: p2w += 1
                else: draws += 1
            total = p1w + p2w + draws
            if total == 0: continue
            # rivalry score = closeness (min wins / max wins) * total games
            closeness = min(p1w, p2w) / max(max(p1w, p2w), 1)
            score = closeness * total
            if score > best['score']:
                best = {'score': score, 'p1': p1, 'p2': p2,
                        'p1_wins': p1w, 'p2_wins': p2w, 'draws': draws,
                        'p1_display': KOC_DISPLAY.get(p1, p1), 'p2_display': KOC_DISPLAY.get(p2, p2)}
    return jsonify(best)

# ===== KOC Edition Story Generator =====
@app.route('/api/koc/story/<int:edition>')
def koc_story(edition):
    if koc_tournaments_collection is None:
        return jsonify({'story': ''})
    t = koc_tournaments_collection.find_one({'edition': edition}, {'_id': 0})
    if not t:
        return jsonify({'story': ''})
    players = sorted(t.get('players', []), key=lambda x: -x.get('total', 0))
    if not players:
        return jsonify({'story': ''})
    winner = players[0]
    runner_up = players[1] if len(players) > 1 else None
    wname = KOC_DISPLAY.get(winner.get('username',''), winner.get('username','')).split()[0]
    margin = winner.get('total',0) - (runner_up.get('total',0) if runner_up else 0)
    games = ['monopoly','bluff','spoon','uno']
    # find winner's best game
    best_game = max(games, key=lambda g: winner.get(g, 0))
    best_score = winner.get(best_game, 0)
    # find closest game across all players
    game_spreads = {}
    for g in games:
        vals = [p.get(g,0) for p in players]
        game_spreads[g] = max(vals) - min(vals)
    closest_game = min(game_spreads, key=game_spreads.get)
    # build story
    if margin == 0:
        opening = f"In a stunning Edition {edition}, {wname} claimed the crown in a tiebreaker after finishing level on points."
    elif margin == 1:
        rname = KOC_DISPLAY.get(runner_up.get('username',''), runner_up.get('username','')).split()[0] if runner_up else 'the field'
        opening = f"Edition {edition} went down to the wire — {wname} edged out {rname} by just 1 point to claim the title."
    else:
        opening = f"Edition {edition} belonged to {wname}, who dominated the competition and won by {margin} points."
    middle = f"{wname.capitalize()} was at their best in {best_game.capitalize()}, scoring {best_score} points in that game alone."
    if game_spreads[closest_game] <= 1:
        closing = f"The {closest_game.capitalize()} game was the tensest of the day, with players separated by just {game_spreads[closest_game]} point."
    else:
        closing = f"With {winner.get('total',0)} total points, {wname} added another chapter to their KOC legacy."
    story = f"{opening} {middle} {closing}"
    return jsonify({'story': story, 'edition': edition})

# ===== KOC Who Can Still Win =====
@app.route('/api/koc/who_can_win/<session_id>')
def koc_who_can_win(session_id):
    if koc_live_collection is None:
        return jsonify([])
    doc = koc_live_collection.find_one({'session_id': session_id}, {'_id': 0})
    if not doc:
        return jsonify([])
    scores = doc.get('scores', {})
    GAMES = ['monopoly','bluff','spoon','uno']
    # count completed games (both rounds done = r1+r2 > 0 for at least one player)
    completed = 0
    for g in GAMES:
        for rnd in [1,2]:
            k = f'r{rnd}_{g}'
            if any(scores.get(u,{}).get(k,0) > 0 for u in scores):
                completed += 1
    remaining_pts = (8 - completed) * 2  # max 2pts per remaining game
    current = {u: scores.get(u,{}).get('total',0) for u in scores}
    if not current:
        return jsonify([])
    leader_score = max(current.values())
    result = []
    for u, pts in current.items():
        can_win = (pts + remaining_pts) >= leader_score
        needed = max(0, leader_score - pts)
        result.append({
            'username': u,
            'display': KOC_DISPLAY.get(u, u),
            'current': pts,
            'can_win': can_win,
            'needed': needed,
            'remaining_pts': remaining_pts
        })
    result.sort(key=lambda x: -x['current'])
    return jsonify(result)

# ===== MULTIPLAYER GAMES =====
import random as _random
from collections import Counter

game_rooms_collection = None
game_stats_collection = None
try:
    if db is not None:
        game_rooms_collection = db.game_rooms
        game_rooms_collection.create_index('code', unique=True)
        game_stats_collection = db.game_stats
        game_stats_collection.create_index('username', unique=True)
except Exception as e:
    log.error('Failed to init game collections: %s', e)

IMPOSTOR_QUESTIONS = [
    "What's the most iconic scene in this movie?",
    "What's the mood of this movie?",
    "Name one word that describes this movie.",
    "What's one thing this movie is trying to say?",
    "What emotion should people feel when the credits roll?",
    "What scene do you think people remember most after watching it?",
    "If you could change ONE thing about this movie, what would it be?",
    "Give this movie a new tagline.",
    "What snack perfectly matches this movie?",
    "What emoji best represents this movie?"
]

def _get_movie_pair():
    """Return two similar-but-different popular movies for Movie Impostor."""
    pool = df.copy()
    pool['_votes_num'] = pd.to_numeric(pool['Votes'].astype(str).str.replace(',', ''), errors='coerce')
    pool['_rating_num'] = pd.to_numeric(pool['Rating'], errors='coerce')
    pool = pool[(pool['_votes_num'] >= 100000) & (pool['_rating_num'] >= 7.0)]
    if len(pool) < 2:
        pool = df.copy()
        pool['_rating_num'] = pd.to_numeric(pool['Rating'], errors='coerce')
        pool = pool[pool['_rating_num'] >= 7.0]
    if len(pool) < 2:
        pool = df.copy()
    m1 = pool.sample(1).iloc[0]
    genre1 = str(m1.get('Genre', '')).split(',')[0].strip().lower()
    same_genre = pool[
        (pool['Genre'].str.lower().str.contains(genre1, na=False)) &
        (pool['ID'] != m1['ID'])
    ]
    if same_genre.empty:
        same_genre = pool[pool['ID'] != m1['ID']]
    m2 = same_genre.sample(1).iloc[0]
    return (
        {'id': int(m1['ID']), 'title': m1['Movie Name'], 'genre': m1.get('Genre', ''), 'year': m1.get('Year', ''), 'rating': m1.get('Rating', '')},
        {'id': int(m2['ID']), 'title': m2['Movie Name'], 'genre': m2.get('Genre', ''), 'year': m2.get('Year', ''), 'rating': m2.get('Rating', '')}
    )

def _save_game_stats(username, game_type, won):
    if game_stats_collection is None or not username:
        return
    game_stats_collection.update_one(
        {'username': username},
        {'$inc': {f'{game_type}_played': 1, f'{game_type}_wins': 1 if won else 0}},
        upsert=True
    )

@app.route('/games')
def games_hub():
    return render_template('games.html', username=current_user())

@app.route('/games/music-guesser')
def music_guesser_page():
    username = current_user()
    if not username:
        return redirect('/login')
    return render_template('music_guesser.html',
        username=username,
        api_key_1=os.getenv('YOUTUBE_API_KEY_1',''),
        api_key_2=os.getenv('YOUTUBE_API_KEY_2',''),
        api_key_3=os.getenv('YOUTUBE_API_KEY_3',''),
        api_key_4=os.getenv('YOUTUBE_API_KEY_4',''),
        api_key_5=os.getenv('YOUTUBE_API_KEY_5',''))

@app.route('/games/music-survivor')
def music_survivor_page():
    username = current_user()
    if not username:
        return redirect('/login')
    return render_template('music_survivor.html', 
        username=username,
        api_key_1=os.getenv('YOUTUBE_API_KEY_1',''),
        api_key_2=os.getenv('YOUTUBE_API_KEY_2',''),
        api_key_3=os.getenv('YOUTUBE_API_KEY_3',''),
        api_key_4=os.getenv('YOUTUBE_API_KEY_4',''),
        api_key_5=os.getenv('YOUTUBE_API_KEY_5',''))

@app.route('/games/movie-impostor')
def movie_impostor_page():
    username = current_user()
    if not username:
        return redirect('/login')
    return render_template('movie_impostor.html', 
        username=username,
        api_key_1=os.getenv('YOUTUBE_API_KEY_1',''),
        api_key_2=os.getenv('YOUTUBE_API_KEY_2',''),
        api_key_3=os.getenv('YOUTUBE_API_KEY_3',''),
        api_key_4=os.getenv('YOUTUBE_API_KEY_4',''),
        api_key_5=os.getenv('YOUTUBE_API_KEY_5',''))

@app.route('/games/song-detective')
def song_detective_page():
    username = current_user()
    if not username:
        return redirect('/login')
    return render_template('song_detective.html', 
        username=username,
        api_key_1=os.getenv('YOUTUBE_API_KEY_1',''),
        api_key_2=os.getenv('YOUTUBE_API_KEY_2',''),
        api_key_3=os.getenv('YOUTUBE_API_KEY_3',''),
        api_key_4=os.getenv('YOUTUBE_API_KEY_4',''),
        api_key_5=os.getenv('YOUTUBE_API_KEY_5',''))

@app.route('/api/movie_data')
def get_movie_data():
    """Fetch full movie details from TMDB for Movie Impostor."""
    title = request.args.get('title', '').strip()
    year = request.args.get('year', '').strip()
    
    if not title:
        return jsonify({'error': 'Title required'}), 400
    
    token = os.getenv('TMDB_TOKEN', '')
    if not token:
        return jsonify({'error': 'TMDB token not configured'}), 500
    
    try:
        params = {'query': title, 'language': 'en-US'}
        if year:
            params['year'] = year
        
        resp = http_requests.get(
            'https://api.themoviedb.org/3/search/movie',
            headers={'Authorization': f'Bearer {token}'},
            params=params, timeout=5
        )
        resp.raise_for_status()
        data = resp.json()
        
        if data.get('results') and len(data['results']) > 0:
            movie = data['results'][0]
            detail_resp = http_requests.get(
                f"https://api.themoviedb.org/3/movie/{movie['id']}",
                headers={'Authorization': f'Bearer {token}'},
                params={'language': 'en-US'}, timeout=5
            )
            detail_resp.raise_for_status()
            full_data = detail_resp.json()
            return jsonify(full_data)
        
        return jsonify({'error': 'Movie not found'}), 404
    except Exception as e:
        log.error('Movie data error: %s', e)
        return jsonify({'error': str(e)}), 500

# ===== MOVIE BATTLE DATA =====
_battle_df = None
try:
    _bd = df.copy()
    _bd['_votes'] = pd.to_numeric(_bd['Votes'].astype(str).str.replace(',', ''), errors='coerce')
    _bd['_gross'] = pd.to_numeric(_bd['Gross'].astype(str).str.replace(',', '').str.replace('$', ''), errors='coerce')
    _bd['_rating'] = pd.to_numeric(_bd['Rating'], errors='coerce')
    _battle_df = _bd[
        (_bd['_gross'] > 1_000_000) &
        (_bd['_votes'] > 50_000) &
        (_bd['_rating'] >= 5)
    ].reset_index(drop=True)
except Exception as _e:
    log.error('Failed to build battle_df: %s', _e)
    _battle_df = df.copy()

@app.route('/api/games/battle/pair')
def battle_pair():
    """Return 2 random movies + the answer for the given question type."""
    if _battle_df is None or len(_battle_df) < 2:
        return jsonify({'error': 'Data unavailable'}), 500
    q_type = request.args.get('q', 'rating')  # rating | votes | gross
    sample = _battle_df.sample(2)
    rows = sample.to_dict(orient='records')
    movies = []
    for r in rows:
        movies.append({
            'id': int(r['ID']),
            'title': r['Movie Name'],
            'rating': float(r['_rating']) if pd.notna(r['_rating']) else 0,
            'votes': int(r['_votes']) if pd.notna(r['_votes']) else 0,
            'gross': float(r['_gross']) if pd.notna(r['_gross']) else 0,
        })
    if q_type == 'votes':
        winner = 0 if movies[0]['votes'] >= movies[1]['votes'] else 1
    elif q_type == 'gross':
        winner = 0 if movies[0]['gross'] >= movies[1]['gross'] else 1
    else:
        winner = 0 if movies[0]['rating'] >= movies[1]['rating'] else 1
    return jsonify({'movies': movies, 'winner': winner, 'q_type': q_type})

@app.route('/games/movie-battle')
def movie_battle_page():
    username = current_user()
    if not username:
        return redirect('/login')
    return render_template('movie_battle.html', username=username)

@app.route('/api/games/room', methods=['POST'])
def create_game_room():
    username = current_user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    if game_rooms_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    data = request.json or {}
    game_type = data.get('game_type', '')
    if game_type not in ('music_survivor', 'movie_impostor', 'song_detective', 'movie_battle', 'music_guesser'):
        return jsonify({'error': 'Invalid game type'}), 400
    code = ''.join([str(_random.randint(0, 9)) for _ in range(6)])
    room = {
        'code': code,
        'game_type': game_type,
        'host': username,
        'players': [{'username': username, 'ready': False, 'score': 0}],
        'state': 'lobby',
        'round': 0,
        'data': {},
        'created_at': datetime.now(timezone.utc).isoformat()
    }
    if game_type == 'movie_impostor':
        m1, m2 = _get_movie_pair()
        room['data']['movie_pair'] = [m1, m2]
        room['data']['questions'] = _random.sample(IMPOSTOR_QUESTIONS, min(5, len(IMPOSTOR_QUESTIONS)))
        room['data']['answers'] = {}
        room['data']['impostor_votes'] = {}
        room['data']['all_answered'] = False
    elif game_type == 'music_survivor':
        room['data']['songs'] = []
        room['data']['all_songs'] = []
        room['data']['votes'] = {}
        room['data']['preview_done'] = False
        room['data']['preview_duration'] = 30
        room['data']['current_index'] = -1
        room['data']['state'] = 'stopped'
        room['data']['position'] = 0
        room['data']['songs_per_player'] = 1
        room['data']['player_songs'] = {}
        room['data']['vote_records'] = {}  # Store all votes
    elif game_type == 'song_detective':
        room['data']['songs_per_player'] = 2
        room['data']['player_songs'] = {}
        room['data']['songs'] = []
        room['data']['all_songs'] = []
        room['data']['votes'] = {}
        room['data']['all_votes'] = {}  # Persistent vote storage across songs
        room['data']['current_index'] = -1
        room['data']['state'] = 'stopped'
        room['data']['position'] = 0
        room['data']['song_states'] = {}
        room['data']['_shuffled'] = False
        room['data']['vote_records'] = {}  # Store all votes for reveal
    elif game_type == 'music_guesser':
        room['data']['songs_per_player'] = 1
        room['data']['clip_duration'] = 5
        room['data']['player_songs'] = {}
        room['data']['songs'] = []
        room['data']['current_index'] = -1
        room['data']['phase'] = 'lobby'  # lobby|playing|buzzer|guessing|verifying|reveal|done
        room['data']['scores'] = {username: 0}
        room['data']['buzzer_winner'] = None
        room['data']['current_guess'] = None
        room['data']['attempt'] = 0  # 0=first, 1=retry
        room['data']['buzz_times'] = {}
    elif game_type == 'movie_battle':
        room['data']['scores'] = {username: 0}
        room['data']['round'] = 0
        room['data']['current_pair'] = None
        room['data']['answers'] = {}  # {username: 0 or 1}
        room['data']['revealed'] = False
    
    game_rooms_collection.insert_one(room)
    return jsonify({'code': code}), 201

@app.route('/api/games/room/<code>', methods=['GET'])
def get_game_room(code):
    username = current_user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    if game_rooms_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    room = game_rooms_collection.find_one({'code': code}, {'_id': 0})
    if not room:
        return jsonify({'error': 'Room not found'}), 404

    # Strip sensitive fields for movie_impostor unless game is finished
    if room.get('game_type') == 'movie_impostor' and room.get('state') != 'finished':
        data = room.get('data', {})
        # Always hide the impostor identity
        data.pop('impostor', None)
        # Hide impostor_votes until everyone has voted
        players = room.get('players', [])
        all_voted = len(data.get('impostor_votes', {})) >= len(players) and len(players) >= 2
        if not all_voted:
            data.pop('impostor_votes', None)
        # Hide the impostor's movie — only send the movie for this player
        pair = data.get('movie_pair')
        if pair and len(pair) == 2:
            # We can't know who the impostor is (we stripped it), so send both
            # movies only to the player who needs each one. Since we don't know
            # the impostor here, send only pair[0] to everyone — the frontend
            # already has the correct movie stored in myMovie from first reveal.
            # Sending pair[0] prevents pair[1] leaking via DevTools.
            data['movie_pair'] = [pair[0]]
        room['data'] = data

    return jsonify(room)

@app.route('/api/games/room/<code>/join', methods=['POST'])
def join_game_room(code):
    username = current_user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    if game_rooms_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    room = game_rooms_collection.find_one({'code': code})
    if not room:
        return jsonify({'error': 'Room not found'}), 404
    if room['state'] != 'lobby':
        return jsonify({'error': 'Game already started'}), 400
    if any(p['username'] == username for p in room['players']):
        return jsonify({'code': code})
    max_players = 2 if room.get('game_type') == 'movie_battle' else 8
    if len(room['players']) >= max_players:
        return jsonify({'error': 'Room full'}), 400
    game_rooms_collection.update_one(
        {'code': code},
        {'$push': {'players': {'username': username, 'ready': False, 'score': 0}}}
    )
    return jsonify({'code': code})

@app.route('/api/games/room/<code>/ready', methods=['POST'])
def ready_game_room(code):
    username = current_user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    if game_rooms_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    
    room = game_rooms_collection.find_one({'code': code}, {'_id': 0})
    if not room:
        return jsonify({'error': 'Room not found'}), 404
    
    if room['state'] != 'lobby':
        return jsonify({'started': True, 'already_started': True})
    
    # Mark this player as ready
    game_rooms_collection.update_one(
        {'code': code, 'players.username': username},
        {'$set': {'players.$.ready': True}}
    )
    
    room = game_rooms_collection.find_one({'code': code}, {'_id': 0})
    all_ready = all(p['ready'] for p in room['players']) and len(room['players']) >= 2
    
    if all_ready and room['state'] == 'lobby':
        # Transition to playing state based on game type
        update = {'state': 'playing', 'round': 1}
        
        if room['game_type'] == 'music_survivor':
            if 'songs_per_player' not in room.get('data', {}):
                update['data.songs_per_player'] = 1
            player_songs = {}
            for p in room['players']:
                player_songs[p['username']] = []
            update['data.player_songs'] = player_songs
            update['data.all_votes'] = {}
            # Shuffle songs so order doesn't reveal who submitted what
            songs = room.get('data', {}).get('songs', [])
            if songs:
                _random.shuffle(songs)
                update['data.songs'] = songs
                update['data.all_songs'] = songs
            
        elif room['game_type'] == 'movie_impostor':
            players = room['players']
            impostor = _random.choice(players)['username']
            update['data.impostor'] = impostor
            update['data.answers'] = {}
            update['data.impostor_votes'] = {}
            update['data.all_answered'] = False
            
        elif room['game_type'] == 'song_detective':
            # Don't transition song_detective here — submit_song handles the
            # state transition once all players have submitted their songs.
            # Just mark ready and return started: False so players keep waiting.
            return jsonify({'started': False})
        
        elif room['game_type'] == 'movie_battle':
            q_types = ['rating', 'votes', 'gross']
            q_type = q_types[0]
            pair = None
            if _battle_df is not None and len(_battle_df) >= 2:
                sample = _battle_df.sample(2)
                rows = sample.to_dict(orient='records')
                movies = []
                for r in rows:
                    movies.append({
                        'id': int(r['ID']),
                        'title': r['Movie Name'],
                        'rating': float(r['_rating']) if pd.notna(r['_rating']) else 0,
                        'votes': int(r['_votes']) if pd.notna(r['_votes']) else 0,
                        'gross': float(r['_gross']) if pd.notna(r['_gross']) else 0,
                    })
                winner_idx = 0 if movies[0]['rating'] >= movies[1]['rating'] else 1
                pair = {'movies': movies, 'winner': winner_idx, 'q_type': q_type}
            scores = {p['username']: 0 for p in room['players']}
            update['data.current_pair'] = pair
            update['data.scores'] = scores
            update['data.answers'] = {}
            update['data.revealed'] = False
            update['data.round'] = 1
        
        game_rooms_collection.update_one({'code': code}, {'$set': update})
        return jsonify({'started': True})
    
    return jsonify({'started': False})

@app.route('/api/games/room/<code>/action', methods=['POST'])
def game_action(code):
    username = current_user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    if game_rooms_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    data = request.json or {}
    action = data.get('action')
    room = game_rooms_collection.find_one({'code': code})
    if not room:
        return jsonify({'error': 'Room not found'}), 404

    game_type = room['game_type']
    update = {}

    # ============ SONG DETECTIVE ============
    if game_type == 'song_detective':
        
        if action == 'set_song_count':
            if room.get('host') != username:
                return jsonify({'error': 'Only host can set song count'}), 403
            count = int(data.get('count', 2))
            if count < 1 or count > 5:
                return jsonify({'error': 'Count must be between 1 and 5'}), 400
            update['data.songs_per_player'] = count
        
        elif action == 'shuffle_songs':
            songs = data.get('songs', [])
            if songs and room.get('host') == username:
                update['data.songs'] = songs
                update['data._shuffled'] = True
        
        elif action == 'submit_song':
            song = data.get('song', {})
            if not song or not song.get('youtube_query'):
                return jsonify({'error': 'Invalid song data'}), 400
            
            player_songs = room.get('data', {}).get('player_songs', {})
            if username not in player_songs:
                player_songs[username] = []
            
            songs_per_player = room.get('data', {}).get('songs_per_player', 2)
            if len(player_songs[username]) >= songs_per_player:
                return jsonify({'error': f'You can only submit {songs_per_player} songs'}), 400
            
            song_entry = {
                'id': str(uuid.uuid4())[:8],
                'youtube_query': song.get('youtube_query'),
                'title': song.get('title', 'Untitled')[:100],
                'artist': song.get('artist', 'Unknown')[:100],
                'thumbnail': song.get('thumbnail', ''),
                'submitter': username,
                'addedAt': datetime.now(timezone.utc).isoformat()
            }
            player_songs[username].append(song_entry)
            update['data.player_songs'] = player_songs
            
            all_players = [p['username'] for p in room['players']]
            all_submitted = all(
                len(player_songs.get(p, [])) >= room.get('data', {}).get('songs_per_player', 2)
                for p in all_players
            )
            if all_submitted and len(all_players) >= 2:
                all_songs = []
                for p, songs in player_songs.items():
                    all_songs.extend(songs)
                _random.shuffle(all_songs)
                update['data.songs'] = all_songs
                update['data.all_songs'] = all_songs
                update['state'] = 'playing'
                update['round'] = 1
                update['data.current_index'] = 0
                update['data.votes'] = {}
                update['data.all_votes'] = {}  # Persistent vote storage
                update['data.song_states'] = {}
                update['data.state'] = 'stopped'
                update['data.position'] = 0
                update['data._shuffled'] = True
                update['data.vote_records'] = {}
        
        elif action == 'vote':
            song_id = data.get('song_id')
            guess = data.get('guess')
            if not song_id or not guess:
                return jsonify({'error': 'Song ID and guess required'}), 400

            # Store votes persistently
            all_votes = room.get('data', {}).get('all_votes', {})
            if username not in all_votes:
                all_votes[username] = {}
            all_votes[username][song_id] = guess
            update['data.all_votes'] = all_votes

            # Also keep current votes for tracking
            votes = room.get('data', {}).get('votes', {})
            if username not in votes:
                votes[username] = {}
            votes[username][song_id] = guess
            update['data.votes'] = votes

            # Update song_states so all clients can see vote progress
            all_songs = room.get('data', {}).get('songs', [])
            if all_songs:
                current_index = room.get('data', {}).get('current_index', 0)
                if 0 <= current_index < len(all_songs):
                    current_song = all_songs[current_index]
                    current_song_id = current_song.get('id')
                    if current_song_id:
                        voted_count = sum(
                            1 for p in room['players']
                            if all_votes.get(p['username'], {}).get(current_song_id)
                        )
                        total_players = len(room['players'])
                        all_voted = voted_count >= total_players

                        song_states = room.get('data', {}).get('song_states', {})
                        song_states[current_song_id] = {
                            'voted_count': voted_count,
                            'total_players': total_players,
                            'all_voted': all_voted,
                            'votes': {p['username']: all_votes.get(p['username'], {}).get(current_song_id) for p in room['players']}
                        }
                        update['data.song_states'] = song_states
                        # NOTE: do NOT auto-advance here — next_song action is the
                        # only thing that should move current_index forward.

        elif action == 'play':
            if room.get('host') != username:
                return jsonify({'error': 'Only host can control playback'}), 403
            update['data.state'] = 'playing'
            update['data.position'] = data.get('position', 0)
        
        elif action == 'pause':
            if room.get('host') != username:
                return jsonify({'error': 'Only host can control playback'}), 403
            update['data.state'] = 'paused'
            update['data.position'] = data.get('position', 0)
        
        elif action == 'next_song':
            if room.get('host') != username:
                return jsonify({'error': 'Only host can advance songs'}), 403
            
            all_songs = room.get('data', {}).get('songs', [])
            current_index = room.get('data', {}).get('current_index', -1)
            next_index = current_index + 1
            
            if next_index >= len(all_songs):
                # All songs played - finish
                update['data.current_index'] = next_index
                update['state'] = 'finished'
                
                # Calculate scores from all_votes
                all_votes = room.get('data', {}).get('all_votes', {})
                scores = {}
                for p in room['players']:
                    scores[p['username']] = 0
                sub_map = {s['id']: s['submitter'] for s in all_songs}
                for voter, gs in all_votes.items():
                    for sid, guess_name in gs.items():
                        if sub_map.get(sid) == guess_name:
                            scores[voter] = scores.get(voter, 0) + 1
                update['data.scores'] = scores
                update['data.vote_records'] = all_votes  # Store all votes for reveal
                winner = max(scores, key=scores.get) if scores else None
                update['data.winner'] = winner
                for p in room['players']:
                    _save_game_stats(p['username'], 'song_detective', p['username'] == winner)
            else:
                update['data.current_index'] = next_index
                update['data.votes'] = {}
                update['data.song_states'] = {}
                update['data.position'] = 0
                update['data.state'] = 'playing'
        
        elif action == 'finish':
            if room.get('host') != username:
                return jsonify({'error': 'Only host can finish'}), 403
            all_songs = room.get('data', {}).get('songs', [])
            all_votes = room.get('data', {}).get('all_votes', {})
            scores = {}
            for p in room['players']:
                scores[p['username']] = 0
            sub_map = {s['id']: s['submitter'] for s in all_songs}
            for voter, gs in all_votes.items():
                for sid, guess_name in gs.items():
                    if sub_map.get(sid) == guess_name:
                        scores[voter] = scores.get(voter, 0) + 1
            update['data.scores'] = scores
            update['data.vote_records'] = all_votes  # Store all votes for reveal
            update['state'] = 'finished'
            winner = max(scores, key=scores.get) if scores else None
            update['data.winner'] = winner
            for p in room['players']:
                _save_game_stats(p['username'], 'song_detective', p['username'] == winner)

    # ============ MUSIC GUESSER ============
    elif game_type == 'music_guesser':

        if action == 'set_config':
            if room.get('host') != username:
                return jsonify({'error': 'Only host'}), 403
            spp = int(data.get('songs_per_player', 1))
            cd = int(data.get('clip_duration', 5))
            update['data.songs_per_player'] = max(1, min(5, spp))
            update['data.clip_duration'] = max(3, min(10, cd))

        elif action == 'submit_song':
            song = data.get('song', {})
            if not song.get('youtube_query'):
                return jsonify({'error': 'Invalid song'}), 400
            player_songs = room['data'].get('player_songs', {})
            spp = room['data'].get('songs_per_player', 1)
            if len(player_songs.get(username, [])) >= spp:
                return jsonify({'error': f'Max {spp} songs'}), 400
            entry = {
                'id': str(uuid.uuid4())[:8],
                'youtube_query': song['youtube_query'],
                'title': song.get('title', 'Untitled')[:100],
                'artist': song.get('artist', 'Unknown')[:100],
                'thumbnail': song.get('thumbnail', ''),
                'owner': username
            }
            if username not in player_songs:
                player_songs[username] = []
            player_songs[username].append(entry)
            update['data.player_songs'] = player_songs
            # Check if all submitted
            all_players = [p['username'] for p in room['players']]
            all_submitted = all(len(player_songs.get(p, [])) >= spp for p in all_players)
            if all_submitted and len(all_players) >= 2:
                all_songs = [s for songs in player_songs.values() for s in songs]
                _random.shuffle(all_songs)
                scores = {p: 0 for p in all_players}
                update['data.songs'] = all_songs
                update['data.scores'] = scores
                update['state'] = 'playing'
                update['data.phase'] = 'playing'
                update['data.current_index'] = 0
                update['data.buzzer_winner'] = None
                update['data.current_guess'] = None
                update['data.attempt'] = 0
                update['data.buzz_times'] = {}

        elif action == 'buzz':
            if room['data'].get('phase') != 'playing':
                return jsonify({'error': 'Not in playing phase'}), 400
            songs = room['data'].get('songs', [])
            idx = room['data'].get('current_index', 0)
            if idx >= len(songs):
                return jsonify({'error': 'No song'}), 400
            owner = songs[idx]['owner']
            if username == owner:
                return jsonify({'error': 'Cannot buzz your own song'}), 403
            # First buzz wins
            buzz_times = room['data'].get('buzz_times', {})
            if buzz_times:  # already buzzed
                return jsonify({'ok': False})
            buzz_times[username] = datetime.now(timezone.utc).isoformat()
            update['data.buzz_times'] = buzz_times
            update['data.buzzer_winner'] = username
            update['data.phase'] = 'guessing'

        elif action == 'submit_guess':
            if room['data'].get('phase') != 'guessing':
                return jsonify({'error': 'Not guessing phase'}), 400
            if room['data'].get('buzzer_winner') != username:
                return jsonify({'error': 'Not your turn'}), 403
            guess = data.get('guess', '').strip()[:200]
            if not guess:
                return jsonify({'error': 'Empty guess'}), 400
            update['data.current_guess'] = guess
            update['data.phase'] = 'verifying'

        elif action == 'verify':
            songs = room['data'].get('songs', [])
            idx = room['data'].get('current_index', 0)
            if idx >= len(songs):
                return jsonify({'error': 'No song'}), 400
            owner = songs[idx]['owner']
            if username != owner:
                return jsonify({'error': 'Only song owner can verify'}), 403
            if room['data'].get('phase') != 'verifying':
                return jsonify({'error': 'Not verifying phase'}), 400
            correct = bool(data.get('correct', False))
            if correct:
                guesser = room['data'].get('buzzer_winner')
                scores = room['data'].get('scores', {})
                scores[guesser] = scores.get(guesser, 0) + 1
                update['data.scores'] = scores
                update['data.phase'] = 'reveal'
                update['data.reveal_correct'] = True
            else:
                attempt = room['data'].get('attempt', 0)
                if attempt == 0:
                    # Give retry — replay song, open buzzer again
                    update['data.attempt'] = 1
                    update['data.phase'] = 'playing'
                    update['data.buzzer_winner'] = None
                    update['data.current_guess'] = None
                    update['data.buzz_times'] = {}
                else:
                    # Skip — reveal song
                    update['data.phase'] = 'reveal'
                    update['data.reveal_correct'] = False

        elif action == 'no_buzz':
            if room.get('host') != username:
                return jsonify({'error': 'Only host'}), 403
            if room['data'].get('phase') != 'playing':
                return jsonify({'error': 'Not in playing phase'}), 400
            force_skip = bool(data.get('force_skip', False))
            attempt = room['data'].get('attempt', 0)
            if not force_skip and attempt == 0:
                update['data.attempt'] = 1
                update['data.phase'] = 'playing'
                update['data.buzzer_winner'] = None
                update['data.current_guess'] = None
                update['data.buzz_times'] = {}
            else:
                update['data.phase'] = 'reveal'
                update['data.reveal_correct'] = False

        elif action == 'next_song':
            if room.get('host') != username:
                return jsonify({'error': 'Only host'}), 403
            songs = room['data'].get('songs', [])
            next_idx = room['data'].get('current_index', 0) + 1
            if next_idx >= len(songs):
                scores = room['data'].get('scores', {})
                if scores:
                    top = max(scores.values())
                    winners = [u for u, s in scores.items() if s == top]
                    winner = winners[0] if len(winners) == 1 else None
                else:
                    winner = None
                update['data.phase'] = 'done'
                update['state'] = 'finished'
                update['data.winner'] = winner
                update['data.tied'] = len([u for u, s in scores.items() if s == max(scores.values())]) > 1 if scores else False
                for p in room['players']:
                    _save_game_stats(p['username'], 'music_guesser', p['username'] == winner)
            else:
                update['data.current_index'] = next_idx
                update['data.phase'] = 'playing'
                update['data.buzzer_winner'] = None
                update['data.current_guess'] = None
                update['data.attempt'] = 0
                update['data.buzz_times'] = {}

    # ============ MUSIC SURVIVOR ============
    elif game_type == 'music_survivor':
        
        if action == 'set_duration':
            if room.get('host') != username:
                return jsonify({'error': 'Only host can set duration'}), 403
            duration = int(data.get('duration', 30))
            if duration < 5 or duration > 120:
                return jsonify({'error': 'Duration must be between 5 and 120 seconds'}), 400
            update['data.preview_duration'] = duration
        
        elif action == 'add_song':
            song = data.get('song', {})
            if song and song.get('id') and song.get('title'):
                songs = room.get('data', {}).get('songs', [])
                all_songs = room.get('data', {}).get('all_songs', [])
                player_songs = room.get('data', {}).get('player_songs', {})
                
                if username not in player_songs:
                    player_songs[username] = []
                player_songs[username].append(song['id'])
                update['data.player_songs'] = player_songs
                
                if not any(s.get('id') == song['id'] for s in songs):
                    entry = {
                        'id': song['id'],
                        'title': song.get('title', '')[:100],
                        'artist': song.get('artist', '')[:100],
                        'youtube_query': song.get('youtube_query', ''),
                        'thumbnail': song.get('thumbnail', ''),
                        'addedBy': song.get('addedBy', username)
                    }
                    songs.append(entry)
                    all_songs.append(entry)
                    update['data.songs'] = songs
                    update['data.all_songs'] = all_songs
        
        elif action == 'preview_done':
            update['data.preview_done'] = True
            update['data.votes'] = {}
            update['data.all_votes'] = {}
            update['round'] = 1
            update['data.current_index'] = 0
            update['data.state'] = 'stopped'
            update['data.position'] = 0
        
        elif action == 'play':
            if room.get('host') != username:
                return jsonify({'error': 'Only host can control playback'}), 403
            update['data.state'] = 'playing'
            update['data.position'] = data.get('position', 0)
        
        elif action == 'pause':
            if room.get('host') != username:
                return jsonify({'error': 'Only host can control playback'}), 403
            update['data.state'] = 'paused'
            update['data.position'] = data.get('position', 0)
        
        elif action == 'vote':
            # Only allow votes during the voting phase (preview_done must be True)
            if not room.get('data', {}).get('preview_done', False):
                return jsonify({'error': 'Voting not open yet'}), 400
            song_id = data.get('song_id', '')
            all_votes = room.get('data', {}).get('all_votes', {})
            all_votes[username] = song_id
            update['data.all_votes'] = all_votes
            
            votes = room.get('data', {}).get('votes', {})
            votes[username] = song_id
            update['data.votes'] = votes
            
            # Check if all players voted
            if len(votes) >= len(room['players']):
                # Find the most voted song
                vote_counts = Counter(votes.values())
                max_votes = max(vote_counts.values())
                eliminated_candidates = [sid for sid, count in vote_counts.items() if count == max_votes]
                
                is_tiebreak = len(eliminated_candidates) > 1
                tiebreak_method = None
                
                if is_tiebreak:
                    # Fetch view counts for tied songs and eliminate least viewed
                    all_songs_data = room.get('data', {}).get('songs', [])
                    tied_songs = [s for s in all_songs_data if s.get('id') in eliminated_candidates]
                    video_ids = [s['youtube_query'] for s in tied_songs if s.get('youtube_query')]
                    
                    view_counts = {}
                    if video_ids:
                        for key_name in ['YOUTUBE_API_KEY_1','YOUTUBE_API_KEY_2','YOUTUBE_API_KEY_3','YOUTUBE_API_KEY_4','YOUTUBE_API_KEY_5']:
                            api_key = os.getenv(key_name, '')
                            if not api_key:
                                continue
                            try:
                                resp = http_requests.get(
                                    'https://www.googleapis.com/youtube/v3/videos',
                                    params={'part': 'statistics', 'id': ','.join(video_ids), 'key': api_key},
                                    timeout=5
                                )
                                if resp.status_code == 200:
                                    for item in resp.json().get('items', []):
                                        vid = item['id']
                                        vc = int(item.get('statistics', {}).get('viewCount', 0))
                                        view_counts[vid] = vc
                                    break
                            except Exception:
                                continue
                    
                    if view_counts:
                        # Map song id -> view count, eliminate lowest
                        song_views = {}
                        for s in tied_songs:
                            vid = s.get('youtube_query', '')
                            song_views[s['id']] = view_counts.get(vid, 0)
                        eliminated = min(song_views, key=song_views.get)
                        tiebreak_method = 'views'
                    else:
                        # Fallback to random if API failed
                        eliminated = _random.choice(eliminated_candidates)
                        tiebreak_method = 'random'
                else:
                    eliminated = eliminated_candidates[0] if eliminated_candidates else None
                
                if eliminated:
                    songs = room.get('data', {}).get('songs', [])
                    songs = [s for s in songs if s.get('id') != eliminated]
                    update['data.songs'] = songs
                    update['data.votes'] = {}
                    update['data.all_votes'] = {}
                    update['data.eliminated'] = eliminated
                    update['data.is_tiebreak'] = is_tiebreak
                    update['data.tiebreak_method'] = tiebreak_method
                    update['round'] = room.get('round', 1) + 1
                    update['data.current_index'] = 0
                    update['data.state'] = 'stopped'
                    update['data.position'] = 0
                    update['data.vote_records'] = votes
                    
                    if len(songs) <= 1:
                        update['state'] = 'finished'
                        update['data.winner_song'] = songs[0] if songs else None
                        for p in room['players']:
                            _save_game_stats(p['username'], 'music_survivor', False)

    # ============ MOVIE IMPOSTOR ============
    elif game_type == 'movie_impostor':
        
        if action == 'answer':
            answers = room.get('data', {}).get('answers', {})
            if username not in answers:
                answers[username] = []
            answers[username].append({
                'q': data.get('question_idx'),
                'a': data.get('answer', '')[:200]
            })
            update['data.answers'] = answers
            
            all_players = [p['username'] for p in room['players']]
            question_count = len(room.get('data', {}).get('questions', []))
            all_answered = all(
                len(answers.get(p, [])) >= question_count
                for p in all_players
            )
            if all_answered and question_count > 0:
                update['data.all_answered'] = True
        
        elif action == 'vote_impostor':
            votes = room.get('data', {}).get('impostor_votes', {})
            votes[username] = data.get('suspect', '')
            update['data.impostor_votes'] = votes
            
            all_players = [p['username'] for p in room['players']]
            all_voted = all(
                votes.get(p) is not None
                for p in all_players
            )
            
            if all_voted and len(all_players) >= 2:
                accused = Counter(votes.values()).most_common(1)[0][0]
                real_impostor = room.get('data', {}).get('impostor', '')
                correct = accused == real_impostor
                update['state'] = 'finished'
                update['data.accused'] = accused
                update['data.correct_guess'] = correct
                update['data.vote_counts'] = dict(Counter(votes.values()))
                update['data.vote_records'] = votes
                for p in room['players']:
                    won = (p['username'] != real_impostor and correct) or (p['username'] == real_impostor and not correct)
                    _save_game_stats(p['username'], 'movie_impostor', won)

    # ============ MOVIE BATTLE ============
    elif game_type == 'movie_battle':

        if action == 'answer':
            choice = data.get('choice')  # 0 or 1
            if choice not in (0, 1):
                return jsonify({'error': 'choice must be 0 or 1'}), 400
            answers = room.get('data', {}).get('answers', {})
            answers[username] = choice
            update['data.answers'] = answers

            # If both players answered, score and reveal
            all_players = [p['username'] for p in room['players']]
            if len(answers) >= len(all_players) and len(all_players) == 2:
                winner_idx = room.get('data', {}).get('current_pair', {}).get('winner', -1)
                scores = room.get('data', {}).get('scores', {})
                for p in all_players:
                    if answers.get(p) == winner_idx:
                        scores[p] = scores.get(p, 0) + 1
                update['data.scores'] = scores
                update['data.revealed'] = True

        elif action == 'finish':
            if room.get('host') != username:
                return jsonify({'error': 'Only host can finish'}), 403
            scores = room.get('data', {}).get('scores', {})
            winner = max(scores, key=scores.get) if scores else None
            update['state'] = 'finished'
            update['data.winner'] = winner
            for p in room['players']:
                _save_game_stats(p['username'], 'movie_battle', p['username'] == winner)

        elif action == 'next_round':
            if room.get('host') != username:
                return jsonify({'error': 'Only host can advance'}), 403
            q_types = ['rating', 'votes', 'gross']
            rnd = room.get('data', {}).get('round', 0)
            q_type = q_types[rnd % 3]
            # Pick a new pair server-side
            if _battle_df is not None and len(_battle_df) >= 2:
                sample = _battle_df.sample(2)
                rows = sample.to_dict(orient='records')
                movies = []
                for r in rows:
                    movies.append({
                        'id': int(r['ID']),
                        'title': r['Movie Name'],
                        'rating': float(r['_rating']) if pd.notna(r['_rating']) else 0,
                        'votes': int(r['_votes']) if pd.notna(r['_votes']) else 0,
                        'gross': float(r['_gross']) if pd.notna(r['_gross']) else 0,
                    })
                if q_type == 'votes':
                    winner_idx = 0 if movies[0]['votes'] >= movies[1]['votes'] else 1
                elif q_type == 'gross':
                    winner_idx = 0 if movies[0]['gross'] >= movies[1]['gross'] else 1
                else:
                    winner_idx = 0 if movies[0]['rating'] >= movies[1]['rating'] else 1
                pair = {'movies': movies, 'winner': winner_idx, 'q_type': q_type}
            else:
                pair = None
            update['data.current_pair'] = pair
            update['data.answers'] = {}
            update['data.revealed'] = False
            update['data.round'] = rnd + 1

    if update:
        game_rooms_collection.update_one({'code': code}, {'$set': update})
    
    room = game_rooms_collection.find_one({'code': code}, {'_id': 0})
    return jsonify(room)

@app.route('/api/games/room/<code>/leave', methods=['POST'])
def leave_game_room(code):
    username = current_user()
    if not username or game_rooms_collection is None:
        return jsonify({'error': 'Unauthorized'}), 401
    game_rooms_collection.update_one(
        {'code': code},
        {'$pull': {'players': {'username': username}}}
    )
    return jsonify({'success': True})

@app.route('/api/games/stats/<target_username>')
def get_game_stats(target_username):
    if game_stats_collection is None:
        return jsonify({})
    doc = game_stats_collection.find_one({'username': target_username}, {'_id': 0})
    return jsonify(doc or {})

# ===== Watch Links =====
watch_links_collection = None
try:
    if db is not None:
        watch_links_collection = db.watch_links
        watch_links_collection.create_index([('type', 1), ('name', 1)], unique=True)
except Exception as e:
    log.error('Failed to init watch_links: %s', e)

@app.route('/api/watch_links', methods=['GET'])
def get_watch_links():
    if watch_links_collection is None:
        return jsonify([])
    link_type = request.args.get('type', 'movie')
    links = list(watch_links_collection.find({'type': link_type}, {'_id': 0}).sort('order', 1))
    return jsonify(links)

@app.route('/api/watch_links', methods=['POST'])
def add_watch_link():
    if current_user() != 'abhinav':
        return jsonify({'error': 'Unauthorized'}), 403
    if watch_links_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    data = request.json or {}
    name = data.get('name', '').strip()
    url_template = data.get('url_template', '').strip()
    link_type = request.args.get('type', data.get('type', 'movie'))
    if not name or not url_template:
        return jsonify({'error': 'name and url_template required'}), 400
    count = watch_links_collection.count_documents({'type': link_type})
    watch_links_collection.update_one(
        {'name': name, 'type': link_type},
        {'$set': {'name': name, 'url_template': url_template, 'type': link_type, 'order': count}},
        upsert=True
    )
    return jsonify({'success': True}), 201

@app.route('/api/watch_links/<name>', methods=['PUT'])
def update_watch_link(name):
    if current_user() != 'abhinav':
        return jsonify({'error': 'Unauthorized'}), 403
    if watch_links_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    link_type = request.args.get('type', 'movie')
    data = request.json or {}
    update = {}
    if 'url_template' in data:
        update['url_template'] = data['url_template'].strip()
    if 'name' in data:
        update['name'] = data['name'].strip()
    if not update:
        return jsonify({'error': 'Nothing to update'}), 400
    watch_links_collection.update_one({'name': name, 'type': link_type}, {'$set': update})
    return jsonify({'success': True})

@app.route('/api/watch_links/<name>', methods=['DELETE'])
def delete_watch_link(name):
    if current_user() != 'abhinav':
        return jsonify({'error': 'Unauthorized'}), 403
    if watch_links_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    link_type = request.args.get('type', 'movie')
    watch_links_collection.delete_one({'name': name, 'type': link_type})
    return jsonify({'success': True})

# ===== GAME CLEANUP =====
@app.route('/api/games/cleanup', methods=['POST'])
def cleanup_game_rooms():
    username = current_user()
    if not username or username not in KOC_USERNAMES:
        return jsonify({'error': 'Unauthorized'}), 403
    if game_rooms_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    result = game_rooms_collection.delete_many({
        'created_at': {'$lt': cutoff},
        'state': 'finished'
    })
    return jsonify({'deleted': result.deleted_count})

# ===== SOCKET.IO EVENTS FOR GAMES =====

# Store active game rooms for socket sync
game_socket_rooms = {}

@socketio.on('game_join')
def on_game_join(data):
    """Join a game room for real-time sync."""
    room_code = data.get('room_code')
    username = session.get('username')
    
    if not room_code or not username:
        return
    
    if game_rooms_collection is None:
        return
    
    room = game_rooms_collection.find_one({'code': room_code}, {'_id': 0})
    if not room:
        return
    
    if username not in [p['username'] for p in room.get('players', [])]:
        return
    
    join_room(room_code)
    
    game_socket_rooms[room_code] = game_socket_rooms.get(room_code, set())
    game_socket_rooms[room_code].add(username)
    
    game_type = room.get('game_type')
    emit_room = room
    
    # For movie_impostor, always send the full data (including impostor and both movies)
    # The frontend handles filtering based on the user's role
    if game_type == 'movie_impostor' and room.get('state') != 'finished':
        import copy
        emit_room = copy.deepcopy(room)
        # Don't strip anything - send the full data so the frontend can determine
        # which movie to show based on the user's username
    
    emit('game_state', {
        'room': emit_room,
        'game_type': game_type
    }, to=request.sid)


@socketio.on('request_full_state')
def on_request_full_state(data):
    """Send the full room state to the requesting client."""
    room_code = data.get('room_code')
    if not room_code:
        return
    
    if game_rooms_collection is None:
        return
    
    room = game_rooms_collection.find_one({'code': room_code}, {'_id': 0})
    if not room:
        return
    
    # Send the full room data (not stripped)
    emit('full_state', {'room': room}, to=request.sid)


# ===== MOVIE IMPOSTOR SOCKET EVENTS =====

@socketio.on('movie_reveal')
def on_movie_reveal(data):
    room_code = data.get('room_code')
    username = data.get('username')
    
    if not room_code or not username:
        return
    
    emit('movie_reveal', {
        'username': username,
        'room_code': room_code
    }, to=room_code, include_self=False)


@socketio.on('answers_submitted')
def on_answers_submitted(data):
    room_code = data.get('room_code')
    username = data.get('username')
    
    if not room_code or not username:
        return
    
    if game_rooms_collection is not None:
        room = game_rooms_collection.find_one({'code': room_code}, {'_id': 0})
        if room:
            emit('answers_submitted', {
                'username': username,
                'room': room
            }, to=room_code, include_self=False)


@socketio.on('vote_cast')
def on_vote_cast(data):
    room_code = data.get('room_code')
    username = data.get('username')
    room = data.get('room')
    
    if not room_code or not username:
        return
    
    if not room and game_rooms_collection is not None:
        room = game_rooms_collection.find_one({'code': room_code}, {'_id': 0})
    
    emit('vote_cast', {
        'username': username,
        'room': room
    }, to=room_code, include_self=False)


# ===== SONG DETECTIVE & MUSIC SURVIVOR SOCKET EVENTS =====

@socketio.on('game_play')
def on_game_play(data):
    room_code = data.get('room_code')
    username = session.get('username')
    position = data.get('position', 0)
    
    if not room_code or not username:
        return
    
    if game_rooms_collection is None:
        return
    
    room = game_rooms_collection.find_one({'code': room_code}, {'_id': 0})
    if not room:
        return
    
    if room.get('host') != username:
        return
    
    game_rooms_collection.update_one(
        {'code': room_code},
        {'$set': {
            'data.state': 'playing',
            'data.position': position
        }}
    )
    
    emit('game_play', {
        'position': position,
        'by': username
    }, to=room_code)


@socketio.on('game_pause')
def on_game_pause(data):
    room_code = data.get('room_code')
    username = session.get('username')
    position = data.get('position', 0)
    
    if not room_code or not username:
        return
    
    if game_rooms_collection is None:
        return
    
    room = game_rooms_collection.find_one({'code': room_code}, {'_id': 0})
    if not room:
        return
    
    if room.get('host') != username:
        return
    
    game_rooms_collection.update_one(
        {'code': room_code},
        {'$set': {
            'data.state': 'paused',
            'data.position': position
        }}
    )
    
    emit('game_pause', {
        'position': position,
        'by': username
    }, to=room_code)


@socketio.on('game_next_song')
def on_game_next_song(data):
    """Broadcast the next song index to all players in the room."""
    room_code = data.get('room_code')
    username = session.get('username')

    if not room_code or not username:
        return

    if game_rooms_collection is None:
        return

    room = game_rooms_collection.find_one({'code': room_code}, {'_id': 0})
    if not room:
        return

    if room.get('host') != username:
        return

    index = data.get('index')
    if index is None or index < 0:
        return

    emit('game_next_song', {
        'index': index,
        'by': username
    }, to=room_code, include_self=False)


@socketio.on('game_sync_request')
def on_game_sync_request(data):
    room_code = data.get('room_code')
    username = session.get('username')
    
    if not room_code or not username:
        return
    
    if game_rooms_collection is None:
        return
    
    room = game_rooms_collection.find_one({'code': room_code}, {'_id': 0})
    if not room:
        return
    
    emit('game_state', {
        'room': room,
        'game_type': room.get('game_type')
    }, to=request.sid)


@socketio.on('game_leave')
def on_game_leave(data):
    room_code = data.get('room_code')
    username = session.get('username')
    
    if not room_code or not username:
        return
    
    leave_room(room_code)
    
    if room_code in game_socket_rooms:
        game_socket_rooms[room_code].discard(username)
        if not game_socket_rooms[room_code]:
            del game_socket_rooms[room_code]


# ===== GARTIC PHONE =====
gartic_collection = None
gartic_drawings_collection = None
try:
    if db is not None:
        gartic_collection = db.gartic_rooms
        gartic_collection.create_index('code', unique=True)
        gartic_collection.create_index('created_at', expireAfterSeconds=86400)  # Auto-delete after 24h
        gartic_drawings_collection = db.gartic_drawings
        gartic_drawings_collection.create_index('created_at', expireAfterSeconds=86400)
except Exception as e:
    log.error('Failed to init gartic_rooms: %s', e)

@app.route('/games/gartic-phone')
def gartic_phone_page():
    username = current_user()
    if not username:
        return redirect('/login')
    return render_template('gartic_phone.html', username=username)

@app.route('/api/gartic/room', methods=['POST'])
def gartic_create_room():
    username = current_user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    if gartic_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    
    # Generate unique code with retry
    for _ in range(10):
        code = ''.join([str(_random.randint(0, 9)) for _ in range(6)])
        if not gartic_collection.find_one({'code': code}):
            break
    else:
        return jsonify({'error': 'Failed to generate unique code'}), 500
    
    req = request.json or {}
    time_limit = int(req.get('time_limit', 0))
    if time_limit not in (0, 30, 60, 90, 120):
        time_limit = 0
    room = {
        'code': code,
        'host': username,
        'players': [username],
        'state': 'lobby',
        'round': 0,
        'total_rounds': 0,
        'time_limit': time_limit,
        'chains': {},
        'submissions': {},
        'assignments': {},
        'reactions': {},
        'reveal_chain_idx': 0,
        'reveal_step_idx': 0,
        'round_started_at': None,
        'created_at': datetime.now(timezone.utc)
    }
    gartic_collection.insert_one(room)
    return jsonify({'code': code}), 201

@app.route('/api/gartic/room/<code>', methods=['GET'])
def gartic_get_room(code):
    username = current_user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    if gartic_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    room = gartic_collection.find_one({'code': code}, {'_id': 0})
    if not room:
        return jsonify({'error': 'Room not found'}), 404
    
    if room['state'] in ('writing', 'drawing'):
        room = _gartic_strip_for_player(room, username)
    return jsonify(room)

def _gartic_strip_for_player(room, username):
    """Remove sensitive data, only show what this player should see."""
    assignment = room.get('assignments', {}).get(username)
    if assignment:
        chain = room.get('chains', {}).get(assignment, [])
        last_entry = chain[-1] if chain else None
        room['my_prompt'] = last_entry
        room['my_chain_owner'] = assignment
    room.pop('chains', None)
    room.pop('submissions', None)
    room.pop('assignments', None)
    return room

@app.route('/api/gartic/room/<code>/join', methods=['POST'])
def gartic_join_room(code):
    username = current_user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    if gartic_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    room = gartic_collection.find_one({'code': code})
    if not room:
        return jsonify({'error': 'Room not found'}), 404
    if room['state'] != 'lobby':
        return jsonify({'error': 'Game already started'}), 400
    if username in room['players']:
        return jsonify({'code': code})
    if len(room['players']) >= 8:
        return jsonify({'error': 'Room full (max 8)'}), 400
    gartic_collection.update_one({'code': code}, {'$push': {'players': username}})
    socketio.emit('gartic_player_joined', {'username': username}, to=code)
    return jsonify({'code': code})

@app.route('/api/gartic/room/<code>/start', methods=['POST'])
def gartic_start(code):
    username = current_user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    if gartic_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    room = gartic_collection.find_one({'code': code})
    if not room:
        return jsonify({'error': 'Room not found'}), 404
    if room['host'] != username:
        return jsonify({'error': 'Only host can start'}), 403
    players = room['players']
    if len(players) < 2:
        return jsonify({'error': 'Need at least 2 players'}), 400
    chains = {p: [] for p in players}
    assignments = {p: p for p in players}
    total_rounds = len(players)
    gartic_collection.update_one({'code': code}, {'$set': {
        'state': 'writing',
        'round': 1,
        'total_rounds': total_rounds,
        'chains': chains,
        'submissions': {},
        'assignments': assignments,
        'reactions': {},
        'reveal_chain_idx': 0,
        'reveal_step_idx': -1,
        'round_started_at': datetime.now(timezone.utc).isoformat()
    }})
    socketio.emit('gartic_refresh', {}, to=code)
    return jsonify({'started': True})

@app.route('/api/gartic/room/<code>/submit', methods=['POST'])
def gartic_submit(code):
    username = current_user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    if gartic_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    room = gartic_collection.find_one({'code': code})
    if not room:
        return jsonify({'error': 'Room not found'}), 404
    if username not in room['players']:
        return jsonify({'error': 'Not in this room'}), 403
    if room['state'] not in ('writing', 'drawing'):
        return jsonify({'error': 'Not accepting submissions'}), 400

    data = request.json or {}
    content = data.get('content', '').strip()
    if not content:
        return jsonify({'error': 'Content required'}), 400
    if room['state'] == 'drawing' and len(content) > 1500000:
        return jsonify({'error': 'Drawing too large'}), 400
    if room['state'] == 'writing' and len(content) > 300:
        return jsonify({'error': 'Text too long (max 300 chars)'}), 400
    # Store drawing data separately to keep room document small
    if room['state'] == 'drawing' and gartic_drawings_collection is not None:
        draw_result = gartic_drawings_collection.insert_one({
            'data': content,
            'created_at': datetime.now(timezone.utc)
        })
        content = 'drawing:' + str(draw_result.inserted_id)

    result = gartic_collection.update_one(
        {'code': code, f'submissions.{username}': {'$exists': False}},
        {'$set': {f'submissions.{username}': content}}
    )
    if result.modified_count == 0:
        room = gartic_collection.find_one({'code': code}, {'_id': 0})
        if room['state'] in ('writing', 'drawing'):
            room = _gartic_strip_for_player(room, username)
        return jsonify(room)
    socketio.emit('gartic_someone_submitted', {'username': username}, to=code)
    room = gartic_collection.find_one({'code': code})
    players = room['players']
    submissions = room.get('submissions', {})
    if all(p in submissions for p in players):
        chains = room.get('chains', {})
        assignments = room.get('assignments', {})
        entry_type = 'text' if room['state'] == 'writing' else 'drawing'

        for player, content_val in submissions.items():
            chain_owner = assignments.get(player, player)
            if chain_owner in chains:
                chains[chain_owner].append({
                    'type': entry_type,
                    'content': content_val,
                    'author': player
                })

        current_round = room['round']
        total_rounds = room['total_rounds']

        if current_round >= total_rounds:
            gartic_collection.update_one({'code': code}, {'$set': {
                'chains': chains,
                'submissions': {},
                'state': 'reveal',
                'reveal_chain_idx': 0,
                'reveal_step_idx': -1,
                'round_started_at': None
            }})
        else:
            new_assignments = _gartic_rotate(players, assignments)
            next_state = 'drawing' if room['state'] == 'writing' else 'writing'
            gartic_collection.update_one({'code': code}, {'$set': {
                'chains': chains,
                'submissions': {},
                'assignments': new_assignments,
                'state': next_state,
                'round': current_round + 1,
                'round_started_at': datetime.now(timezone.utc).isoformat()
            }})
        socketio.emit('gartic_refresh', {}, to=code)

    room = gartic_collection.find_one({'code': code}, {'_id': 0})
    if room['state'] in ('writing', 'drawing'):
        room = _gartic_strip_for_player(room, username)
    return jsonify(room)

def _gartic_rotate(players, assignments):
    """Rotate each player's assignment to the next player's chain."""
    n = len(players)
    new_assignments = {}
    for player in players:
        current_chain = assignments.get(player, player)
        try:
            current_idx = players.index(current_chain)
        except ValueError:
            try:
                current_idx = players.index(player)
            except ValueError:
                current_idx = 0
        next_chain_owner = players[(current_idx + 1) % n]
        new_assignments[player] = next_chain_owner
    return new_assignments

@app.route('/api/gartic/drawing/<drawing_id>', methods=['GET'])
def gartic_get_drawing(drawing_id):
    username = current_user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    if gartic_drawings_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    try:
        from bson import ObjectId
        doc = gartic_drawings_collection.find_one({'_id': ObjectId(drawing_id)})
    except Exception:
        return jsonify({'error': 'Invalid id'}), 400
    if not doc:
        return jsonify({'error': 'Not found'}), 404
    data_url = doc['data']
    # Return as image directly so browser can cache it
    if data_url.startswith('data:image/jpeg;base64,'):
        import base64
        img_bytes = base64.b64decode(data_url.split(',', 1)[1])
        from flask import Response
        return Response(img_bytes, mimetype='image/jpeg',
                        headers={'Cache-Control': 'public, max-age=86400'})
    return jsonify({'error': 'Invalid drawing data'}), 400




@app.route('/api/gartic/room/<code>/react', methods=['POST'])
def gartic_react(code):
    username = current_user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    if gartic_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    room = gartic_collection.find_one({'code': code})
    if not room:
        return jsonify({'error': 'Room not found'}), 404
    if username not in room.get('players', []):
        return jsonify({'error': 'Not in this room'}), 403
    if room.get('state') != 'reveal':
        return jsonify({'error': 'Not in reveal state'}), 400
    data = request.json or {}
    chain_owner = data.get('chain_owner', '')
    step_idx = str(data.get('step_idx', 0))
    emoji = data.get('emoji', '')
    if not emoji or len(emoji) > 10:
        return jsonify({'error': 'Invalid reaction'}), 400
    if not chain_owner or chain_owner not in room.get('players', []):
        return jsonify({'error': 'Invalid chain owner'}), 400
    key = f'reactions.{chain_owner}.{step_idx}.{username}'
    gartic_collection.update_one({'code': code}, {'$set': {key: emoji}})
    room = gartic_collection.find_one({'code': code}, {'reactions': 1, '_id': 0})
    socketio.emit('gartic_reactions', {'reactions': room.get('reactions', {})}, to=code)
    return jsonify({'ok': True})

@app.route('/api/gartic/room/<code>/reveal_next', methods=['POST'])
def gartic_reveal_next(code):
    username = current_user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    if gartic_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    room = gartic_collection.find_one({'code': code})
    if not room:
        return jsonify({'error': 'Room not found'}), 404
    if room['host'] != username:
        return jsonify({'error': 'Only host can advance reveal'}), 403
    if room['state'] != 'reveal':
        return jsonify({'error': 'Not in reveal state'}), 400

    chains = room.get('chains', {})
    players = room['players']
    chain_idx = room.get('reveal_chain_idx', 0)
    step_idx = room.get('reveal_step_idx', 0)

    if chain_idx >= len(players):
        gartic_collection.update_one({'code': code}, {'$set': {'state': 'done'}})
        socketio.emit('gartic_refresh', {}, to=code)
        return jsonify({'done': True})

    chain_owner = players[chain_idx]
    chain = chains.get(chain_owner, [])
    next_step = step_idx + 1

    if next_step >= len(chain):
        gartic_collection.update_one({'code': code}, {'$set': {
            'reveal_chain_idx': chain_idx + 1,
            'reveal_step_idx': -1
        }})
    else:
        gartic_collection.update_one({'code': code}, {'$set': {
            'reveal_step_idx': next_step
        }})
    
    socketio.emit('gartic_refresh', {}, to=code)
    room = gartic_collection.find_one({'code': code}, {'_id': 0})
    return jsonify(room)

@app.route('/api/gartic/room/<code>/leave', methods=['POST'])
def gartic_leave(code):
    username = current_user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    if gartic_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    
    room = gartic_collection.find_one({'code': code})
    if not room:
        return jsonify({'error': 'Room not found'}), 404
    
    if username not in room['players']:
        return jsonify({'error': 'Not in this room'}), 400
    
    if room['state'] in ('writing', 'drawing') and username not in room.get('submissions', {}):
        placeholder = '[left the game]'
        gartic_collection.update_one(
            {'code': code, f'submissions.{username}': {'$exists': False}},
            {'$set': {f'submissions.{username}': placeholder}}
        )
        updated = gartic_collection.find_one({'code': code})
        remaining_active = [p for p in updated['players'] if p != username]
        if remaining_active and all(p in updated.get('submissions', {}) for p in remaining_active):
            chains = updated.get('chains', {})
            assignments = updated.get('assignments', {})
            subs = updated.get('submissions', {})
            entry_type = 'text' if updated['state'] == 'writing' else 'drawing'
            for p, c in subs.items():
                owner = assignments.get(p, p)
                if owner in chains:
                    chains[owner].append({'type': entry_type, 'content': c, 'author': p})
            cur_round = updated['round']
            tot_rounds = updated['total_rounds']
            if cur_round >= tot_rounds:
                gartic_collection.update_one({'code': code}, {'$set': {
                    'chains': chains, 'submissions': {}, 'state': 'reveal',
                    'reveal_chain_idx': 0, 'reveal_step_idx': -1
                }})
            else:
                new_asgn = _gartic_rotate(remaining_active, assignments)
                nxt = 'drawing' if updated['state'] == 'writing' else 'writing'
                gartic_collection.update_one({'code': code}, {'$set': {
                    'chains': chains, 'submissions': {}, 'assignments': new_asgn,
                    'state': nxt, 'round': cur_round + 1,
                    'round_started_at': datetime.now(timezone.utc).isoformat()
                }})
    if room['host'] == username:
        remaining = [p for p in room['players'] if p != username]
        if remaining:
            gartic_collection.update_one(
                {'code': code},
                {'$set': {'host': remaining[0]}, '$pull': {'players': username}}
            )
        else:
            gartic_collection.delete_one({'code': code})
            return jsonify({'deleted': True})
    else:
        gartic_collection.update_one({'code': code}, {'$pull': {'players': username}})
    socketio.emit('gartic_player_left', {'username': username}, to=code)
    socketio.emit('gartic_refresh', {}, to=code)
    return jsonify({'left': True})

# SocketIO events for Gartic
@socketio.on('gartic_join')
def on_gartic_join(data):
    code = data.get('code')
    username = session.get('username')
    if not code or not username:
        return
    join_room(code)

@socketio.on('gartic_leave')
def on_gartic_leave(data):
    code = data.get('code')
    username = session.get('username')
    if not code or not username:
        return
    leave_room(code)

if __name__ == "__main__":
    _debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    socketio.run(app, host="0.0.0.0", port=5000, debug=_debug)