"""rclone backend query で取得した全フォルダから、/rclone-s3 配下の同名フォルダの重複を集計する。"""
import json, collections, hashlib, sys
data = json.load(open('folders.json'))
by_id = {f['id']: f for f in data}
children = collections.defaultdict(list)
for f in data:
    for p in f.get('parents') or []:
        children[p].append(f)
roots = [f for f in data if f['name'] == 'rclone-s3']
print('folders total', len(data), 'rclone-s3 candidates', len(roots))
for r in roots:
    print(' root', r['id'][:6]+'…', 'parents', [by_id.get(p,{}).get('name','<root>') for p in r.get('parents') or []], r.get('createdTime'))
# 配下を BFS し、(親, 名前) の重複を数える
for r in roots:
    stack=[(r,'rclone-s3')]; n=0; dups=[]; depth=collections.Counter()
    while stack:
        f,path=stack.pop(); n+=1
        groups=collections.defaultdict(list)
        for c in children[f['id']]:
            groups[c['name']].append(c)
        for name,cs in groups.items():
            if len(cs)>1:
                dups.append((path+'/'+name, sorted(c['createdTime'] for c in cs), len(cs)))
            for c in cs:
                depth[(path+'/'+name).count('/')]+=1
                stack.append((c,path+'/'+name))
    print('under', r['id'][:6]+'…', 'folders', n, 'by depth', dict(sorted(depth.items())), 'duplicate names', len(dups))
    for d in sorted(dups)[:50]:
        print('  DUP', d)
