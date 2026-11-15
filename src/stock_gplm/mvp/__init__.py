"""Five-stock diagnostic MVP: config-driven V1 strategies over pre-cutoff daily data.

Composes existing components (``ingestion.daily`` integrity checks, the V1 schema,
evaluator, portfolio simulator and metrics) and adds only data pinning, horizon
variants, batching and deterministic reporting. Results are
``DIAGNOSTIC_ONLY_NON_INVESTABLE``; nothing here ranks, promotes or trains.
"""
