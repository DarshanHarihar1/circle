#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/../backend"

if [ ! -f .env ]; then
  echo "ERROR: backend/.env not found. Copy backend/.env.example to backend/.env and fill in values."
  exit 1
fi

echo "Running Alembic migrations..."
alembic upgrade head
echo "Migrations complete."
