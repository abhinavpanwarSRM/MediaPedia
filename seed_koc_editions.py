from app import app, users_collection, koc_tournaments_collection
from datetime import datetime, timezone

# Scores are sum of both rounds per game (monopoly1+monopoly2, etc.)
PAST_EDITIONS = [
    {
        'edition': 1, 'played_on': '2021-12-26',
        'winner_username': 'abhishek',
        'players': [
            {'username':'abhishek','display_name':'ABHISHEK SEHRAWAT','monopoly':0+2,'bluff':0+1,'spoon':2+0,'uno':0+2,'total':7},
            {'username':'shruti',  'display_name':'SHRUTI SEHRAWAT',  'monopoly':2+0,'bluff':1+0,'spoon':0+2,'uno':0+1,'total':6},
            {'username':'sahil',   'display_name':'SAHIL PANWAR',     'monopoly':0+0,'bluff':2+2,'spoon':0+1,'uno':0+0,'total':5},
            {'username':'utkarsh', 'display_name':'UTKARSH PANWAR',   'monopoly':1+1,'bluff':0+0,'spoon':0+0,'uno':2+0,'total':4},
            {'username':'abhinav', 'display_name':'ABHINAV PANWAR',   'monopoly':0+0,'bluff':0+0,'spoon':1+0,'uno':1+0,'total':2},
        ]
    },
    {
        'edition': 2, 'played_on': '2022-03-19',
        'winner_username': 'utkarsh',
        'players': [
            {'username':'utkarsh', 'display_name':'UTKARSH PANWAR',   'monopoly':2+0,'bluff':1+2,'spoon':0+0,'uno':0+2,'total':7},
            {'username':'abhinav', 'display_name':'ABHINAV PANWAR',   'monopoly':0+0,'bluff':2+0,'spoon':1+0,'uno':2+1,'total':6},
            {'username':'sahil',   'display_name':'SAHIL PANWAR',     'monopoly':0+2,'bluff':0+0,'spoon':2+0,'uno':0+0,'total':4},
            {'username':'shruti',  'display_name':'SHRUTI SEHRAWAT',  'monopoly':1+1,'bluff':0+0,'spoon':0+1,'uno':1+0,'total':4},
            {'username':'abhishek','display_name':'ABHISHEK SEHRAWAT','monopoly':0+0,'bluff':0+1,'spoon':0+2,'uno':0+0,'total':3},
        ]
    },
    {
        'edition': 3, 'played_on': '2022-08-07',
        'winner_username': 'utkarsh',
        'players': [
            {'username':'utkarsh', 'display_name':'UTKARSH PANWAR',   'monopoly':2+0,'bluff':2+1,'spoon':0+2,'uno':1+0,'total':8},
            {'username':'shruti',  'display_name':'SHRUTI SEHRAWAT',  'monopoly':0+1,'bluff':0+2,'spoon':2+0,'uno':0+1,'total':6},
            {'username':'abhinav', 'display_name':'ABHINAV PANWAR',   'monopoly':0+2,'bluff':0+0,'spoon':0+0,'uno':2+0,'total':4},
            {'username':'abhishek','display_name':'ABHISHEK SEHRAWAT','monopoly':1+0,'bluff':0+0,'spoon':0+1,'uno':0+2,'total':4},
            {'username':'sahil',   'display_name':'SAHIL PANWAR',     'monopoly':0+0,'bluff':1+0,'spoon':1+0,'uno':0+0,'total':2},
            {'username':'akhil',   'display_name':'AKHIL PANWAR',     'monopoly':0+0,'bluff':0+0,'spoon':0+0,'uno':0+0,'total':0},
        ]
    },
    {
        'edition': 4, 'played_on': '2023-01-02',
        'winner_username': 'abhinav',
        'players': [
            {'username':'abhinav', 'display_name':'ABHINAV PANWAR',   'monopoly':1+2,'bluff':0+0,'spoon':2+2,'uno':2+2,'total':11},
            {'username':'abhishek','display_name':'ABHISHEK SEHRAWAT','monopoly':2+0,'bluff':0+2,'spoon':0+0,'uno':1+0,'total':5},
            {'username':'sahil',   'display_name':'SAHIL PANWAR',     'monopoly':0+0,'bluff':2+1,'spoon':0+0,'uno':0+1,'total':4},
            {'username':'shruti',  'display_name':'SHRUTI SEHRAWAT',  'monopoly':0+0,'bluff':0+0,'spoon':1+1,'uno':0+0,'total':2},
            {'username':'utkarsh', 'display_name':'UTKARSH PANWAR',   'monopoly':0+1,'bluff':1+0,'spoon':0+0,'uno':0+0,'total':2},
        ]
    },
    {
        'edition': 5, 'played_on': '2025-07-19',
        'winner_username': 'abhinav',
        'players': [
            {'username':'abhinav', 'display_name':'ABHINAV PANWAR',   'monopoly':2+1,'bluff':0+0,'spoon':2+1,'uno':2+2,'total':10},
            {'username':'shruti',  'display_name':'SHRUTI SEHRAWAT',  'monopoly':0+2,'bluff':0+0,'spoon':0+2,'uno':0+0,'total':4},
            {'username':'abhishek','display_name':'ABHISHEK SEHRAWAT','monopoly':0+0,'bluff':2+0,'spoon':0+0,'uno':1+1,'total':4},
            {'username':'utkarsh', 'display_name':'UTKARSH PANWAR',   'monopoly':1+0,'bluff':1+2,'spoon':0+0,'uno':0+0,'total':4},
            {'username':'akhil',   'display_name':'AKHIL PANWAR',     'monopoly':0+0,'bluff':0+1,'spoon':1+0,'uno':0+0,'total':2},
        ]
    },
]

with app.app_context():
    inserted, skipped = 0, 0
    for ed in PAST_EDITIONS:
        if koc_tournaments_collection.find_one({'edition': ed['edition']}):
            print(f'SKIP  edition {ed["edition"]}')
            skipped += 1
            continue
        ed['added_by'] = 'abhinav'
        ed['created_at'] = datetime.now(timezone.utc).isoformat()
        koc_tournaments_collection.insert_one(ed)
        print(f'INSERT edition {ed["edition"]} — winner: {ed["winner_username"]}')
        inserted += 1

    print(f'\nEditions: {inserted} inserted, {skipped} skipped')

    # Clean up stale accounts
    for u in ['a3h1', 'sahilp', 'utkarshp']:
        r = users_collection.delete_one({'username': u})
        print(f'DELETED {u}' if r.deleted_count else f'NOT FOUND {u}')

    print('\nDONE')
