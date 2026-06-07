#!/usr/bin/env bash
set -e
ROOT="$(dirname "$0")/.."

# 1. Start Postgres + Supabase
echo "Starting infra..."
docker compose -f "$ROOT/infra/docker-compose.yml" up -d

# 2. Wait for Postgres to be healthy
echo "Waiting for Postgres..."
until docker compose -f "$ROOT/infra/docker-compose.yml" exec -T db pg_isready -U postgres > /dev/null 2>&1; do
  sleep 1
done

# 3. Run migrations
bash "$ROOT/scripts/migrate.sh"

# 4. Start backend
echo "Starting FastAPI..."
cd "$ROOT/backend"
uvicorn main:app --reload --port 8000 &
BACKEND_PID=$!

# 5. Start frontend
echo "Starting Next.js..."
cd "$ROOT/frontend"
npm run dev &
FRONTEND_PID=$!

echo ""
echo "Circle is running:"
echo "  API:      http://localhost:8000"
echo "  Frontend: http://localhost:3000"
echo "  Supabase: http://localhost:54321"
echo ""
echo "Press Ctrl+C to stop all services."

trap "kill $BACKEND_PID $FRONTEND_PID; docker compose -f '$ROOT/infra/docker-compose.yml' stop" SIGINT SIGTERM
wait $BACKEND_PID $FRONTEND_PID
