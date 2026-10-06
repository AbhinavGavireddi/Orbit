"""Reservation ledger for paid validation. Estimates are not provider billing enforcement."""
import argparse
import math
from pathlib import Path
import sqlite3
import subprocess


class ValidationBudget:
    def __init__(self, path, limit=4500):
        self.path, self.limit = Path(path), limit
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS usage (id TEXT PRIMARY KEY, reserved REAL, actual REAL)')

    @staticmethod
    def amount(value):
        if not math.isfinite(value) or value < 0:
            raise ValueError('Amount must be finite and nonnegative')

    def reserve(self, identity, amount):
        self.amount(amount)
        with sqlite3.connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT reserved FROM usage WHERE id=?', (identity,)).fetchone()
            if prior:
                if prior[0] != amount:
                    raise ValueError('Reservation identity already has a different amount')
                return False
            spent = db.execute('SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM usage').fetchone()[0]
            if spent + amount > self.limit:
                raise ValueError('Validation budget exhausted; reconcile costs before another run')
            db.execute('INSERT INTO usage VALUES (?,?,NULL)', (identity, amount))
        return True

    def settle(self, identity, amount):
        self.amount(amount)
        with sqlite3.connect(self.path) as db:
            if db.execute('UPDATE usage SET actual=? WHERE id=?', (amount, identity)).rowcount != 1:
                raise ValueError('Unknown reservation')

    def total(self):
        with sqlite3.connect(self.path) as db:
            return db.execute('SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM usage').fetchone()[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger', default='var/validation/budget.sqlite')
    parser.add_argument('--id', required=True)
    parser.add_argument('--reserve-inr', required=True, type=float)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or args.reserve_inr <= 0:
        parser.error('Provide a positive conservative reservation and a validation command after --')
    if not ValidationBudget(args.ledger).reserve(args.id, args.reserve_inr):
        raise SystemExit('This run identity has already been reserved; it will not execute again.')
    # Failed/interrupted runs retain the reservation until explicitly reconciled.
    raise SystemExit(subprocess.run(command, check=False).returncode)


if __name__ == '__main__':
    main()
