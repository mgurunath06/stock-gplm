"""Stock GPLM research application (M5.5): local, audit-first strategy discovery and rolling prediction.

The application reads every locally available Shoonya/NSE daily and minute candle under the
owner's ``OWNER_AUTHORIZED_FULL_LOCAL`` access basis (authorized 2026-11-15). The historical
M3.2 split plan and its records are untouched; its cutoff simply does not govern this workflow.

Every output is research evidence, not investment advice and never an order. See
``docs/m55-gplm-application.md``.
"""

__version__ = "0.5.5"
CLASSIFICATION = "RESEARCH_ONLY_NOT_INVESTMENT_ADVICE"
ACCESS_BASIS = "OWNER_AUTHORIZED_FULL_LOCAL"
