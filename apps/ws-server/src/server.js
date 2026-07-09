/**
 * OpenFormat WebSocket Server — Week 2 Day 1
 *
 * Responsibilities:
 *   - Authenticate JWTs from ?token= query param
 *   - Accept WebSocket connections on /ws
 *   - Subscribe to Redis channel model_events:{user_id} per connection
 *   - Relay model_ready / model_failed events to the connected client
 *   - Heartbeat (PING/PONG) with 30s interval, 10s timeout
 *   - Room-based broadcast for collaborative cursor/annotation events
 */

import Fastify from 'fastify';
import websocketPlugin from '@fastify/websocket';
import jwt from 'jsonwebtoken';
import Redis from 'ioredis';
import 'dotenv/config';

import {
  connectedClients,
  messagesSent,
  messagesDropped,
  redisEventsReceived,
  redisPublishFailures,
  reconnections,
  roomBroadcasts,
  cursorMovesThrottled,
  startMetricsServer,
} from './metrics.js';
// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------
const PORT = parseInt(process.env.PORT || '8001', 10);
const JWT_SECRET = process.env.JWT_SECRET || 'dev-secret-change-in-prod';
const REDIS_URL = process.env.REDIS_URL || 'redis://localhost:6379/0';

const HEARTBEAT_INTERVAL_MS = 30_000;
const PONG_TIMEOUT_MS = 10_000;
const CURSOR_THROTTLE_MS = parseInt(process.env.CURSOR_THROTTLE_MS || '100', 10); // 10/sec default
// ---------------------------------------------------------------------------
// Shared Redis client for general commands (NOT pub/sub)
// ---------------------------------------------------------------------------
const redisClient = new Redis(REDIS_URL, {
  lazyConnect: false,
  enableOfflineQueue: false,
  retryStrategy: (times) => Math.min(times * 200, 5000),
});

redisClient.on('error', (err) => {
  console.error('[redis] connection error:', err.message);
});

// ---------------------------------------------------------------------------
// Fastify app
// ---------------------------------------------------------------------------
// Use pino-pretty for dev only if installed; fall back to plain JSON logger
let loggerConfig = { level: process.env.LOG_LEVEL || 'info' };
if (process.env.NODE_ENV !== 'production') {
  try {
    await import('pino-pretty');
    loggerConfig.transport = { target: 'pino-pretty', options: { colorize: true } };
  } catch {
    // pino-pretty not installed — plain JSON logging
  }
}

const fastify = Fastify({ logger: loggerConfig });

await fastify.register(websocketPlugin);

// ---------------------------------------------------------------------------
// In-memory state (per-pod — not distributed)
// ---------------------------------------------------------------------------
/** modelId -> Set<ClientContext> */
const rooms = new Map();

/** userId -> Set<ClientContext> */
const userConnections = new Map();

// ---------------------------------------------------------------------------
// JWT verification
// ---------------------------------------------------------------------------
/**
 * @param {string | undefined} token
 * @returns {string | null} userId (sub claim) or null if invalid
 */
function verifyToken(token) {
  if (!token) return null;
  try {
    const payload = jwt.verify(token, JWT_SECRET, { algorithms: ['HS256'] });
    if (payload.type !== 'access') return null;
    return payload.sub;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
/**
 * @param {import('ws').WebSocket} socket
 * @param {object} message
 */
function send(socket, message) {
  if (socket.readyState === 1 /* OPEN */) {
    socket.send(JSON.stringify(message));
    messagesSent.inc();
  } else {
    messagesDropped.inc();
  }
}

/**
 * Broadcast to all clients in a room, optionally excluding one.
 * @param {string} modelId
 * @param {object} message
 * @param {object|null} [exclude]
 */
function broadcast(modelId, message, exclude = null) {
  const room = rooms.get(modelId);
  if (!room) return;
  const payload = JSON.stringify(message);
  for (const client of room) {
    if (client === exclude) continue;
    if (client.socket.readyState === 1) {
      client.socket.send(payload);
      messagesSent.inc();
    }
  }
}

// ---------------------------------------------------------------------------
// Health check
// ---------------------------------------------------------------------------
fastify.get('/health', async () => {
  let redisOk = false;
  try {
    await redisClient.ping();
    redisOk = true;
  } catch {
    // degraded but still serving
  }
  return {
    status: 'ok',
    redis: redisOk ? 'ok' : 'degraded',
    connections: userConnections.size,
  };
});

// ---------------------------------------------------------------------------
// WebSocket handler
// ---------------------------------------------------------------------------
fastify.get('/ws', { websocket: true }, (socket, req) => {
  const token = req.query?.token;
  const userId = verifyToken(token);

  if (!userId) {
    // Close with 4001 = Unauthorized; must happen before any async work
    socket.close(4001, 'Unauthorized');
    return;
  }

  const initialModelId = req.query?.model_id || null;

  /** @type {{ socket: import('ws').WebSocket, userId: string, isAlive: boolean, heartbeatTimer: NodeJS.Timeout|null, pongTimer: NodeJS.Timeout|null, lastCursorBroadcastAt: number }} */
  const client = {
    socket,
    userId,
    isAlive: true,
    heartbeatTimer: null,
    pongTimer: null,
    lastCursorBroadcastAt: 0,
  };
// Track reconnects
if (userConnections.has(userId)) {
  reconnections.inc();
}
connectedClients.inc();

  fastify.log.info({ userId, initialModelId }, 'WebSocket connected');

  // ── Track user connections ────────────────────────────────────────────────
  if (!userConnections.has(userId)) userConnections.set(userId, new Set());
  userConnections.get(userId).add(client);

  // ── Dedicated Redis subscriber for this connection ────────────────────────
  // Each connection needs its OWN subscriber client — ioredis subscriber mode
  // is exclusive (can only call subscribe/unsubscribe, not regular commands).
  const subscriber = new Redis(REDIS_URL, {
    lazyConnect: false,
    enableOfflineQueue: true,
    retryStrategy: (times) => Math.min(times * 200, 5000),
  });

  const redisChannel = `model_events:${userId}`;

  subscriber.subscribe(redisChannel, (err) => {
    if (err) {
      fastify.log.error({ err, redisChannel }, 'Redis subscribe failed');
    } else {
      fastify.log.debug({ redisChannel }, 'Subscribed to Redis channel');
    }
  });

  subscriber.on('message', (channel, message) => {
    redisEventsReceived.inc();
    if (channel !== redisChannel) return;
    if (client.socket.readyState === 1) {
      client.socket.send(message);
      messagesSent.inc();
    } else {
      messagesDropped.inc();
    }
  });

  subscriber.on('error', (err) => {
    redisPublishFailures.inc();
    fastify.log.warn({ err, userId }, 'Subscriber Redis error');
  });

  // ── Room helpers ──────────────────────────────────────────────────────────
  let currentModelId = null;

  function joinRoom(modelId) {
    if (currentModelId === modelId) return;
    if (currentModelId) leaveRoom(currentModelId);
    currentModelId = modelId;
    if (!rooms.has(modelId)) rooms.set(modelId, new Set());
    rooms.get(modelId).add(client);
    broadcast(modelId, { event: 'user:join', userId }, client);
    fastify.log.debug({ userId, modelId }, 'Joined room');
  }

  function leaveRoom(modelId) {
    const room = rooms.get(modelId);
    if (!room) return;
    room.delete(client);
    broadcast(modelId, { event: 'user:leave', userId }, client);
    if (room.size === 0) rooms.delete(modelId);
    if (currentModelId === modelId) currentModelId = null;
    fastify.log.debug({ userId, modelId }, 'Left room');
  }

  if (initialModelId) joinRoom(initialModelId);

  // ── Heartbeat ─────────────────────────────────────────────────────────────
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
          fastify.log.warn({ userId }, 'Heartbeat timeout — closing');
          client.socket.close(4002, 'Heartbeat timeout');
          cleanup();
        }
      }, PONG_TIMEOUT_MS);
    }, HEARTBEAT_INTERVAL_MS);
  }

  function stopHeartbeat() {
    if (client.heartbeatTimer) clearInterval(client.heartbeatTimer);
    if (client.pongTimer) clearTimeout(client.pongTimer);
    client.heartbeatTimer = null;
    client.pongTimer = null;
  }

  startHeartbeat();

  // ── Message handler ───────────────────────────────────────────────────────
  socket.on('message', (raw) => {
    let msg;
    try {
      msg = JSON.parse(raw.toString());
    } catch {
      fastify.log.debug({ userId }, 'Received non-JSON message — ignoring');
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
          const now = Date.now();
          if (now - client.lastCursorBroadcastAt < CURSOR_THROTTLE_MS) {
            cursorMovesThrottled.inc();
            break;
          }
          client.lastCursorBroadcastAt = now;
          broadcast(currentModelId, { event: 'CURSOR_MOVE', userId, data: msg.data }, client);
          roomBroadcasts.labels({ event_type: msg.event }).inc();
        }
        break;
      }
      case 'PING': {
        // Client-initiated ping — respond immediately
        send(client.socket, { event: 'PONG' });
        break;
      }
      case 'PONG': {
        // Response to our server-initiated PING
        client.isAlive = true;
        if (client.pongTimer) clearTimeout(client.pongTimer);
        break;
      }
      case 'annotation:update':
      case 'model:sync': {
        if (currentModelId) {
          broadcast(currentModelId, { event: msg.event, userId, data: msg.data }, client);
          roomBroadcasts.labels({ event_type: msg.event }).inc();
        }
        break;
      }
      default:
        fastify.log.debug({ userId, event: msg.event }, 'Unknown event type');
        break;
    }
  });

  // ── Cleanup ───────────────────────────────────────────────────────────────
  function cleanup() {
    stopHeartbeat();
    if (currentModelId) leaveRoom(currentModelId);

    const userSet = userConnections.get(userId);
    if (userSet) {
      userSet.delete(client);
      if (userSet.size === 0) userConnections.delete(userId);
    }

    // Unsubscribe and close the per-connection Redis subscriber
    subscriber.unsubscribe(redisChannel).catch(() => {});
    subscriber.quit().catch(() => {});

    fastify.log.info({ userId }, 'WebSocket disconnected');
    connectedClients.dec();
  }
  socket.on('close', cleanup);
  socket.on('error', (err) => {
    fastify.log.warn({ userId, err: err.message }, 'WebSocket error');
    cleanup();
  });
});

// ---------------------------------------------------------------------------
// Graceful shutdown
// ---------------------------------------------------------------------------
async function gracefulShutdown(signal) {
  fastify.log.info({ signal }, 'Shutting down ws-server');
  await fastify.close();
  await redisClient.quit();
  process.exit(0);
}

process.on('SIGTERM', () => gracefulShutdown('SIGTERM'));
process.on('SIGINT', () => gracefulShutdown('SIGINT'));

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------
try {
  await fastify.listen({ port: PORT, host: '0.0.0.0' });
  fastify.log.info({ port: PORT }, 'ws-server listening');
} catch (err) {
  fastify.log.error(err);
  process.exit(1);
}
startMetricsServer();