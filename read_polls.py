with open('app.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()
for i in range(2873, 2935):
    print(str(i+1) + ': ' + lines[i], end='')
