from flask import Blueprint, request, jsonify, render_template, session
from datetime import datetime, timezone
import uuid
import math
from push_utils import send_push

splitwise_bp = Blueprint('splitwise', __name__)

sw_groups_col = None
sw_expenses_col = None
sw_settlements_col = None
users_col = None
follows_col = None

CATEGORIES = {
    'food', 'groceries', 'restaurant', 'cafe', 'fast_food', 'delivery', 'alcohol',
    'travel', 'flight', 'train', 'bus', 'taxi', 'uber', 'fuel', 'parking',
    'rent', 'utilities', 'electricity', 'water', 'gas', 'internet', 'phone',
    'entertainment', 'movies', 'concerts', 'sports', 'games', 'streaming',
    'shopping', 'clothing', 'electronics', 'accessories', 'cosmetics', 'gifts',
    'health', 'medical', 'gym', 'pharmacy',
    'education', 'tuition', 'books', 'courses',
    'subscriptions', 'bills', 'insurance', 'personal_care',
    'pets', 'children', 'charity', 'business', 'other'
}


def init_splitwise(db):
    global sw_groups_col, sw_expenses_col, sw_settlements_col, users_col, follows_col
    sw_groups_col = db.sw_groups
    sw_groups_col.create_index('members')
    sw_expenses_col = db.sw_expenses
    sw_expenses_col.create_index('group_id')
    sw_settlements_col = db.sw_settlements
    sw_settlements_col.create_index('group_id')
    users_col = db.users
    follows_col = db.follows


def _current_user():
    return session.get('username')


def _now():
    return datetime.now(timezone.utc).isoformat()


def _parse_splits(data, amount, members):
    """
    Returns a dict {member: share_amount} based on split_type.
    split_type: 'equal' | 'exact' | 'percent'
    splits: {member: value}  (required for exact/percent)
    """
    split_type = data.get('split_type', 'equal')
    split_among = [m.strip() for m in data.get('split_among', members) if m.strip()]
    if not split_among:
        split_among = members
    splits_input = data.get('splits', {})

    if split_type == 'exact':
        result = {}
        total = 0
        for m in split_among:
            try:
                v = round(float(splits_input.get(m, 0)), 2)
                if math.isnan(v) or math.isinf(v) or v < 0:
                    v = 0
            except (ValueError, TypeError):
                v = 0
            result[m] = v
            total += v
        if abs(total - amount) > 0.05:
            return None, f'Exact splits sum to ₹{total:.2f}, expected ₹{amount:.2f}'
        return result, None

    if split_type == 'percent':
        result = {}
        total_pct = 0
        for m in split_among:
            try:
                pct = round(float(splits_input.get(m, 0)), 4)
                if math.isnan(pct) or math.isinf(pct) or pct < 0:
                    pct = 0
            except (ValueError, TypeError):
                pct = 0
            result[m] = pct
            total_pct += pct
        if abs(total_pct - 100) > 0.1:
            return None, f'Percentages sum to {total_pct:.2f}%, must equal 100%'
        return {m: round(amount * pct / 100, 2) for m, pct in result.items()}, None

    # equal (default) — distribute rounding remainder to first member
    n = len(split_among)
    share = round(amount / n, 2)
    result = {m: share for m in split_among}
    remainder = round(amount - share * n, 2)
    if remainder:
        result[split_among[0]] = round(result[split_among[0]] + remainder, 2)
    return result, None


def _compute_balances(group_id):
    expenses = list(sw_expenses_col.find({'group_id': group_id}, {'_id': 0}))
    settlements = list(sw_settlements_col.find({'group_id': group_id}, {'_id': 0}))

    net = {}

    for exp in expenses:
        paid_by = exp['paid_by']
        member_shares = exp.get('member_shares')
        if not member_shares:
            share = exp.get('share', 0)
            member_shares = {m: share for m in exp.get('split_among', [])}

        for member, share in member_shares.items():
            if member == paid_by:
                continue
            net[paid_by] = net.get(paid_by, 0) + share
            net[member] = net.get(member, 0) - share

    for s in settlements:
        net[s['from_user']] = net.get(s['from_user'], 0) + s['amount']
        net[s['to_user']] = net.get(s['to_user'], 0) - s['amount']

    creditors = sorted([(u, v) for u, v in net.items() if v > 0.005], key=lambda x: -x[1])
    debtors = sorted([(u, -v) for u, v in net.items() if v < -0.005], key=lambda x: -x[1])

    transactions = []
    i, j = 0, 0
    creditors = list(creditors)
    debtors = list(debtors)
    while i < len(creditors) and j < len(debtors):
        cred_user, cred_amt = creditors[i]
        debt_user, debt_amt = debtors[j]
        settle = round(min(cred_amt, debt_amt), 2)
        transactions.append({'from': debt_user, 'to': cred_user, 'amount': settle})
        creditors[i] = (cred_user, round(cred_amt - settle, 2))
        debtors[j] = (debt_user, round(debt_amt - settle, 2))
        if creditors[i][1] < 0.005:
            i += 1
        if debtors[j][1] < 0.005:
            j += 1

    return {u: round(v, 2) for u, v in net.items()}, transactions


# ── Page ──────────────────────────────────────────────────────────────────────

@splitwise_bp.route('/splitwise')
def splitwise_page():
    username = _current_user()
    if not username:
        from flask import redirect
        return redirect('/login')
    return render_template('splitwise.html', username=username)


# ── Groups ────────────────────────────────────────────────────────────────────

@splitwise_bp.route('/api/sw/groups', methods=['GET'])
def get_groups():
    username = _current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    groups = list(sw_groups_col.find({'members': username}, {'_id': 0}))
    return jsonify(groups)


@splitwise_bp.route('/api/sw/groups', methods=['POST'])
def create_group():
    username = _current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    data = request.json or {}
    name = data.get('name', '').strip()[:60]
    members = list({username} | {m.strip() for m in data.get('members', []) if m.strip()})
    if not name:
        return jsonify({'error': 'Group name required'}), 400
    if users_col is not None:
        for m in members:
            if m != username and not users_col.find_one({'username': m}):
                return jsonify({'error': f'User "{m}" not found'}), 404
    group_id = str(uuid.uuid4())[:10]
    doc = {
        'group_id': group_id,
        'name': name,
        'members': members,
        'created_by': username,
        'created_at': _now()
    }
    sw_groups_col.insert_one(doc)
    return jsonify({'group_id': group_id, 'name': name, 'members': members}), 201


@splitwise_bp.route('/api/sw/groups/<group_id>', methods=['PATCH'])
def rename_group(group_id):
    username = _current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    g = sw_groups_col.find_one({'group_id': group_id, 'members': username})
    if not g:
        return jsonify({'error': 'Not found'}), 404
    if g.get('created_by') != username:
        return jsonify({'error': 'Only the group creator can rename it'}), 403
    name = (request.json or {}).get('name', '').strip()[:60]
    if not name:
        return jsonify({'error': 'Name required'}), 400
    sw_groups_col.update_one({'group_id': group_id}, {'$set': {'name': name}})
    return jsonify({'success': True, 'name': name})


@splitwise_bp.route('/api/sw/groups/<group_id>', methods=['GET'])
def get_group(group_id):
    username = _current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    g = sw_groups_col.find_one({'group_id': group_id, 'members': username}, {'_id': 0})
    if not g:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(g)


@splitwise_bp.route('/api/sw/groups/<group_id>', methods=['DELETE'])
def delete_group(group_id):
    username = _current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    g = sw_groups_col.find_one({'group_id': group_id, 'members': username})
    if not g:
        return jsonify({'error': 'Not found'}), 404
    if g.get('created_by') != username:
        return jsonify({'error': 'Only the group creator can delete it'}), 403
    sw_groups_col.delete_one({'group_id': group_id})
    sw_expenses_col.delete_many({'group_id': group_id})
    sw_settlements_col.delete_many({'group_id': group_id})
    return jsonify({'success': True})


@splitwise_bp.route('/api/sw/groups/<group_id>/members', methods=['POST'])
def add_member(group_id):
    username = _current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    new_member = (request.json or {}).get('username', '').strip()
    if not new_member:
        return jsonify({'error': 'Username required'}), 400
    if users_col is not None and not users_col.find_one({'username': new_member}):
        return jsonify({'error': f'User "{new_member}" not found'}), 404
    g = sw_groups_col.find_one({'group_id': group_id, 'members': username})
    if not g:
        return jsonify({'error': 'Group not found'}), 404
    sw_groups_col.update_one({'group_id': group_id}, {'$addToSet': {'members': new_member}})
    send_push(new_member, {
        'title': '👥 Added to a Split group',
        'body': f'{username} added you to "{g["name"]}"',
        'url': '/splitwise',
        'tag': f'sw-member-{group_id}'
    })
    return jsonify({'success': True})


@splitwise_bp.route('/api/sw/groups/<group_id>/members/<member>', methods=['DELETE'])
def remove_member(group_id, member):
    username = _current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    g = sw_groups_col.find_one({'group_id': group_id, 'members': username})
    if not g:
        return jsonify({'error': 'Group not found'}), 404
    if g.get('created_by') != username:
        return jsonify({'error': 'Only the group creator can remove members'}), 403
    if member == g.get('created_by'):
        return jsonify({'error': 'Cannot remove the group creator'}), 400
    sw_groups_col.update_one({'group_id': group_id}, {'$pull': {'members': member}})
    return jsonify({'success': True})


# ── Groups with balance hints ────────────────────────────────────────────────

@splitwise_bp.route('/api/sw/groups_with_balances', methods=['GET'])
def get_groups_with_balances():
    username = _current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    groups = list(sw_groups_col.find({'members': username}, {'_id': 0}))
    result = []
    for g in groups:
        net, _ = _compute_balances(g['group_id'])
        g['my_net'] = round(net.get(username, 0), 2)
        result.append(g)
    return jsonify(result)


# ── Summary (cross-group) ─────────────────────────────────────────────────────

@splitwise_bp.route('/api/sw/summary', methods=['GET'])
def get_summary():
    username = _current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    groups = list(sw_groups_col.find({'members': username}, {'_id': 0}))
    total_owed = 0    # others owe me
    total_owing = 0   # I owe others
    for g in groups:
        net, _ = _compute_balances(g['group_id'])
        v = net.get(username, 0)
        if v > 0:
            total_owed += v
        elif v < 0:
            total_owing += abs(v)
    return jsonify({
        'total_owed': round(total_owed, 2),
        'total_owing': round(total_owing, 2),
        'net': round(total_owed - total_owing, 2)
    })


# ── Expenses ──────────────────────────────────────────────────────────────────

@splitwise_bp.route('/api/sw/groups/<group_id>/expenses', methods=['GET'])
def get_expenses(group_id):
    username = _current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    g = sw_groups_col.find_one({'group_id': group_id, 'members': username})
    if not g:
        return jsonify({'error': 'Not found'}), 404
    query = {'group_id': group_id}
    cat = request.args.get('category')
    paid = request.args.get('paid_by')
    if cat:
        query['category'] = cat
    if paid:
        query['paid_by'] = paid
    skip = int(request.args.get('skip', 0))
    limit = int(request.args.get('limit', 50))
    total = sw_expenses_col.count_documents(query)
    expenses = list(sw_expenses_col.find(query, {'_id': 0}).sort('date', -1).skip(skip).limit(limit))
    return jsonify({'expenses': expenses, 'total': total, 'skip': skip, 'limit': limit})


@splitwise_bp.route('/api/sw/groups/<group_id>/expenses', methods=['POST'])
def add_expense(group_id):
    username = _current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    g = sw_groups_col.find_one({'group_id': group_id, 'members': username}, {'_id': 0})
    if not g:
        return jsonify({'error': 'Group not found'}), 404
    data = request.json or {}
    desc = data.get('description', '').strip()[:200]
    try:
        amount = round(float(data.get('amount', 0)), 2)
        if math.isnan(amount) or math.isinf(amount):
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid amount'}), 400
    if amount <= 0:
        return jsonify({'error': 'Amount must be positive'}), 400
    paid_by = data.get('paid_by', username).strip()
    if paid_by not in g['members']:
        return jsonify({'error': 'paid_by must be a group member'}), 400
    category = data.get('category', 'other')
    if category not in CATEGORIES:
        category = 'other'
    date = data.get('date', _now()[:10])

    split_among_req = [m.strip() for m in data.get('split_among', g['members']) if m.strip()]
    if not split_among_req:
        return jsonify({'error': 'split_among cannot be empty'}), 400
    for m in split_among_req:
        if m not in g['members']:
            return jsonify({'error': f'split_among member "{m}" is not in the group'}), 400

    member_shares, err = _parse_splits(data, amount, g['members'])
    if err:
        return jsonify({'error': err}), 400

    image_url = data.get('image_url', '').strip()[:500]
    if image_url and not image_url.startswith(('http://', 'https://', '/api/img/')):
        image_url = ''

    expense_id = str(uuid.uuid4())[:10]
    doc = {
        'expense_id': expense_id,
        'group_id': group_id,
        'description': desc or 'Expense',
        'amount': amount,
        'paid_by': paid_by,
        'split_among': list(member_shares.keys()),
        'member_shares': member_shares,
        'split_type': data.get('split_type', 'equal'),
        'category': category,
        'date': date,
        'image_url': image_url,
        'created_by': username,
        'created_at': _now()
    }
    sw_expenses_col.insert_one(doc)
    doc.pop('_id', None)
    # Notify other group members
    for m in g['members']:
        if m != username:
            send_push(m, {
                'title': f'💸 {username} added an expense',
                'body': f'{desc or "Expense"} — ₹{amount:.2f} in {g["name"]}',
                'url': '/splitwise',
                'tag': f'sw-expense-{group_id}'
            })
    return jsonify(doc), 201


@splitwise_bp.route('/api/sw/groups/<group_id>/expenses/<expense_id>', methods=['PUT'])
def edit_expense(group_id, expense_id):
    username = _current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    g = sw_groups_col.find_one({'group_id': group_id, 'members': username}, {'_id': 0})
    if not g:
        return jsonify({'error': 'Group not found'}), 404
    exp = sw_expenses_col.find_one({'expense_id': expense_id, 'group_id': group_id})
    if not exp:
        return jsonify({'error': 'Expense not found'}), 404
    if exp['created_by'] != username and g.get('created_by') != username:
        return jsonify({'error': 'Not authorized'}), 403
    data = request.json or {}
    desc = data.get('description', exp['description']).strip()[:200]
    try:
        amount = round(float(data.get('amount', exp['amount'])), 2)
        if math.isnan(amount) or math.isinf(amount):
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid amount'}), 400
    if amount <= 0:
        return jsonify({'error': 'Amount must be positive'}), 400
    paid_by = data.get('paid_by', exp['paid_by']).strip()
    if paid_by not in g['members']:
        return jsonify({'error': 'paid_by must be a group member'}), 400
    category = data.get('category', exp.get('category', 'other'))
    if category not in CATEGORIES:
        category = 'other'
    date = data.get('date', exp.get('date', _now()[:10]))

    split_among_req = [m.strip() for m in data.get('split_among', exp.get('split_among', g['members'])) if m.strip()]
    if not split_among_req:
        return jsonify({'error': 'split_among cannot be empty'}), 400
    for m in split_among_req:
        if m not in g['members']:
            return jsonify({'error': f'split_among member "{m}" is not in the group'}), 400

    member_shares, err = _parse_splits(data, amount, g['members'])
    if err:
        return jsonify({'error': err}), 400

    image_url = data.get('image_url', exp.get('image_url', '')).strip()[:500]
    if image_url and not image_url.startswith(('http://', 'https://', '/api/img/')):
        image_url = ''

    update = {
        'description': desc or 'Expense',
        'amount': amount,
        'paid_by': paid_by,
        'split_among': list(member_shares.keys()),
        'member_shares': member_shares,
        'split_type': data.get('split_type', exp.get('split_type', 'equal')),
        'category': category,
        'date': date,
        'image_url': image_url,
    }
    sw_expenses_col.update_one({'expense_id': expense_id}, {'$set': update})
    return jsonify({'success': True, **update})


@splitwise_bp.route('/api/sw/groups/<group_id>/expenses/<expense_id>', methods=['DELETE'])
def delete_expense(group_id, expense_id):
    username = _current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    g = sw_groups_col.find_one({'group_id': group_id, 'members': username})
    if not g:
        return jsonify({'error': 'Not found'}), 404
    exp = sw_expenses_col.find_one({'expense_id': expense_id, 'group_id': group_id})
    if not exp:
        return jsonify({'error': 'Expense not found'}), 404
    if exp['created_by'] != username and g.get('created_by') != username:
        return jsonify({'error': 'Not authorized'}), 403
    sw_expenses_col.delete_one({'expense_id': expense_id})
    return jsonify({'success': True})


# ── Balances ──────────────────────────────────────────────────────────────────

@splitwise_bp.route('/api/sw/groups/<group_id>/balances', methods=['GET'])
def get_balances(group_id):
    username = _current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    g = sw_groups_col.find_one({'group_id': group_id, 'members': username})
    if not g:
        return jsonify({'error': 'Not found'}), 404
    net, transactions = _compute_balances(group_id)
    return jsonify({'net': net, 'transactions': transactions})


# ── Settlements ───────────────────────────────────────────────────────────────

@splitwise_bp.route('/api/sw/groups/<group_id>/settle', methods=['POST'])
def settle(group_id):
    username = _current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    g = sw_groups_col.find_one({'group_id': group_id, 'members': username})
    if not g:
        return jsonify({'error': 'Not found'}), 404
    data = request.json or {}
    to_user = data.get('to_user', '').strip()
    try:
        amount = round(float(data.get('amount', 0)), 2)
        if math.isnan(amount) or math.isinf(amount):
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid amount'}), 400
    if amount <= 0:
        return jsonify({'error': 'Amount must be positive'}), 400
    if to_user not in g['members']:
        return jsonify({'error': 'to_user must be a group member'}), 400
    note = (data.get('note') or '').strip()[:200]
    doc = {
        'settlement_id': str(uuid.uuid4())[:10],
        'group_id': group_id,
        'from_user': username,
        'to_user': to_user,
        'amount': amount,
        'note': note,
        'settled_at': _now()
    }
    sw_settlements_col.insert_one(doc)
    send_push(to_user, {
        'title': f'✅ {username} paid you',
        'body': f'₹{amount:.2f} settled in {g["name"]}' + (f' — {note}' if note else ''),
        'url': '/splitwise',
        'tag': f'sw-settle-{group_id}'
    })
    return jsonify({'success': True, 'settlement_id': doc['settlement_id']}), 201


@splitwise_bp.route('/api/sw/groups/<group_id>/settlements', methods=['GET'])
def get_settlements(group_id):
    username = _current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    g = sw_groups_col.find_one({'group_id': group_id, 'members': username})
    if not g:
        return jsonify({'error': 'Not found'}), 404
    settlements = list(sw_settlements_col.find({'group_id': group_id}, {'_id': 0}).sort('settled_at', -1))
    return jsonify(settlements)


@splitwise_bp.route('/api/sw/groups/<group_id>/settlements/<settlement_id>', methods=['DELETE'])
def delete_settlement(group_id, settlement_id):
    username = _current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    g = sw_groups_col.find_one({'group_id': group_id, 'members': username})
    if not g:
        return jsonify({'error': 'Not found'}), 404
    s = sw_settlements_col.find_one({'settlement_id': settlement_id, 'group_id': group_id})
    if not s:
        return jsonify({'error': 'Settlement not found'}), 404
    if s['from_user'] != username and g.get('created_by') != username:
        return jsonify({'error': 'Not authorized'}), 403
    sw_settlements_col.delete_one({'settlement_id': settlement_id})
    return jsonify({'success': True})


@splitwise_bp.route('/api/sw/groups/<group_id>/settle_all', methods=['POST'])
def settle_all(group_id):
    username = _current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    
    g = sw_groups_col.find_one({'group_id': group_id, 'members': username})
    if not g:
        return jsonify({'error': 'Not found'}), 404
    
    net, transactions = _compute_balances(group_id)
    
    created = []
    for t in transactions:
        doc = {
            'settlement_id': str(uuid.uuid4())[:10],
            'group_id': group_id,
            'from_user': t['from'],
            'to_user': t['to'],
            'amount': t['amount'],
            'note': 'Settled all balances',
            'settled_at': _now()
        }
        sw_settlements_col.insert_one(doc)
        doc.pop('_id', None)
        created.append(doc)
    
    return jsonify({'success': True, 'count': len(created)})


# ── Activity Feed ────────────────────────────────────────────────────────────

@splitwise_bp.route('/api/sw/activity', methods=['GET'])
def get_activity():
    username = _current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    
    groups = list(sw_groups_col.find({'members': username}, {'_id': 0}))
    group_ids = [g['group_id'] for g in groups]
    
    if not group_ids:
        return jsonify([])
    
    expenses = list(sw_expenses_col.find(
        {'group_id': {'$in': group_ids}},
        {'_id': 0}
    ).sort('created_at', -1).limit(30))
    
    settlements = list(sw_settlements_col.find(
        {'group_id': {'$in': group_ids}},
        {'_id': 0}
    ).sort('settled_at', -1).limit(30))
    
    activities = []
    for e in expenses:
        activities.append({
            'username': e.get('created_by', e.get('paid_by', '')),
            'action': f"added expense '{e.get('description', '')}' of ₹{e.get('amount', 0)}",
            'timestamp': e.get('created_at', e.get('date', ''))
        })
    for s in settlements:
        activities.append({
            'username': s['from_user'],
            'action': f"settled ₹{s['amount']} with {s['to_user']}",
            'timestamp': s['settled_at']
        })
    
    activities.sort(key=lambda x: x['timestamp'], reverse=True)
    return jsonify(activities[:50])


# ── Mutual Followers ─────────────────────────────────────────────────────────

@splitwise_bp.route('/api/mutual_followers', methods=['GET'])
def get_mutual_followers():
    username = _current_user()
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    if follows_col is None:
        return jsonify([])
    following = {f['following'] for f in follows_col.find({'follower': username})}
    followers = {f['follower'] for f in follows_col.find({'following': username})}
    mutuals = following & followers
    return jsonify([{'username': m} for m in mutuals])