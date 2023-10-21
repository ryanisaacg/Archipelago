with open('Missions.csv') as f:
    data = f.read()
lines = data.split('\n')[:-1]
output = "{\n"

for line in lines:
    name, chapter, anti_shield, impact, tank, tier, misc = line.split(',')[:7]
    requirements = []
    if len(anti_shield) > 0:
        requirements.append('"competent_anti_shield"')
    if len(impact) > 0:
        requirements.append('"competent_impact"')
    if len(tank) > 0:
        requirements.append('"competent_tank"')
    if len(tier) > 0:
        requirements.append('"tier ' + tier + '"')
    if len(misc) > 0:
        requirements.append('"' + misc + '"')
    requirements = ",".join(requirements)
    template = '    { \'%s\', "mission", "%s", { %s } },\n'
    output += template % (name, chapter, requirements)

with open('arena.csv') as f:
    data = f.read()
lines = data.split('\n')[:-1]

for line in lines:
    name, rank = line.split(',')
    template = '    { \'%s\', "arena", "%s" },\n'
    output += template % (name, rank)

output += "}"
print(output)
