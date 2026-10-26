"""Banking domain: accounts, multi-currency wallets, cards, payments, analytics and insights.

Card issuing and payment rails run against sandbox providers only (`providers.py`); no real
card is issued and no real money moves. The money path (`ledger`, `authorization`, `fraud`)
never imports the AI assistant (import contract in `.importlinter`).
"""
