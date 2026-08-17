from flask import Blueprint, request, jsonify, render_template, session
from datetime import datetime, timezone
import random
import uuid

game_bp = Blueprint('game', __name__)

# Injected by init_game()
_socketio = None
_tl_rooms_col = None
_movies_df = None
_series_df = None
_app_module = None

DEFAULT_ROWS = [
    {'label': 'S', 'color': '#ff4d4d'},
    {'label': 'A', 'color': '#ff9f43'},
    {'label': 'B', 'color': '#ffd700'},
    {'label': 'C', 'color': '#2ed573'},
    {'label': 'D', 'color': '#74b9ff'},
]


def init_game(socketio, db, movies_df=None, series_df=None, app_module=None):
    global _socketio, _tl_rooms_col, _movies_df, _series_df, _app_module
    _socketio = socketio
    _tl_rooms_col = db.tl_game_rooms
    _tl_rooms_col.create_index('code', unique=True)
    _movies_df = movies_df
    _series_df = series_df
    _app_module = app_module


def _user():
    return session.get('username')


def _now():
    return datetime.now(timezone.utc).isoformat()


def _room(code):
    if _tl_rooms_col is None:
        return None
    return _tl_rooms_col.find_one({'code': code}, {'_id': 0})


def _emit_room(code):
    """Broadcast updated room state to all players in the room."""
    room = _room(code)
    if room and _socketio:
        _socketio.emit('tl_state', {'room': room}, to=code)


# ── Page ──────────────────────────────────────────────────────────────────────

@game_bp.route('/games/tier-list-game')
def tier_list_game_page():
    username = _user()
    if not username:
        from flask import redirect
        return redirect('/login')
    return render_template('tier_list_game.html', username=username)


# ── Room CRUD ─────────────────────────────────────────────────────────────────

@game_bp.route('/api/tlg/room', methods=['POST'])
def create_tl_room():
    username = _user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    if _tl_rooms_col is None:
        return jsonify({'error': 'DB unavailable'}), 500

    data = request.json or {}
    max_per_tier1 = data.get('max_per_tier1')  # None = unlimited
    if max_per_tier1 is not None:
        try:
            max_per_tier1 = max(1, int(max_per_tier1))
        except (ValueError, TypeError):
            max_per_tier1 = None

    code = ''.join([str(random.randint(0, 9)) for _ in range(6)])
    room = {
        'code': code,
        'host': username,
        'players': [username],
        'state': 'lobby',          # lobby | adding | playing | finished
        'title': 'My Tier List',
        'rows': [dict(r) for r in DEFAULT_ROWS],
        'items': [],               # {id, title, kind, img, added_by, votes, row}
        'queue': [],               # shuffled ids waiting to be placed
        'current_item_id': None,
        'vote': None,              # {item_id, row_label, yes:[], no:[]}
        'max_per_tier1': max_per_tier1,
        'round': 0,
        'created_at': _now(),
    }
    _tl_rooms_col.insert_one(room)
    return jsonify({'code': code}), 201


@game_bp.route('/api/tlg/room/<code>', methods=['GET'])
def get_tl_room(code):
    username = _user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    room = _room(code)
    if not room:
        return jsonify({'error': 'Room not found'}), 404
    return jsonify(room)


@game_bp.route('/api/tlg/room/<code>/join', methods=['POST'])
def join_tl_room(code):
    username = _user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    if _tl_rooms_col is None:
        return jsonify({'error': 'DB unavailable'}), 500
    room = _tl_rooms_col.find_one({'code': code})
    if not room:
        return jsonify({'error': 'Room not found'}), 404
    if room['state'] not in ('lobby', 'adding'):
        return jsonify({'error': 'Game already in progress'}), 400
    if username not in room['players']:
        _tl_rooms_col.update_one({'code': code}, {'$addToSet': {'players': username}})
    _emit_room(code)
    return jsonify({'code': code})


# ── Settings (host only) ──────────────────────────────────────────────────────

@game_bp.route('/api/tlg/room/<code>/settings', methods=['POST'])
def update_settings(code):
    username = _user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    if _tl_rooms_col is None:
        return jsonify({'error': 'DB unavailable'}), 500
    room = _tl_rooms_col.find_one({'code': code})
    if not room:
        return jsonify({'error': 'Room not found'}), 404
    if room['host'] != username:
        return jsonify({'error': 'Host only'}), 403

    data = request.json or {}
    upd = {}
    if 'max_per_tier1' in data:
        v = data['max_per_tier1']
        upd['max_per_tier1'] = max(1, int(v)) if v is not None else None
    if upd:
        _tl_rooms_col.update_one({'code': code}, {'$set': upd})
    _emit_room(code)
    return jsonify({'ok': True})


# ── Title & Row rename (everyone can) ────────────────────────────────────────

@game_bp.route('/api/tlg/room/<code>/title', methods=['POST'])
def set_title(code):
    username = _user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    room = _room(code)
    if not room:
        return jsonify({'error': 'Room not found'}), 404
    if username not in room['players']:
        return jsonify({'error': 'Not in room'}), 403
    title = (request.json or {}).get('title', '').strip()[:80] or 'My Tier List'
    _tl_rooms_col.update_one({'code': code}, {'$set': {'title': title}})
    _emit_room(code)
    return jsonify({'ok': True})


@game_bp.route('/api/tlg/room/<code>/rows', methods=['POST'])
def update_rows(code):
    username = _user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    room = _room(code)
    if not room:
        return jsonify({'error': 'Room not found'}), 404
    if username not in room['players']:
        return jsonify({'error': 'Not in room'}), 403
    rows = (request.json or {}).get('rows', [])
    clean = []
    for r in rows[:10]:
        clean.append({
            'label': str(r.get('label', ''))[:10],
            'color': str(r.get('color', '#888'))[:20],
        })
    if not clean:
        return jsonify({'error': 'Need at least one row'}), 400
    _tl_rooms_col.update_one({'code': code}, {'$set': {'rows': clean}})
    _emit_room(code)
    return jsonify({'ok': True})


# ── Adding phase ──────────────────────────────────────────────────────────────

@game_bp.route('/api/tlg/room/<code>/start_adding', methods=['POST'])
def start_adding(code):
    username = _user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    room = _room(code)
    if not room:
        return jsonify({'error': 'Room not found'}), 404
    if room['host'] != username:
        return jsonify({'error': 'Host only'}), 403
    if room['state'] != 'lobby':
        return jsonify({'error': 'Already started'}), 400
    _tl_rooms_col.update_one({'code': code}, {'$set': {'state': 'adding'}})
    _emit_room(code)
    return jsonify({'ok': True})


@game_bp.route('/api/tlg/room/<code>/add_item', methods=['POST'])
def add_item(code):
    username = _user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    room = _room(code)
    if not room:
        return jsonify({'error': 'Room not found'}), 404
    if username not in room['players']:
        return jsonify({'error': 'Not in room'}), 403
    if room['state'] not in ('lobby', 'adding'):
        return jsonify({'error': 'Not in adding phase'}), 400

    data = request.json or {}
    title = str(data.get('title', '')).strip()[:120]
    kind = data.get('kind', 'movie')
    img = str(data.get('img', '')).strip()[:500]
    if not title:
        return jsonify({'error': 'Title required'}), 400

    # Auto-transition lobby -> adding on first item add
    if room['state'] == 'lobby':
        _tl_rooms_col.update_one({'code': code}, {'$set': {'state': 'adding'}})
        room['state'] = 'adding'

    items = room.get('items', [])

    # Deduplicate: remove existing item with same title+kind (keep latest)
    items = [i for i in items if not (
        i['title'].lower() == title.lower() and i['kind'] == kind
    )]

    item = {
        'id': str(uuid.uuid4())[:10],
        'title': title,
        'kind': kind,
        'img': img,
        'added_by': username,
        'votes': 0,
        'row': None,
    }
    items.append(item)
    _tl_rooms_col.update_one({'code': code}, {'$set': {'items': items}})
    _emit_room(code)
    return jsonify({'ok': True, 'item': item})


@game_bp.route('/api/tlg/room/<code>/remove_item', methods=['POST'])
def remove_item(code):
    username = _user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    room = _room(code)
    if not room:
        return jsonify({'error': 'Room not found'}), 404
    if username not in room['players']:
        return jsonify({'error': 'Not in room'}), 403
    if room['state'] not in ('lobby', 'adding'):
        return jsonify({'error': 'Not in adding phase'}), 400

    item_id = (request.json or {}).get('item_id')
    items = room.get('items', [])
    item = next((i for i in items if i['id'] == item_id), None)
    if not item:
        return jsonify({'error': 'Item not found'}), 404
    # Only adder or host can remove
    if item['added_by'] != username and room['host'] != username:
        return jsonify({'error': 'Not authorized'}), 403
    items = [i for i in items if i['id'] != item_id]
    _tl_rooms_col.update_one({'code': code}, {'$set': {'items': items}})
    _emit_room(code)
    return jsonify({'ok': True})


# ── Playing phase ─────────────────────────────────────────────────────────────

@game_bp.route('/api/tlg/room/<code>/start_game', methods=['POST'])
def start_game(code):
    username = _user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    room = _room(code)
    if not room:
        return jsonify({'error': 'Room not found'}), 404
    if room['host'] != username:
        return jsonify({'error': 'Host only'}), 403
    if room['state'] not in ('lobby', 'adding'):
        return jsonify({'error': 'Must be in adding phase'}), 400

    items = room.get('items', [])
    if not items:
        return jsonify({'error': 'Add at least one item first'}), 400

    # Shuffle queue
    queue = [i['id'] for i in items]
    random.shuffle(queue)

    _tl_rooms_col.update_one({'code': code}, {'$set': {
        'state': 'playing',
        'queue': queue,
        'current_item_id': queue[0],
        'vote': None,
        'round': room.get('round', 0) + 1
    }})
    _emit_room(code)
    return jsonify({'ok': True})


@game_bp.route('/api/tlg/room/<code>/propose', methods=['POST'])
def propose_placement(code):
    """Host proposes placing current item in a row — triggers vote."""
    username = _user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    room = _room(code)
    if not room:
        return jsonify({'error': 'Room not found'}), 404
    if room['host'] != username:
        return jsonify({'error': 'Host only'}), 403
    if room['state'] != 'playing':
        return jsonify({'error': 'Not in playing phase'}), 400
    if room.get('vote'):
        return jsonify({'error': 'A vote is already in progress'}), 400

    data = request.json or {}
    row_label = data.get('row_label', '')
    item_id = room.get('current_item_id')
    if not item_id:
        return jsonify({'error': 'No current item'}), 400

    # Validate row exists
    rows = room.get('rows', [])
    if not any(r['label'] == row_label for r in rows):
        return jsonify({'error': 'Invalid row'}), 400

    # Check S-tier limit
    max_t1 = room.get('max_per_tier1')
    tier1_label = rows[0]['label'] if rows else None
    if max_t1 and row_label == tier1_label:
        items = room.get('items', [])
        placed_in_t1 = sum(1 for i in items if i.get('row') == tier1_label)
        if placed_in_t1 >= max_t1:
            return jsonify({'error': f'S-tier is full ({max_t1} items max)'}), 400

    vote = {
        'item_id': item_id,
        'row_label': row_label,
        'yes': [],
        'no': [],
    }
    _tl_rooms_col.update_one({'code': code}, {'$set': {'vote': vote}})
    _emit_room(code)
    return jsonify({'ok': True})


@game_bp.route('/api/tlg/room/<code>/cast_vote', methods=['POST'])
def cast_vote(code):
    """Any player votes yes/no on current proposal."""
    username = _user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    room = _room(code)
    if not room:
        return jsonify({'error': 'Room not found'}), 404
    if username not in room['players']:
        return jsonify({'error': 'Not in room'}), 403
    if room['state'] != 'playing':
        return jsonify({'error': 'Not in playing phase'}), 400

    vote = room.get('vote')
    if not vote:
        return jsonify({'error': 'No active vote'}), 400

    choice = (request.json or {}).get('choice')  # 'yes' or 'no'
    if choice not in ('yes', 'no'):
        return jsonify({'error': 'Invalid choice'}), 400

    # Remove from both lists first (allow change)
    yes_list = [u for u in vote.get('yes', []) if u != username]
    no_list = [u for u in vote.get('no', []) if u != username]

    if choice == 'yes':
        yes_list.append(username)
    else:
        no_list.append(username)

    vote['yes'] = yes_list
    vote['no'] = no_list

    total_players = len(room['players'])
    total_voted = len(yes_list) + len(no_list)

    upd = {'vote': vote}

    # Auto-resolve when everyone has voted
    if total_voted >= total_players:
        upd = _resolve_vote(room, vote, yes_list, no_list)

    _tl_rooms_col.update_one({'code': code}, {'$set': upd})
    _emit_room(code)
    return jsonify({'ok': True})


def _resolve_vote(room, vote, yes_list, no_list):
    """Resolve vote result and advance queue or keep current item."""
    yes_count = len(yes_list)
    no_count = len(no_list)
    item_id = vote['item_id']
    row_label = vote['row_label']
    items = room.get('items', [])
    queue = room.get('queue', [])
    rows = room.get('rows', [])
    max_t1 = room.get('max_per_tier1')

    upd = {'vote': None}

    if yes_count > no_count:
        # Check tier1 limit
        tier1_label = rows[0]['label'] if rows else None
        if max_t1 and row_label == tier1_label:
            placed_in_t1 = sum(1 for i in items if i.get('row') == tier1_label)
            if placed_in_t1 >= max_t1:
                # Reject — tier1 full
                upd['vote'] = None
                upd['current_item_id'] = item_id  # keep same item
                upd['_tier1_full_reject'] = True
                return upd

        # Place item
        new_items = []
        for i in items:
            if i['id'] == item_id:
                i = dict(i)
                i['row'] = row_label
                i['votes'] = yes_count
            new_items.append(i)
        upd['items'] = new_items

        # Advance queue
        new_queue = [q for q in queue if q != item_id]
        upd['queue'] = new_queue
        upd['current_item_id'] = new_queue[0] if new_queue else None

        if not new_queue:
            upd['state'] = 'finished'
    else:
        # Rejected — keep same item, clear vote so host can try another row
        upd['current_item_id'] = item_id

    return upd


@game_bp.route('/api/tlg/room/<code>/skip_item', methods=['POST'])
def skip_item(code):
    """Host skips the current item (removes it from queue without placing)."""
    username = _user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    room = _room(code)
    if not room:
        return jsonify({'error': 'Room not found'}), 404
    if room['host'] != username:
        return jsonify({'error': 'Host only'}), 403
    if room['state'] != 'playing':
        return jsonify({'error': 'Not in playing phase'}), 400
    if room.get('vote'):
        return jsonify({'error': 'Cannot skip during a vote'}), 400

    queue = room.get('queue', [])
    current_id = room.get('current_item_id')
    
    if not current_id or not queue:
        return jsonify({'error': 'No current item'}), 400
    
    # Remove current item from queue
    new_queue = [q for q in queue if q != current_id]
    
    upd = {
        'queue': new_queue,
        'current_item_id': new_queue[0] if new_queue else None,
        'vote': None
    }
    
    if not new_queue:
        upd['state'] = 'finished'
    
    _tl_rooms_col.update_one({'code': code}, {'$set': upd})
    _emit_room(code)
    return jsonify({'ok': True})


@game_bp.route('/api/tlg/room/<code>/force_resolve', methods=['POST'])
def force_resolve(code):
    """Host force-resolves current vote (in case someone disconnected)."""
    username = _user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    room = _room(code)
    if not room:
        return jsonify({'error': 'Room not found'}), 404
    if room['host'] != username:
        return jsonify({'error': 'Host only'}), 403
    if room['state'] != 'playing':
        return jsonify({'error': 'Not in playing phase'}), 400

    vote = room.get('vote')
    if not vote:
        return jsonify({'error': 'No active vote'}), 400

    yes_list = vote.get('yes', [])
    no_list = vote.get('no', [])
    
    # If no one has voted, default to yes (approve placement)
    if not yes_list and not no_list:
        # Get all players
        players = room.get('players', [])
        # Host votes yes by default
        yes_list = [room['host']]
        # Other players are considered abstained (not counted)
    
    upd = _resolve_vote(room, vote, yes_list, no_list)
    _tl_rooms_col.update_one({'code': code}, {'$set': upd})
    _emit_room(code)
    return jsonify({'ok': True})


# ── Reorder (finished phase, host + voting) ───────────────────────────────────

@game_bp.route('/api/tlg/room/<code>/reorder', methods=['POST'])
def reorder_items(code):
    """
    Host proposes moving an item to a new position (same row = horizontal,
    different row = vertical). Triggers a vote.
    """
    username = _user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    room = _room(code)
    if not room:
        return jsonify({'error': 'Room not found'}), 404
    if room['host'] != username:
        return jsonify({'error': 'Host only'}), 403
    if room['state'] != 'finished':
        return jsonify({'error': 'Only after game is finished'}), 400
    if room.get('vote'):
        return jsonify({'error': 'A vote is already in progress'}), 400

    data = request.json or {}
    item_id = data.get('item_id')
    new_row = data.get('new_row')
    new_index = data.get('new_index', 0)

    items = room.get('items', [])
    rows = room.get('rows', [])
    item = next((i for i in items if i['id'] == item_id), None)
    if not item:
        return jsonify({'error': 'Item not found'}), 404
    if not any(r['label'] == new_row for r in rows):
        return jsonify({'error': 'Invalid row'}), 400

    vote = {
        'item_id': item_id,
        'row_label': new_row,
        'new_index': new_index,
        'reorder': True,
        'yes': [],
        'no': [],
    }
    _tl_rooms_col.update_one({'code': code}, {'$set': {'vote': vote}})
    _emit_room(code)
    return jsonify({'ok': True})


@game_bp.route('/api/tlg/room/<code>/cast_reorder_vote', methods=['POST'])
def cast_reorder_vote(code):
    username = _user()
    if not username:
        return jsonify({'error': 'Login required'}), 401
    room = _room(code)
    if not room:
        return jsonify({'error': 'Room not found'}), 404
    if username not in room['players']:
        return jsonify({'error': 'Not in room'}), 403

    vote = room.get('vote')
    if not vote or not vote.get('reorder'):
        return jsonify({'error': 'No active reorder vote'}), 400

    choice = (request.json or {}).get('choice')
    if choice not in ('yes', 'no'):
        return jsonify({'error': 'Invalid choice'}), 400

    yes_list = [u for u in vote.get('yes', []) if u != username]
    no_list = [u for u in vote.get('no', []) if u != username]
    if choice == 'yes':
        yes_list.append(username)
    else:
        no_list.append(username)

    vote['yes'] = yes_list
    vote['no'] = no_list
    total_voted = len(yes_list) + len(no_list)
    total_players = len(room['players'])

    upd = {'vote': vote}

    if total_voted >= total_players:
        if len(yes_list) > len(no_list):
            # Apply reorder
            item_id = vote['item_id']
            new_row = vote['row_label']
            new_index = vote.get('new_index', 0)
            items = room.get('items', [])
            # Remove item from list
            moved = next((i for i in items if i['id'] == item_id), None)
            if moved:
                moved = dict(moved)
                moved['row'] = new_row
                items = [i for i in items if i['id'] != item_id]
                # Get items in target row
                row_items = [i for i in items if i.get('row') == new_row]
                other_items = [i for i in items if i.get('row') != new_row]
                row_items.insert(max(0, new_index), moved)
                upd['items'] = other_items + row_items
        upd['vote'] = None

    _tl_rooms_col.update_one({'code': code}, {'$set': upd})
    _emit_room(code)
    return jsonify({'ok': True})


# ── Search ────────────────────────────────────────────────────────────────────

@game_bp.route('/api/tlg/search')
def tlg_search():
    username = _user()
    if not username:
        return jsonify({'error': 'Login required'}), 401

    q = request.args.get('q', '').strip().lower()
    kind = request.args.get('kind', 'movie')
    if not q:
        return jsonify([])

    results = []
    # Lazily grab dataframes from app module if not injected at startup
    df = _movies_df if kind == 'movie' else _series_df
    if df is None and _app_module is not None:
        df = getattr(_app_module, 'df', None) if kind == 'movie' else getattr(_app_module, 'series_df', None)
    if df is not None:
        title_col = next((c for c in df.columns if 'title' in c.lower() or 'name' in c.lower()), None)
        if title_col:
            mask = df[title_col].astype(str).str.lower().str.contains(q, na=False)
            for _, row in df[mask].head(10).iterrows():
                results.append({
                    'title': str(row[title_col]),
                    'kind': kind,
                    'img': '',
                })
    return jsonify(results)


# ── SocketIO events ───────────────────────────────────────────────────────────

def register_socketio_events(socketio):
    from flask_socketio import join_room as sio_join, leave_room as sio_leave

    @socketio.on('tl_join')
    def on_tl_join(data):
        code = data.get('room_code', '')
        username = session.get('username')
        if not username or not code:
            return
        sio_join(code)
        room = _room(code)
        if room:
            socketio.emit('tl_state', {'room': room}, to=code)

    @socketio.on('tl_leave')
    def on_tl_leave(data):
        code = data.get('room_code', '')
        sio_leave(code)