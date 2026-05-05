"""Built-in autonomous watchers — MIGRATED.

All watchers have been migrated to their respective skill files and are
now registered automatically via the register_watchers(runtime) hook in
skills/__init__.py load_skills():

  checkin_atom_watcher  — skills/behavioral_atoms.py   (every 5 min)
  crisis_scan_watcher   — skills/crisis_escalation.py  (every 60 min)
  care_gap_watcher      — skills/care_gap.py            (every 24 h)

This file is retained as an empty shell to preserve import compatibility
with any external references.  It may be removed once all callers are updated.
"""
