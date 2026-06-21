import Fastify from 'fastify';
import websocketPlugin from '@fastify/websocket';
import jwt from 'jsonwebtoken';
import Redis from 'ioredis';
import 'dotenv/config';

const PORT = process.env.PORT || 8001;
const JWT_SECRET = process.env.JWT_SECRET || 'dev-secret-change-in-prod';
const REDIS_URL = process.env.REDIS_URL || 'redis://localhost:6379/0';

const HEARTBEAT_INTERVAL_MS = 30_000;
const PONG_TIMEOUT_MS = 10_000;

const fastify = Fastify({ logger: true });

await fastify.register(websocketPlugin);

// In-memory room tracking — per pod only, NOT Redis.
// Map of modelId -> Set of client objects { socket, userId, isAlive, heartbeatTimer, pongTimer }
const rooms = new Map();

// Map of userId -> Set of client objects currently connected (for Redis pub/sub targeting)
const userConnections = new Map();

function verifyToken(token) {
  try {
    const payload = jwt.verify(token, JWT_SECRET, { algorithms: ['HS256'] });
    if (payload.type !== 'access') return null;
    return payload.sub;
  } catch {
    return null;
  }
}

fastify.get('/health', async () => ({ status: 'ok' }));

fastify.get('/ws', { websocket: true }, (connection, req) => {
  const token = req.query?.token;
  const userId = verifyToken(token);

  if (!userId) {
    connection.socket.close(4001, 'Unauthorized');
    return;
  }

  // Initial model_id from query param is optional now — client can JOIN_MODEL explicitly.
  let currentModelId = req.query?.model_id || null;

  const client = {
    socket: connection.socket,
    userId,
    isAlive: true,
    heartbeatTimer: null,
    pongTimer: null,
  };

  // ── Redis pub/sub subscription for this user ──────────────────────────────
  // model_events:{user_id} — relay all messages published on this channel
  // directly to the connected client.
  const subscriber = new Redis(REDIS_URL);
  const channel = `model_events:${userId}`;

  subscriber.subscribe(channel).catch((err) => {
    fastify.log.error({ err, channel }, 'Redis subscribe failed');
  });

  subscriber.on('message', (chan, message) => {
    if (chan !== channel) return;
    if (client.socket.readyState === 1) {
      client.socket.send(message);
    }
  });

  if (!userConnections.has(userId)) userConnections.set(userId, new Set());
  userConnections.get(userId).add(client);

  function joinRoom(modelId) {
    if (currentModelId === modelId) return;
    if (currentModelId) leaveRoom(currentModelId);

    currentModelId = modelId;
    if (!rooms.has(modelId)) rooms.set(modelId, new Set());
    rooms.get(modelId).add(client);

    broadcast(modelId, { event: 'user:join', userId }, client);
  }

  function leaveRoom(modelId) {
    const room = rooms.get(modelId);
    if (!room) return;
    room.delete(client);
    broadcast(modelId, { event: 'user:leave', userId }, client);
    if (room.size === 0) rooms.delete(modelId);
    if (currentModelId === modelId) currentModelId = null;
  }

  if (currentModelId) joinRoom(currentModelId);

  // ── Heartbeat ────────────────────────────────────────────────────────────
  function startHeartbeat() {
    client.heartbeatTimer = setInterval(() => {
      if (client.socket.readyState !== 1) {
        cleanup();
        return;
      }

      client.isAlive = false;
      send(client.socket, { event: 'PING' });

      client.pongTimer = setTimeout(() => {
        if (!client.isAlive) {
          fastify.log.warn({ userId }, 'No PONG received, closing connection');
          client.socket.close(4002, 'Heartbeat timeout');
          cleanup();
        }
      }, PONG_TIMEOUT_MS);
    }, HEARTBEAT_INTERVAL_MS);
  }

  function stopHeartbeat() {
    if (client.heartbeatTimer) clearInterval(client.heartbeatTimer);
    if (client.pongTimer) clearTimeout(client.pongTimer);
  }

  startHeartbeat();

  connection.socket.on('message', (raw) => {
    let msg;
    try {
      msg = JSON.parse(raw.toString());
    } catch {
      return;
    }

    switch (msg.event) {
      case 'JOIN_MODEL': {
        if (msg.model_id) joinRoom(msg.model_id);
        break;
      }
      case 'LEAVE_MODEL': {
        if (msg.model_id) leaveRoom(msg.model_id);
        break;
      }
      case 'CURSOR_MOVE': {
        if (currentModelId) {
          broadcast(
            currentModelId,
            { event: 'CURSOR_MOVE', userId, data: msg.data },
            client
          );
        }
        break;
      }
      case 'PING': {
        send(client.socket, { event: 'PONG' });
        break;
      }
      case 'PONG': {
        client.isAlive = true;
        if (client.pongTimer) clearTimeout(client.pongTimer);
        break;
      }
      case 'annotation:update':
      case 'model:sync': {
        if (currentModelId) {
          broadcast(
            currentModelId,
            { event: msg.event, userId, data: msg.data },
            client
          );
        }
        break;
      }
      default:
        break;
    }
  });

  function cleanup() {
    stopHeartbeat();
    if (currentModelId) leaveRoom(currentModelId);

    const userSet = userConnections.get(userId);
    if (userSet) {
      userSet.delete(client);
      if (userSet.size === 0) userConnections.delete(userId);
    }

    subscriber.unsubscribe(channel).catch(() => {});
    subscriber.quit().catch(() => {});
  }

  connection.socket.on('close', cleanup);
  connection.socket.on('error', cleanup);
});

function send(socket, message) {
  if (socket.readyState === 1) {
    socket.send(JSON.stringify(message));
  }
}

function broadcast(modelId, message, exclude) {
  const room = rooms.get(modelId);
  if (!room) return;
  const payload = JSON.stringify(message);
  for (const client of room) {
    if (client === exclude) continue;
    if (client.socket.readyState === 1) {
      client.socket.send(payload);
    }
  }
}

try {
  await fastify.listen({ port: PORT, host: '0.0.0.0' });
} catch (err) {
  fastify.log.error(err);
  process.exit(1);
}