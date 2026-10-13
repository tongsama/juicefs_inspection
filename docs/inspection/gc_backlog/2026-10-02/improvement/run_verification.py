#!/usr/bin/env python3
"""Run isolated verification while preserving the shared MemKV test setting."""
import fcntl, json, os, pathlib, subprocess, sys, time
root = pathlib.Path('/home/kwatanabe/tmp_local/juicefs_inspection')
artifacts = root / 'gc_backlog/2026-10-02/improvement'
label = sys.argv[1]
args = sys.argv[2:]
if args and args[0] == '--':
    args = args[1:]
with open('/tmp/juicefs-inspection-tests.lock', 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    setting = pathlib.Path('/tmp/juicefs.memkv.setting.json')
    old = setting.read_bytes() if setting.exists() else None
    start = time.monotonic()
    try:
        with (artifacts / (label + '.log')).open('wb') as output:
            result = subprocess.run(args, cwd=root / 'juicefs', env=os.environ.copy(), stdout=output, stderr=subprocess.STDOUT)
    finally:
        if old is None:
            setting.unlink(missing_ok=True)
        else:
            setting.write_bytes(old)
    record = {'command': args, 'exit_code': result.returncode, 'wall_seconds': time.monotonic()-start}
    (artifacts / (label + '.json')).write_text(json.dumps(record, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(record, ensure_ascii=False))
    print((artifacts / (label+'.log')).read_text()[-5000:])
    sys.exit(result.returncode)
