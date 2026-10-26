"""Fintech ledger: customers, accounts, cards, transactions, payments, budgets, goals,
alerts, security events and assistant conversations.

The DDL is a frozen snapshot of `oracle.fintech.models` at this revision; later model changes
need a new migration. On SQLite (the development default) `oracle.fintech.db.ensure_schema`
creates the same tables from the models instead.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE fin_customers (
            id VARCHAR(40) NOT NULL,
            display_name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT,
            tier VARCHAR(20) NOT NULL,
            kyc_status VARCHAR(20) NOT NULL,
            country VARCHAR(2) NOT NULL,
            base_currency VARCHAR(3) NOT NULL,
            synthetic BOOLEAN NOT NULL,
            preferences JSONB NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE fin_accounts (
            id VARCHAR(40) NOT NULL,
            customer_id VARCHAR(40) NOT NULL,
            kind VARCHAR(12) NOT NULL,
            name TEXT NOT NULL,
            currency VARCHAR(3) NOT NULL,
            balance_minor BIGINT NOT NULL,
            opening_balance_minor BIGINT NOT NULL,
            credit_limit_minor BIGINT,
            apy_bps INTEGER NOT NULL,
            status VARCHAR(12) NOT NULL,
            number_masked TEXT NOT NULL,
            opened_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT ck_fin_accounts_kind CHECK (kind IN ('checking','savings','wallet','credit')),
            CONSTRAINT ck_fin_accounts_status CHECK (status IN ('open','frozen','closed')),
            FOREIGN KEY(customer_id) REFERENCES fin_customers (id)
        )
        """
    )
    op.execute("CREATE INDEX ix_fin_accounts_customer_id ON fin_accounts (customer_id)")
    op.execute(
        """
        CREATE TABLE fin_alerts (
            id VARCHAR(40) NOT NULL,
            customer_id VARCHAR(40) NOT NULL,
            kind VARCHAR(20) NOT NULL,
            severity VARCHAR(10) NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            subject_type VARCHAR(20),
            subject_id VARCHAR(40),
            status VARCHAR(10) NOT NULL,
            resolution VARCHAR(12),
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            resolved_at TIMESTAMP WITH TIME ZONE,
            PRIMARY KEY (id),
            CONSTRAINT ck_fin_alerts_severity CHECK (severity IN ('info','warning','critical')),
            FOREIGN KEY(customer_id) REFERENCES fin_customers (id)
        )
        """
    )
    op.execute("CREATE INDEX ix_fin_alerts_created_at ON fin_alerts (created_at)")
    op.execute("CREATE INDEX ix_fin_alerts_customer_id ON fin_alerts (customer_id)")
    op.execute(
        """
        CREATE TABLE fin_budgets (
            id VARCHAR(40) NOT NULL,
            customer_id VARCHAR(40) NOT NULL,
            category VARCHAR(20) NOT NULL,
            monthly_limit_minor BIGINT NOT NULL,
            currency VARCHAR(3) NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_fin_budgets UNIQUE (customer_id, category),
            FOREIGN KEY(customer_id) REFERENCES fin_customers (id)
        )
        """
    )
    op.execute("CREATE INDEX ix_fin_budgets_customer_id ON fin_budgets (customer_id)")
    op.execute(
        """
        CREATE TABLE fin_conversations (
            id VARCHAR(40) NOT NULL,
            customer_id VARCHAR(40) NOT NULL,
            title TEXT NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY(customer_id) REFERENCES fin_customers (id)
        )
        """
    )
    op.execute("CREATE INDEX ix_fin_conversations_customer_id ON fin_conversations (customer_id)")
    op.execute(
        """
        CREATE TABLE fin_payees (
            id VARCHAR(40) NOT NULL,
            customer_id VARCHAR(40) NOT NULL,
            name TEXT NOT NULL,
            kind VARCHAR(12) NOT NULL,
            account_masked TEXT NOT NULL,
            bank_name TEXT NOT NULL,
            category VARCHAR(20) NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY(customer_id) REFERENCES fin_customers (id)
        )
        """
    )
    op.execute("CREATE INDEX ix_fin_payees_customer_id ON fin_payees (customer_id)")
    op.execute(
        """
        CREATE TABLE fin_security_events (
            id SERIAL NOT NULL,
            customer_id VARCHAR(40) NOT NULL,
            occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            subject_type VARCHAR(20) NOT NULL,
            subject_id VARCHAR(40),
            details JSONB NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY(customer_id) REFERENCES fin_customers (id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_fin_security_events_customer_id ON fin_security_events (customer_id)"
    )
    op.execute(
        "CREATE INDEX ix_fin_security_events_occurred_at ON fin_security_events (occurred_at)"
    )
    op.execute(
        """
        CREATE TABLE fin_cards (
            id VARCHAR(40) NOT NULL,
            customer_id VARCHAR(40) NOT NULL,
            account_id VARCHAR(40) NOT NULL,
            cardholder_name TEXT NOT NULL,
            nickname TEXT NOT NULL,
            network VARCHAR(20) NOT NULL,
            product VARCHAR(20) NOT NULL,
            form VARCHAR(12) NOT NULL,
            last4 VARCHAR(4) NOT NULL,
            token TEXT NOT NULL,
            exp_month INTEGER NOT NULL,
            exp_year INTEGER NOT NULL,
            status VARCHAR(12) NOT NULL,
            design VARCHAR(20) NOT NULL,
            issuer TEXT NOT NULL,
            allow_online BOOLEAN NOT NULL,
            allow_contactless BOOLEAN NOT NULL,
            allow_international BOOLEAN NOT NULL,
            allow_atm BOOLEAN NOT NULL,
            blocked_categories JSONB NOT NULL,
            limit_single_minor BIGINT,
            limit_daily_minor BIGINT,
            limit_monthly_minor BIGINT,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            cancelled_at TIMESTAMP WITH TIME ZONE,
            PRIMARY KEY (id),
            CONSTRAINT ck_fin_cards_form CHECK (form IN ('physical','virtual','single_use')),
            CONSTRAINT ck_fin_cards_status CHECK (status IN ('active','frozen','cancelled')),
            FOREIGN KEY(customer_id) REFERENCES fin_customers (id),
            FOREIGN KEY(account_id) REFERENCES fin_accounts (id),
            UNIQUE (token)
        )
        """
    )
    op.execute("CREATE INDEX ix_fin_cards_account_id ON fin_cards (account_id)")
    op.execute("CREATE INDEX ix_fin_cards_customer_id ON fin_cards (customer_id)")
    op.execute(
        """
        CREATE TABLE fin_goals (
            id VARCHAR(40) NOT NULL,
            customer_id VARCHAR(40) NOT NULL,
            account_id VARCHAR(40),
            name TEXT NOT NULL,
            icon VARCHAR(20) NOT NULL,
            target_minor BIGINT NOT NULL,
            saved_minor BIGINT NOT NULL,
            currency VARCHAR(3) NOT NULL,
            deadline TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            completed_at TIMESTAMP WITH TIME ZONE,
            PRIMARY KEY (id),
            FOREIGN KEY(customer_id) REFERENCES fin_customers (id),
            FOREIGN KEY(account_id) REFERENCES fin_accounts (id)
        )
        """
    )
    op.execute("CREATE INDEX ix_fin_goals_customer_id ON fin_goals (customer_id)")
    op.execute(
        """
        CREATE TABLE fin_messages (
            id SERIAL NOT NULL,
            conversation_id VARCHAR(40) NOT NULL,
            role VARCHAR(10) NOT NULL,
            content TEXT NOT NULL,
            meta JSONB NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY(conversation_id) REFERENCES fin_conversations (id)
        )
        """
    )
    op.execute("CREATE INDEX ix_fin_messages_conversation_id ON fin_messages (conversation_id)")
    op.execute(
        """
        CREATE TABLE fin_payments (
            id VARCHAR(40) NOT NULL,
            customer_id VARCHAR(40) NOT NULL,
            from_account_id VARCHAR(40) NOT NULL,
            to_account_id VARCHAR(40),
            payee_id VARCHAR(40),
            amount_minor BIGINT NOT NULL,
            currency VARCHAR(3) NOT NULL,
            status VARCHAR(12) NOT NULL,
            rail VARCHAR(20) NOT NULL,
            memo TEXT NOT NULL,
            scheduled_for TIMESTAMP WITH TIME ZONE NOT NULL,
            recurrence VARCHAR(10) NOT NULL,
            last_executed_at TIMESTAMP WITH TIME ZONE,
            executions INTEGER NOT NULL,
            failure_reason TEXT,
            rail_reference TEXT,
            idempotency_key TEXT,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT ck_fin_payments_status CHECK (status IN ('scheduled','completed','failed','cancelled')),
            CONSTRAINT ck_fin_payments_amount CHECK (amount_minor > 0),
            FOREIGN KEY(customer_id) REFERENCES fin_customers (id),
            FOREIGN KEY(from_account_id) REFERENCES fin_accounts (id),
            FOREIGN KEY(to_account_id) REFERENCES fin_accounts (id),
            FOREIGN KEY(payee_id) REFERENCES fin_payees (id),
            UNIQUE (idempotency_key)
        )
        """
    )
    op.execute("CREATE INDEX ix_fin_payments_customer_id ON fin_payments (customer_id)")
    op.execute(
        """
        CREATE TABLE fin_transactions (
            id VARCHAR(40) NOT NULL,
            customer_id VARCHAR(40) NOT NULL,
            account_id VARCHAR(40) NOT NULL,
            card_id VARCHAR(40),
            payment_id VARCHAR(40),
            kind VARCHAR(20) NOT NULL,
            status VARCHAR(12) NOT NULL,
            amount_minor BIGINT NOT NULL,
            currency VARCHAR(3) NOT NULL,
            original_amount_minor BIGINT,
            original_currency VARCHAR(3),
            merchant_name TEXT NOT NULL,
            mcc INTEGER,
            category VARCHAR(20) NOT NULL,
            category_source VARCHAR(10) NOT NULL,
            channel VARCHAR(12) NOT NULL,
            country VARCHAR(2) NOT NULL,
            city TEXT,
            description TEXT NOT NULL,
            counterparty TEXT,
            occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
            posted_at TIMESTAMP WITH TIME ZONE,
            balance_after_minor BIGINT,
            fraud_score FLOAT,
            fraud_signals JSONB NOT NULL,
            decline_reason TEXT,
            idempotency_key TEXT,
            note TEXT,
            synthetic BOOLEAN NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT ck_fin_txn_kind CHECK (kind IN ('card_purchase','atm_withdrawal','deposit','interest','refund','fee','transfer_in','transfer_out','bill_payment')),
            CONSTRAINT ck_fin_txn_status CHECK (status IN ('pending','posted','declined','reversed')),
            FOREIGN KEY(customer_id) REFERENCES fin_customers (id),
            FOREIGN KEY(account_id) REFERENCES fin_accounts (id),
            FOREIGN KEY(card_id) REFERENCES fin_cards (id),
            UNIQUE (idempotency_key)
        )
        """
    )
    op.execute("CREATE INDEX ix_fin_transactions_account_id ON fin_transactions (account_id)")
    op.execute("CREATE INDEX ix_fin_transactions_card_id ON fin_transactions (card_id)")
    op.execute("CREATE INDEX ix_fin_transactions_category ON fin_transactions (category)")
    op.execute("CREATE INDEX ix_fin_transactions_customer_id ON fin_transactions (customer_id)")
    op.execute("CREATE INDEX ix_fin_transactions_occurred_at ON fin_transactions (occurred_at)")
    op.execute("CREATE INDEX ix_fin_transactions_payment_id ON fin_transactions (payment_id)")
    op.execute(
        """
        CREATE FUNCTION fin_security_events_reject_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'fin_security_events is append-only (% rejected)', TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER fin_security_events_no_update_delete BEFORE UPDATE OR DELETE ON "
        "fin_security_events FOR EACH ROW EXECUTE FUNCTION fin_security_events_reject_mutation();"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS fin_security_events_no_update_delete ON fin_security_events;"
    )
    op.execute("DROP FUNCTION IF EXISTS fin_security_events_reject_mutation();")
    op.execute("DROP TABLE IF EXISTS fin_transactions")
    op.execute("DROP TABLE IF EXISTS fin_payments")
    op.execute("DROP TABLE IF EXISTS fin_messages")
    op.execute("DROP TABLE IF EXISTS fin_goals")
    op.execute("DROP TABLE IF EXISTS fin_cards")
    op.execute("DROP TABLE IF EXISTS fin_security_events")
    op.execute("DROP TABLE IF EXISTS fin_payees")
    op.execute("DROP TABLE IF EXISTS fin_conversations")
    op.execute("DROP TABLE IF EXISTS fin_budgets")
    op.execute("DROP TABLE IF EXISTS fin_alerts")
    op.execute("DROP TABLE IF EXISTS fin_accounts")
    op.execute("DROP TABLE IF EXISTS fin_customers")
