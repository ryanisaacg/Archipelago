with open('Items.csv') as f:
    data = f.read()
lines = data.split('\n')[:-1]

output = '[\n'

prev_name = ''
name_idx = 2

for line in lines:
    name, shield, impact, tank, tier = line.split(',')
    if name == prev_name:
        name += ' ' + str(name_idx)
        name_idx += 1
    else:
        prev_name = name
        name_idx = 2

    output += '    ["%s", %r, %r, %r, %s],\n' % (name, bool(shield), bool(impact), bool(tank), tier)

output += ']'
print(output)
