import json
import math
import atexit
import logging
import requests as http_requests
from pywebpush import webpush, WebPushException
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

def _close_mongo():
    if client is not None:
        try:
            client.close()
        except Exception as exc:
            log.error("Error closing MongoDB client: %s", exc)

atexit.register(_close_mongo)

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

@app.route('/authors_choice')
def authors_choice():
    return render_template('authors_choice.html')

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
            c['created_at'] = c['created_at'].isoformat()
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
    if users_collection is not None:
        user_doc = users_collection.find_one({'username': username}, {'_id': 0, 'bio': 1, 'avatar_url': 1})
        if user_doc:
            bio = user_doc.get('bio', '')
            avatar_url = user_doc.get('avatar_url', '')

    return render_template('profile.html', username=username, comments=user_comments,
                           total_likes=total_likes, avg_rating=avg_rating,
                           followers=followers, following=following,
                           is_following=is_following, viewer=viewer,
                           user_lists=user_lists, bio=bio, avatar_url=avatar_url)

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
            c['created_at'] = c['created_at'].isoformat()
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

# ===== Avatar Update =====
@app.route('/api/profile/avatar', methods=['POST'])
def update_avatar():
    username = current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    if users_collection is None:
        return jsonify({'error': 'DB unavailable'}), 500
    url = (request.json or {}).get('url', '').strip()[:500]
    if url and not url.startswith(('http://', 'https://')):
        return jsonify({'error': 'Invalid URL'}), 400
    users_collection.update_one({'username': username}, {'$set': {'avatar_url': url}})
    return jsonify({'success': True, 'avatar_url': url})

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

def send_push(username, payload):
    """Send a push notification — never raises, logs errors only."""
    if push_subs_collection is None or not VAPID_PRIVATE_KEY:
        return
    try:
        doc = push_subs_collection.find_one({'username': username}, {'_id': 0, 'subscription': 1})
        if not doc:
            return
        sub = doc['subscription']
        webpush(
            subscription_info=sub,
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={'sub': VAPID_CLAIMS_EMAIL}
        )
    except WebPushException as ex:
        log.error('WebPush failed for %s: %s', username, ex)
        if ex.response and ex.response.status_code in (404, 410):
            push_subs_collection.delete_one({'username': username})
    except Exception as ex:
        log.error('send_push unexpected error for %s: %s', username, ex)

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
    else:
        msgs = []

    return render_template('conversation.html', username=username,
                           other_user=other_user, messages=msgs)


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

    if not to or not text:
        return jsonify({'error': 'Recipient and message required'}), 400
    if to == username:
        return jsonify({'error': 'Cannot message yourself'}), 400

    # Verify recipient exists
    if users_collection is not None:
        if not users_collection.find_one({'username': to}):
            return jsonify({'error': 'User not found'}), 404

    msg = {
        'msg_id': str(uuid.uuid4())[:12],
        'sender': username,
        'text': text,
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
        'body': text[:80],
        'url': f'/messages/{username}',
        'tag': f'dm-{username}'
    })
    return jsonify(msg), 201


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
    sub = doc['subscription']
    if not VAPID_PRIVATE_KEY:
        return jsonify({'error': 'VAPID_PRIVATE_KEY not set'}), 500
    try:
        webpush(
            subscription_info=sub,
            data=json.dumps({
                'title': '🎉 Test Notification',
                'body': 'MediaPedia push is working!',
                'url': '/party',
                'tag': 'test'
            }),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={'sub': VAPID_CLAIMS_EMAIL}
        )
        return jsonify({'sent': True, 'to': username})
    except WebPushException as ex:
        return jsonify({'error': str(ex), 'response': str(ex.response.text if ex.response else '')}), 500
    except Exception as ex:
        return jsonify({'error': type(ex).__name__ + ': ' + str(ex)}), 500

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


if __name__ == "__main__":
    _debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    socketio.run(app, host="0.0.0.0", port=5000, debug=_debug)
