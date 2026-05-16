#!/usr/bin/env python3
"""dump_state.py — pretty-print the triage state.json.

Usage:
    python scripts/dump_state.py                  # default ./data/state.json
    python scripts/dump_state.py /path/to/state.json
    STATE_FILE=/data/state.json python scripts/dump_state.py

Shows:
    - quota usage + roll date
    - controls block (poller, kill switch, etc.)
    - top signatures by occurrence count
    - recent attempts
    - deferred queue
    - last ticket
"""

import json
import os
import sys
from datetime import datetime


def _ago(iso):
    if not iso:
        return '-'
    try:
        t = datetime.strptime(iso.replace('Z', ''), '%Y-%m-%dT%H:%M:%S')
        delta = datetime.utcnow() - t
        s = int(delta.total_seconds())
        if s < 60:
            return f'{s}s ago'
        if s < 3600:
            return f'{s // 60}m ago'
        if s < 86400:
            return f'{s // 3600}h ago'
        return f'{s // 86400}d ago'
    except Exception:
        return iso


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('STATE_FILE', './data/state.json')
    if not os.path.exists(path):
        print(f'state.json not found at {path}')
        sys.exit(1)

    with open(path) as f:
        state = json.load(f)

    print(f'=== state.json: {path} ===')
    print(f"  Quota:   {state.get('quota_used', 0)} / ? "
          f"(date={state.get('quota_date')})")

    c = state.get('controls') or {}
    print('\n--- Controls ---')
    for k in ('killSwitch', 'pollerEnabled', 'issueCreationEnabled',
              'skipProcessedDocs', 'skipKnownSignatures', 'modelOverride',
              'updatedAt', 'updatedBy'):
        print(f"  {k:<24} {c.get(k)}")

    last = state.get('lastTicket') or {}
    print('\n--- Last ticket ---')
    if last:
        print(f"  #{last.get('issueNumber')} {last.get('issueTitle') or ''}")
        print(f"  url:       {last.get('issueUrl')}")
        print(f"  docId:     {last.get('docId')}")
        print(f"  severity:  {last.get('severity')}")
        print(f"  model:     {last.get('model')}")
        print(f"  grokMs:    {last.get('grokMs')}")
        print(f"  tokens:    {(last.get('tokens') or {}).get('total')}")
        print(f"  at:        {last.get('at')} ({_ago(last.get('at'))})")
    else:
        print('  (none yet)')

    sigs = state.get('signatures') or {}
    print(f'\n--- Signatures ({len(sigs)} total) ---')
    ordered = sorted(sigs.items(), key=lambda kv: (kv[1] or {}).get('count', 0), reverse=True)
    for sig, entry in ordered[:20]:
        e = entry or {}
        print(f"  {sig[:8]}  #{e.get('issueNumber')}  count={e.get('count', 0):>3}  "
              f"sev={e.get('severity') or '-':<8} last={_ago(e.get('lastSeenAt'))}  "
              f"{(e.get('issueTitle') or '')[:60]}")
    if len(ordered) > 20:
        print(f"  ... and {len(ordered) - 20} more")

    deferred = state.get('deferred') or []
    print(f'\n--- Deferred queue ({len(deferred)}) ---')
    for d in deferred[-20:]:
        print(f"  docId={d.get('docId'):<32} reason={d.get('reason')} at={d.get('at')}")

    attempts = state.get('attempts') or []
    print(f'\n--- Recent attempts ({len(attempts)} total, showing last 15) ---')
    icon = {'created': '🟢', 'commented': '🔁', 'deferred': '⏳', 'error': '❌'}
    for a in attempts[:15]:
        outcome = a.get('outcome') or ''
        glyph = icon.get(outcome, '⚪')
        print(f"  {glyph} {outcome:<28} doc={(a.get('docId') or '')[:24]:<24} "
              f"#{a.get('issueNumber') or '-'} grokMs={a.get('grokMs') or 0:>5} "
              f"tok={(a.get('tokens') or {}).get('total', 0):>5} "
              f"{a.get('trigger', '-'):<12} {_ago(a.get('at'))}")

    stats = state.get('stats') or {}
    print('\n--- Stats ---')
    for slot in ('today', 'lifetime'):
        s = stats.get(slot) or {}
        if not s:
            continue
        print(f"  {slot:<10} attempts={s.get('attempts', 0):>4} "
              f"created={s.get('created', 0):>3} commented={s.get('commented', 0):>3} "
              f"skipped={s.get('skipped', 0):>3} deferred={s.get('deferred', 0):>3} "
              f"errors={s.get('errors', 0):>3} tokens={s.get('tokensTotal', 0)}")


if __name__ == '__main__':
    main()
