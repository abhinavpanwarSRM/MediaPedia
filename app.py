import json
from flask import Flask, request, jsonify, render_template
import pandas as pd
import os
from pymongo import MongoClient
from datetime import datetime
import uuid
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

MONGO_URI = os.getenv("MONGO_URI", "")
DB_NAME = "mediapedia"

try:
    client = MongoClient(MONGO_URI)
    db = client[DB_NAME]
    comments_collection = db.comments
    
    # Create indexes for better performance
    comments_collection.create_index("id")
    comments_collection.create_index("created_at")
    print("✅ MongoDB connected successfully")
except Exception as e:
    print(f"❌ MongoDB connection failed: {e}")
    comments_collection = None

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
            "comment_id": str(uuid.uuid4()),  # Generate unique ID
            "id": data["id"],
            "username": data["username"][:50],  # Limit length
            "text": data["text"][:1000],  # Limit length
            "rating": min(5, max(1, int(data.get("rating", 5)))),  # 1-5 stars
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
    return render_template("index.html")

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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)