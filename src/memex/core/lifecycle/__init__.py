"""
Memory lifecycle: encoding → consolidation → decay → verification → forgetting.

These modules are the seams forced by axiom 3 (memory has a lifecycle).
v0.1 ships the schema seams + a minimal decay implementation; the heavier
passes (consolidation, code-verification, active forgetting) land in v0.4-v0.5.
Keeping the file structure now means we won't retrofit later.
"""
