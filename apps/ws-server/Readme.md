# OpenFormat WS Server

## Run
```bash
cd apps/ws-server
npm install
cp .env.example .env
npm run dev
```

## Verify
```bash
curl http://localhost:8001/health
# Connect: ws://localhost:8001/ws?token=<JWT>&model_id=<uuid>
```