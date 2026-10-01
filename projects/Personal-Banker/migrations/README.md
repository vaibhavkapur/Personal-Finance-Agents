# Migrations

The prototype creates its schema at startup with SQLAlchemy `create_all`
(SQLite by default). The numbered SQL files here are the reviewable equivalent
for PostgreSQL and SQLite and are generated from `backend/app/persistence/models.py`:

```bash
cd backend && python scripts/dump_schema.py
```

`0001_initial_*.sql` is the full initial schema. Once a deployment carries
real data, add incremental `000N_*.sql` files (or adopt Alembic) rather than
regenerating the initial file. Conventions enforced by the schema:

- fiat amounts are integer minor units with an explicit currency column;
- rates are fixed-precision decimal strings;
- timestamps are ISO-8601 UTC strings (identical on SQLite and PostgreSQL);
- provider/event references are unique (`inbox_events(provider_id, event_id)`,
  `actions.idempotency_key`, `actions.request_ref`, `mock_bank_instructions.provider_reference`);
- case events are unique per `(case_id, sequence)`.
