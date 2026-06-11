import json
from flask import Flask, request, jsonify, render_template, session, redirect, url_for
import pandas as pd
import os
from pymongo import MongoClient, DESCENDING
from datetime import datetime
import uuid
from dotenv import load_dotenv
from flask_bcrypt import Bcrypt

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "mediapedia-secret-2024")
bcrypt = Bcrypt(app)

MONGO_URI = os.getenv("MONGO_URI", "")
DB_NAME = "mediapedia"

try:
    client = MongoClient(MONGO_URI)
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
    print("✅ MongoDB connected successfully")
except Exception as e:
    print(f"❌ MongoDB connection failed: {e}")
    db = None
    comments_collection = None
    users_collection = None
    lists_collection = None
    follows_collection = None

# ===== TMDB Proxy (keeps token server-side) =====
@app.route("/api/tmdb/popular")
def tmdb_popular():
    import urllib.request
    token = os.getenv("TMDB_TOKEN", "")
    if not token:
        return jsonify({"error": "TMDB token not configured"}), 500
    try:
        req = urllib.request.Request(
            "https://api.themoviedb.org/3/movie/popular",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            import json as _json
            return jsonify(_json.loads(resp.read()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
            "created_at": datetime.utcnow(),
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
        
        return jsonify({"likes": comment.get("likes", 0)})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/comments/<comment_id>", methods=["DELETE"])
def delete_comment(comment_id):
    """Delete a comment (requires username verification)"""
    if comments_collection is None:
        return jsonify({"error": "Database not connected"}), 500
    
    try:
        username = request.args.get("username")
        if not username:
            return jsonify({"error": "Username required"}), 400
        
        # Find and delete comment (only if username matches)
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

    # Check if MongoDB is connected
    mongo_connected = comments_collection is not None

    return render_template(
        "movie.html", 
        movie=movie, 
        related_movies=related_movies,
        api_key_1=api_key_1,
        api_key_2=api_key_2,
        mongo_connected=mongo_connected,
        movie_id=movie_id
    )

@app.route("/search")
def search():
    query = request.args.get("query", "").lower()
    genre = request.args.get("genre", "").lower()
    actor = request.args.get("actor", "").lower()
    director = request.args.get("director", "").lower()

    try:
        min_rating = float(request.args.get("min_rating", 0) or 0)
    except ValueError:
        min_rating = 0

    try:
        max_rating = float(request.args.get("max_rating", 10) or 10)
    except ValueError:
        max_rating = 10

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

    if year:
        results = results[results['Year'].astype(str).str.contains(year, na=False)]

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

    return render_template(
        "series.html",
        series=series,
        related_series=related_series,
        api_key_1=api_key_1,
        api_key_2=api_key_2,
        series_id=series_id
    )

# ===== Series Search =====
@app.route("/search_series")
def search_series():
    query = request.args.get("query", "").strip().lower()
    genre = request.args.get("genre", "").strip().lower()
    actor = request.args.get("actor", "").strip().lower()
    year = request.args.get("year", "").strip()
    min_rating = float(request.args.get("min_rating", 0) or 0)
    max_rating = float(request.args.get("max_rating", 10) or 10)

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

    return render_template(
        "artist.html",
        artist=artist,
        related_artists=related_artists,
        api_key_1=api_key_1,
        api_key_2=api_key_2
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

    return render_template(
        "game.html",
        game=game,
        recommendations=recommendations,
        api_key_1=api_key_1,
        api_key_2=api_key_2
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
        (games_df['Rank'] > 100)  # Lower ranked games
    ].sample(n=1)
    
    recommendations = pd.concat([similar_games, diverse_games])
    return recommendations.to_dict(orient='records')

# ===== Game Search =====
@app.route("/search_games")
def search_games():
    name = request.args.get("name", "").strip().lower()
    platform = request.args.get("platform", "").strip()
    year = request.args.get("year", "").strip()
    genre = request.args.get("genre", "").strip()
    publisher = request.args.get("publisher", "").strip()
    min_sales = float(request.args.get("min_sales", 0) or 0)
    max_sales = float(request.args.get("max_sales", 100) or 100)

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
    diverse_games = games_df[
        (games_df['Rank'] > 100) & 
        ~games_df['Genre'].isin(top_games['Genre'].unique())
    ].sample(n=5)
    
    recommendations = pd.concat([top_games, diverse_games]).sample(frac=1)  # Shuffle
    recommendations['DetailLink'] = recommendations['ID'].apply(lambda x: f"/game/{x}")
    
    # Limit to 500 results
    recommendations = recommendations.head(500)
    
    return jsonify(recommendations.to_dict(orient="records"))

# ===== Director Page =====
@app.route("/director/<path:director_name>")
def director_page(director_name):
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
                           signature_genres=signature_genres, avg_rating=avg_rating, total=len(movies))

# ===== Actor Page =====
@app.route("/actor/<path:actor_name>")
def actor_page(actor_name):
    name_lower = actor_name.lower()
    movies = df[df['Stars'].str.lower().str.contains(name_lower, na=False)].to_dict(orient='records')
    series = series_df[series_df['Actors'].str.lower().str.contains(name_lower, na=False)].to_dict(orient='records')
    if not movies and not series:
        return render_template('404.html'), 404
    movies.sort(key=lambda x: float(x.get('Rating') or 0), reverse=True)
    series.sort(key=lambda x: float(x.get('Rating') or 0), reverse=True)
    return render_template('actor.html', actor_name=actor_name, movies=movies[:20], series=series[:10])

# ===== Franchise Tracker =====
@app.route("/franchise/<path:franchise_name>")
def franchise_page(franchise_name):
    name_lower = franchise_name.lower()
    franchise_movies = df[df['Movie Name'].str.lower().str.contains(name_lower, na=False)]\
        .sort_values('Rating', ascending=False).to_dict(orient='records')
    if not franchise_movies:
        return render_template('404.html'), 404
    return render_template('franchise.html', franchise_name=franchise_name, movies=franchise_movies)

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
        # movies.csv has no Year column — decade filter skipped for movies
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
except Exception:
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
        return render_template('404.html'), 404
    user_comments = list(comments_collection.find(
        {'username': username}, {'_id': 0}
    ).sort('created_at', -1).limit(50))
    for c in user_comments:
        if 'created_at' in c:
            c['created_at'] = c['created_at'].isoformat()
    total_likes = sum(c.get('likes', 0) for c in user_comments)
    avg_rating = round(sum(c.get('rating', 0) for c in user_comments) / len(user_comments), 1) if user_comments else 0
    followers = follows_collection.count_documents({'following': username}) if follows_collection else 0
    following = follows_collection.count_documents({'follower': username}) if follows_collection else 0
    viewer = current_user()
    is_following = follows_collection.find_one({'follower': viewer, 'following': username}) is not None if (follows_collection and viewer) else False
    user_lists = list(lists_collection.find({'username': username}, {'_id': 0})) if lists_collection else []
    return render_template('profile.html', username=username, comments=user_comments,
                           total_likes=total_likes, avg_rating=avg_rating,
                           followers=followers, following=following,
                           is_following=is_following, viewer=viewer,
                           user_lists=user_lists)

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
    {'year': 2021, 'title': 'Nomadland', 'director': 'Chloé Zhao'},
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
    return render_template('oscars.html', winners=enriched)

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
    results = []
    # Movies
    movie_hits = df[df['Movie Name'].str.lower().str.contains(query, na=False)].head(5)
    for _, m in movie_hits.iterrows():
        results.append({'type': 'movie', 'id': int(m['ID']), 'title': m['Movie Name'],
                        'sub': m.get('Genre', ''), 'url': f"/movie/{m['ID']}"})
    # Series
    series_hits = series_df[series_df['Title'].str.lower().str.contains(query, na=False)].head(5)
    for _, s in series_hits.iterrows():
        results.append({'type': 'series', 'id': int(s['ID']), 'title': s['Title'],
                        'sub': s.get('Genres', ''), 'url': f"/series/{s['ID']}"})
    # Artists
    artist_hits = artists_df[artists_df['artist_name'].str.lower().str.contains(query, na=False)].head(3)
    for _, a in artist_hits.iterrows():
        results.append({'type': 'artist', 'id': int(a['ID']), 'title': a['artist_name'],
                        'sub': a.get('artist_genre', ''), 'url': f"/artist/{a['ID']}"})
    # Directors
    dir_hits = df[df['Directors'].str.lower().str.contains(query, na=False)]
    if not dir_hits.empty:
        dirs = set()
        for d_str in dir_hits['Directors'].dropna():
            for d in str(d_str).strip("[]").replace("'", "").split(","):
                d = d.strip()
                if d and query in d.lower():
                    dirs.add(d)
        for d in list(dirs)[:2]:
            results.append({'type': 'director', 'id': 0, 'title': d,
                            'sub': 'Director', 'url': f"/director/{d}"})
    return jsonify(results[:12])

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
            return render_template('register.html', error='Username must be 3–30 characters')
        if users_collection is None:
            return render_template('register.html', error='Database unavailable')
        if users_collection.find_one({'username': username}):
            return render_template('register.html', error='Username already taken')
        hashed = bcrypt.generate_password_hash(password).decode('utf-8')
        users_collection.insert_one({
            'username': username,
            'password': hashed,
            'created_at': datetime.utcnow(),
            'bio': ''
        })
        session['username'] = username
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
    follows_collection.insert_one({'follower': username, 'following': target, 'created_at': datetime.utcnow()})
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
        'created_at': datetime.utcnow()
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
        'added_at': datetime.utcnow().isoformat()
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
        'created_at': datetime.utcnow().isoformat(),
        'likes': 0
    }
    result = comments_collection.update_one(
        {'comment_id': comment_id},
        {'$push': {'replies': reply}}
    )
    if result.matched_count == 0:
        return jsonify({'error': 'Comment not found'}), 404
    return jsonify(reply), 201

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)