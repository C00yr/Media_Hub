# Use a bounded workflow for cross-source media search

Cross-source requests are executed by a composite workflow that discovers TMDB candidates, searches M-Team incrementally, applies strict canonical constraints, and reports completeness. The language model chooses and may revise the workflow inputs, but it does not manually evaluate every raw provider row. This trades some low-level model freedom for reproducible filtering, controllable request budgets, and explainable partial results.
