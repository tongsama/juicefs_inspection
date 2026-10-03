"""Summarize health evidence from a fixed byte prefix without changing the live log."""
from pathlib import Path
from collections import Counter, defaultdict
import re, json, hashlib
SOURCE = Path('/home/kwatanabe/.juicefs/diagnostics/vm-io-20261002-014922.log')
PREFIX = 179156595

def rows():
    """Read complete recorded lines within the common analysis boundary."""
    with SOURCE.open('rb') as stream:
        remaining = PREFIX
        while remaining:
            line = stream.readline(remaining)
            if not line:
                raise EOFError('Source became shorter than the recorded prefix')
            remaining -= len(line)
            yield line

counts = Counter()
samples = defaultdict(list)
levels = Counter()
versions = []
first = last = None
sha = hashlib.sha256()
for raw in rows():
    sha.update(raw)
    line = raw.decode('utf-8', errors='replace').rstrip()
    if re.match(r'\d{4}/\d\d/\d\d ', line):
        first = first or line[:26]
        last = line[:26]
    if 'JuiceFS version' in line:
        versions.append(line)
    level = re.search(r'<(\w+)>:', line)
    if level:
        levels[level[1]] += 1
    labels = []
    if level and level[1] in ('ERROR', 'FATAL', 'PANIC'):
        labels.append('error_level')
    if level and level[1] == 'WARNING':
        label = 'transaction_retry_succeeded' if 'Transaction succeeded after' in line else 'read_context_canceled' if 'context canceled' in line else next((key for key in ('slow slice commit', 'slow metadata write', 'slow metadata transaction', 'slow compaction', 'slow request', 'still waiting', 'upload it directly', 'not enough space', 'failed', 'timeout') if key in line), 'other_warning')
        labels.append(label)
    if 'slow operation' in line:
        labels.append('slow_operation')
    if re.search(r'\b(errno|err)=(?:input/output error|no space left on device|disk quota exceeded)|\bEIO\b|flush \d+ timeout', line):
        labels.append('explicit_io_error_or_flush_timeout')
    if 'Found staging block:' in line:
        labels.append('startup_stage_block')
    for label in labels:
        counts[label] += 1
        if len(samples[label]) < 8:
            samples[label].append(line)
        elif label in ('error_level', 'explicit_io_error_or_flush_timeout'):
            samples[label][-1] = line
out = dict(path=str(SOURCE), prefix_bytes=PREFIX, sha256=sha.hexdigest(), first=first, last=last, levels=levels, counts=counts, samples=samples, versions=versions)
Path(__file__).with_name('health.json').write_text(json.dumps(out, indent=2, ensure_ascii=False))
print(json.dumps(out, indent=2, ensure_ascii=False))
