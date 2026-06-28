from app import app, users_collection, bcrypt
from datetime import datetime, timezone

players = [
    {'username': 'abhishek', 'password': 'koc@abhishek', 'bio': 'King of Cards player - ABHISHEK SEHRAWAT'},
    {'username': 'sahil',    'password': 'koc@sahil',    'bio': 'King of Cards player - SAHIL PANWAR'},
    {'username': 'utkarsh',  'password': 'koc@utkarsh',  'bio': 'King of Cards player - UTKARSH PANWAR'},
]

with app.app_context():
    for p in players:
        if users_collection.find_one({'username': p['username']}):
            print('EXISTS:', p['username'])
        else:
            hashed = bcrypt.generate_password_hash(p['password']).decode('utf-8')
            users_collection.insert_one({
                'username': p['username'], 'password': hashed,
                'bio': p['bio'], 'created_at': datetime.now(timezone.utc)
            })
            print('CREATED:', p['username'])

    print('\n--- All 6 KOC players ---')
    for u in ['abhinav', 'abhishek', 'akhil', 'sahil', 'shruti', 'utkarsh']:
        doc = users_collection.find_one({'username': u}, {'_id': 0, 'username': 1})
        print('OK' if doc else 'MISSING', '-', u)
