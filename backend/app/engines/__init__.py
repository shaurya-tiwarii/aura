"""Genuine analysis engines (spec sections 36/37).

These are AURA's real application logic -- the SEO scorer, the AI-visibility
heuristic, the recommendation engine -- not thin wrappers around a language
model. Each engine takes the crawler's persisted evidence and produces an
explainable result where every number traces back to a documented rule. Gemini
is never called from here; generation and rewriting live in the service layer.
"""
