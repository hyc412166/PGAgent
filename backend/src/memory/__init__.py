"""Two-phase persistent memory with model routing and usage feedback.

Boundaries:
- :mod:`extraction` filters one accepted rollout and parses Phase-1 evidence.
- :mod:`consolidation` defines and validates Phase-2 handbook operations.
- :mod:`pipeline` owns durable asynchronous model jobs.
- :mod:`protocol` owns provider index/citation framing.
- :mod:`repository` owns citation usage and index persistence queries.
- :mod:`service` remains the compatibility facade for memory CRUD/tools and
  deterministic Markdown projection.
"""
