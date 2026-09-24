-- Estructura de tablas para el reto SmartBancs
CREATE TABLE IF NOT EXISTS accounts (
    account_number VARCHAR(20) PRIMARY KEY,
    owner_name VARCHAR(100) NOT NULL,
    balance NUMERIC(12, 2) NOT NULL CHECK (balance >= 0),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id VARCHAR(36) PRIMARY KEY,
    source_account VARCHAR(20) REFERENCES accounts(account_number),
    destination_account VARCHAR(20) REFERENCES accounts(account_number),
    amount NUMERIC(12, 2) NOT NULL,
    status VARCHAR(20) NOT NULL,
    latency_ms NUMERIC(8, 2),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS transaction_attempts (
    id SERIAL PRIMARY KEY,
    transaction_id VARCHAR(36),
    outcome VARCHAR(30) NOT NULL,
    error_class VARCHAR(10) NOT NULL,
    latency_ms NUMERIC(8, 2),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ai_recommendations (
    id SERIAL PRIMARY KEY,
    transaction_id VARCHAR(36) REFERENCES transactions(transaction_id),
    account_number VARCHAR(20),
    recommendation_text TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Cuentas de prueba con saldo inicial
INSERT INTO accounts (account_number, owner_name, balance) VALUES 
('CTA-1001', 'Juan Perez', 5000.00),
('CTA-2002', 'Maria Lopez', 1200.00),
('CTA-3003', 'Carlos Ramirez', 2500.00),
('CTA-4004', 'Ana Torres', 3400.00),
('CTA-5005', 'Luis Mendoza', 1800.00),
('CTA-6006', 'Sofia Herrera', 4200.00)
ON CONFLICT (account_number) DO NOTHING;