PRAGMA foreign_keys = ON;

CREATE TABLE contracts (
    contract_id INTEGER PRIMARY KEY,
    contract_name TEXT NOT NULL,
    currency TEXT NOT NULL,
    tenant_id TEXT NOT NULL
);

CREATE TABLE invoices (
    invoice_id TEXT PRIMARY KEY,
    contract_id INTEGER NOT NULL REFERENCES contracts(contract_id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    due_date TEXT NOT NULL,
    paid INTEGER NOT NULL CHECK (paid IN (0, 1)),
    tenant_id TEXT NOT NULL,
    reference TEXT NOT NULL UNIQUE
);

INSERT INTO contracts (contract_id, contract_name, currency, tenant_id) VALUES
    (1, 'Northwind Services', 'USD', 'tenant-a'),
    (2, 'Fabrikam Operations', 'USD', 'tenant-a'),
    (3, 'Contoso Internal', 'USD', 'tenant-b');

INSERT INTO invoices (
    invoice_id, contract_id, amount_cents, due_date, paid, tenant_id, reference
) VALUES
    ('INV-1001', 1, 125000, '2026-01-15', 0, 'tenant-a', 'NW-2026-01'),
    ('INV-1002', 1, 25000, '2026-02-01', 0, 'tenant-a', 'NW-2026-02'),
    ('INV-2001', 2, 90000, '2026-02-10', 0, 'tenant-a', 'FK-2026-02'),
    ('INV-2002', 2, 50000, '2026-02-20', 1, 'tenant-a', 'FK-2026-01'),
    ('INV-1003', 1, 70000, '2026-03-15', 0, 'tenant-a', 'NW-2026-03'),
    ('INV-9001', 3, 9000000, '2026-01-01', 0, 'tenant-b', 'CT-2026-01');
