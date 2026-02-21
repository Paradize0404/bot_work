with open('bot/handlers.py', 'rb') as f:
    lines = f.readlines()

new_lines = []
for i, line in enumerate(lines):
    new_lines.append(line)
    if i == 131:
        new_lines.append(b'        [KeyboardButton(text="\xf0\x9f\x8d\xb0 \xd0\x93\xd1\x80\xd1\x83\xd0\xbf\xd0\xbf\xd1\x8b \xd0\xba\xd0\xbe\xd0\xbd\xd0\xb4\xd0\xb8\xd1\x82\xd0\xb5\xd1\x80\xd0\xbe\xd0\xb2")],\n')

with open('bot/handlers.py', 'wb') as f:
    f.writelines(new_lines)
