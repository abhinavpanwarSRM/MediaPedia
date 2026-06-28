"""One-time script to seed all KOC editions with correct R1/R2 scores from the original source."""
import os
from datetime import datetime, timezone
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI"))
col = client["mediapedia"].koc_tournaments

KOC_DISPLAY = {
    'abhinav':  'ABHINAV PANWAR',
    'abhishek': 'ABHISHEK SEHRAWAT',
    'akhil':    'AKHIL PANWAR',
    'sahil':    'SAHIL PANWAR',
    'shruti':   'SHRUTI SEHRAWAT',
    'utkarsh':  'UTKARSH PANWAR',
}

# Each row: (username, r1_mono, r1_bluff, r1_spoon, r1_uno, r2_mono, r2_bluff, r2_spoon, r2_uno)
EDITIONS = [
    {
        'edition': 1,
        'played_on': '2021-12-26',
        'players': [
            ('abhinav',  0,0,1,1, 0,0,0,0),
            ('abhishek', 0,0,2,0, 2,1,0,2),
            ('sahil',    0,2,0,0, 0,2,1,0),
            ('shruti',   2,1,0,0, 0,0,2,1),
            ('utkarsh',  1,0,0,2, 1,0,0,0),
        ]
    },
    {
        'edition': 2,
        'played_on': '2022-03-19',
        'players': [
            ('abhinav',  0,2,1,2, 0,0,0,1),
            ('abhishek', 0,0,0,0, 0,1,2,0),
            ('sahil',    0,0,2,0, 2,0,0,0),
            ('shruti',   1,0,0,1, 1,0,1,0),
            ('utkarsh',  2,1,0,0, 0,2,0,2),
        ]
    },
    {
        'edition': 3,
        'played_on': '2022-08-07',
        'players': [
            ('abhinav',  0,0,0,2, 2,0,0,0),
            ('abhishek', 1,0,0,0, 0,0,1,2),
            ('akhil',    0,0,0,0, 0,0,0,0),
            ('sahil',    0,1,1,0, 0,0,0,0),
            ('shruti',   0,0,2,0, 1,2,0,1),
            ('utkarsh',  2,2,0,1, 0,1,2,0),
        ]
    },
    {
        'edition': 4,
        'played_on': '2023-01-02',
        'players': [
            ('abhinav',  1,0,2,2, 2,0,2,2),
            ('abhishek', 2,0,0,1, 0,2,0,0),
            ('sahil',    0,2,0,0, 0,1,0,1),
            ('shruti',   0,0,1,0, 0,0,1,0),
            ('utkarsh',  0,1,0,0, 1,0,0,0),
        ]
    },
    {
        'edition': 5,
        'played_on': '2025-07-19',
        'players': [
            ('abhinav',  2,0,2,2, 1,0,1,2),
            ('abhishek', 0,2,0,1, 0,0,0,1),
            ('akhil',    0,0,1,0, 0,1,0,0),
            ('shruti',   0,0,0,0, 2,0,2,0),
            ('utkarsh',  1,1,0,0, 0,2,0,0),
        ]
    },
]

def _wins(p):
    return sum(1 for g in ('monopoly','bluff','spoon','uno') if p.get(g,0) >= 2)

for ed in EDITIONS:
    players = []
    for row in ed['players']:
        u, r1m, r1b, r1s, r1u, r2m, r2b, r2s, r2u = row
        mono  = r1m + r2m
        bluff = r1b + r2b
        spoon = r1s + r2s
        uno   = r1u + r2u
        total = mono + bluff + spoon + uno
        players.append({
            'username': u,
            'display_name': KOC_DISPLAY[u],
            'r1_monopoly': r1m, 'r2_monopoly': r2m, 'monopoly': mono,
            'r1_bluff':    r1b, 'r2_bluff':    r2b, 'bluff':    bluff,
            'r1_spoon':    r1s, 'r2_spoon':    r2s, 'spoon':    spoon,
            'r1_uno':      r1u, 'r2_uno':      r2u, 'uno':      uno,
            'total': total
        })
    winner = max(players, key=lambda p: (p['total'], _wins(p)))
    col.update_one(
        {'edition': ed['edition']},
        {'$set': {
            'played_on': ed['played_on'],
            'players': players,
            'winner_username': winner['username'],
            'updated_at': datetime.now(timezone.utc).isoformat()
        }},
        upsert=True
    )
    print(f"Edition {ed['edition']} - winner: {winner['username']} ({winner['total']} pts)")

print("Done.")
