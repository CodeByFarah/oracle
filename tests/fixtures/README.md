# Test fixtures

These are **hand-constructed sample responses**. They follow the documented response formats
of FRED (`fred/series/observations`, `file_type=json`) and SEC EDGAR (`data.sec.gov/submissions`).
Identifiers and values are fake (`TESTSERIES`, CIK `0000000001`, "Example Test Registrant").
They are **not market data** and must never be used as evidence.

They test format parsing, pagination, missing-value handling and timestamp rules without
network access or credentials.

To replace them with genuine recorded responses later, ingest once with a free FRED key or a
configured SEC User-Agent. Then copy the payload from `data/raw/<provider>/...` and keep the
fake-identifier versions alongside it. Real vendor payloads must stay out of git if their
licence forbids redistribution. FRED series notes and SEC fair-access terms apply.
