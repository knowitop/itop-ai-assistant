"""Content providers — tickets, FAQ articles — that speak `VectorSource`
(`vector/ports/source.py`) so `vector/`'s indexer can sweep them.

This is the one place iTop domain knowledge (the family schemas, their
repositories) meets the vector subsystem's contract. `vector/` itself must
never import from here (rule 6.4) — the dependency runs one way, source to
protocol, not back.
"""
