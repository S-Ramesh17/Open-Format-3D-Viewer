/**
 * OpenFormat WebSocket Server
 *
 * Responsibilities:
 *   - Authenticate JWTs from ?token= query param
 *   - Accept WebSocket connections on /ws
 *   - Subscribe to Redis channel model_events:{user_id} per connection
 *   - Relay model_ready / model_failed events to the connected client
 *   - Heartbeat (PING/PONG) with 30s interval, 10s timeout
 *   - Room-based broadcast for collaborative cursor/annotation events
 */

import { randomUUID } from 'node:crypto';

import Fastify from 'fastify';
import websocketPlugin from '@fastify/websocket';
import jwt from 'jsonwebtoken';
import Redis from 'ioredis';
import 'dotenv/config';

// Unique per-process id, used to recognize (and skip) our own room
// broadcasts when they echo back to us via the shared Redis channel.
const PROCESS_ID = randomUUID();

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
const JWT_SECRET = process.env.JWT_SECRET;
if (!JWT_SECRET) {
  // eslint-disable-next-line no-console
  console.error('FATAL: JWT_SECRET environment variable is not set. Refusing to start with an insecure default.');
  process.exit(1);
}
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
// Shared, process-wide subscriber for cross-instance room broadcasts.
// Room state (`rooms` Map, below) is per-pod; without this, CURSOR_MOVE /
// ANNOTATION_CREATED / ANNOTATION_UPDATED / MODEL_SYNC / join-leave events
// only reach clients connected to the same ws-server replica. One
// PSUBSCRIBE per process (not per connection) fans room events out to
// every replica; each replica then delivers only to its own locally
// connected room members.
const ROOM_CHANNEL_PREFIX = 'ws:room:';
const roomSubscriber = new Redis(REDIS_URL, {
  lazyConnect: false,
  enableOfflineQueue: true,
  retryStrategy: (times) => Math.min(times * 200, 5000),
});
roomSubscriber.on('error', (err) => {
  console.error('[redis] room subscriber error:', err.message);
});
roomSubscriber.psubscribe(`${ROOM_CHANNEL_PREFIX}*`, (err) => {
  if (err) console.error('[redis] room psubscribe failed:', err.message);
});
roomSubscriber.on('pmessage', (_pattern, channel, raw) => {
  let envelope;
  try {
    envelope = JSON.parse(raw);
  } catch {
    return;
  }
  if (envelope.originProcessId === PROCESS_ID) return; // already delivered locally
  const modelId = channel.slice(ROOM_CHANNEL_PREFIX.length);
  const room = rooms.get(modelId);
  if (!room) return;
  const payload = JSON.stringify(envelope.message);
  for (const client of room) {
    if (client.socket.readyState === 1) {
      client.socket.send(payload);
      messagesSent.inc();
    }
  }
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
  if (room) {
    const payload = JSON.stringify(message);
    for (const client of room) {
      if (client === exclude) continue;
      if (client.socket.readyState === 1) {
        client.socket.send(payload);
        messagesSent.inc();
      }
    }
  }

  // Fan out to other ws-server replicas so clients connected to a
  // different pod (but the same model room) also receive this event.
  // originProcessId lets every replica's subscriber skip its own
  // broadcasts, since it already delivered them locally above.
  redisClient
    .publish(
      `${ROOM_CHANNEL_PREFIX}${modelId}`,
      JSON.stringify({ originProcessId: PROCESS_ID, message })
    )
    .catch((err) => {
      redisPublishFailures.inc();
      fastify.log.warn({ err, modelId }, 'Room broadcast publish failed');
    });
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
fastify.get('/connect', { websocket: true }, async (socket, req) => {
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

  // ── Room helpers ──────────────────────────────────────────────────────────
  // Declared before the Redis subscribe step (moved up from where they used
  // to live) so processMessage(), below, can close over them.
  let currentModelId = null;

  function joinRoom(modelId) {
    if (currentModelId === modelId) return;
    if (currentModelId) leaveRoom(currentModelId);
    currentModelId = modelId;
    if (!rooms.has(modelId)) rooms.set(modelId, new Set());
    rooms.get(modelId).add(client);
    broadcast(modelId, { event: 'USER_JOINED', user: { id: userId } }, client);
    fastify.log.debug({ userId, modelId }, 'Joined room');
  }

  function leaveRoom(modelId) {
    const room = rooms.get(modelId);
    if (!room) return;
    room.delete(client);
    broadcast(modelId, { event: 'USER_LEFT', user_id: userId }, client);
    if (room.size === 0) rooms.delete(modelId);
    if (currentModelId === modelId) currentModelId = null;
    fastify.log.debug({ userId, modelId }, 'Left room');
  }

  // ── Connection readiness gate ─────────────────────────────────────────────
  // A WS upgrade completes (and the browser sees `open`) synchronously,
  // well before the async setup below (Redis subscribe, room join,
  // heartbeat start) finishes. `socket.on('message', ...)` is a plain
  // EventEmitter listener — Node does not buffer/replay events emitted
  // before a listener is attached, so a message the client sends in that
  // window would previously be lost entirely, not merely dropped by a
  // guard. Attaching the listener immediately (below, before any `await`)
  // and queueing until `connectionReady` closes that window instead of
  // narrowing it.
  let connectionReady = false;
  const pendingMessages = [];
  // Defensive cap: bounds memory if a client floods messages during the
  // (normally sub-second) init window. Generous enough to never trigger
  // in legitimate use.
  const MAX_PENDING_MESSAGES = 100;

  function processMessage(raw) {
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
          broadcast(
            currentModelId,
            { event: 'CURSOR_MOVED', user_id: userId, position: msg.data?.position },
            client
          );
          roomBroadcasts.labels({ event_type: 'CURSOR_MOVED' }).inc();
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
      case 'ANNOTATION_CREATED':
      case 'ANNOTATION_UPDATED':
      case 'MODEL_SYNC': {
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
  }

  // Attached now — before the Redis subscribe await below — so no message
  // sent immediately after `open` can arrive with no listener registered
  // at all. Messages are queued until initialization finishes.
  socket.on('message', (raw) => {
    if (!connectionReady) {
      if (pendingMessages.length >= MAX_PENDING_MESSAGES) {
        fastify.log.warn({ userId }, 'Pending message queue full during init — dropping');
        return;
      }
      pendingMessages.push(raw);
      return;
    }
    processMessage(raw);
  });

  // ── Dedicated Redis subscriber for this connection ────────────────────────
  // Each connection needs its OWN subscriber client — ioredis subscriber mode
  // is exclusive (can only call subscribe/unsubscribe, not regular commands).
  //
  // RACE CONDITION FIX: this subscriber is a brand-new Redis connection
  // (fresh TCP handshake), which is measurably slower than the WS upgrade
  // completing on the client side. Previously the client was registered in
  // userConnections and rooms immediately, while subscribe() ran
  // fire-and-forget in the background — a MODEL_READY/MODEL_PROCESSING
  // event published in that window was permanently lost (Redis pub/sub
  // does not buffer/replay). Everything that makes this connection
  // "active" (connection map, room join, heartbeat, client message
  // handling) now happens only after subscription is confirmed.
  const subscriber = new Redis(REDIS_URL, {
    lazyConnect: false,
    enableOfflineQueue: true,
    retryStrategy: (times) => Math.min(times * 200, 5000),
  });

  const redisChannel = `model_events:${userId}`;

  // Attach the message/error listeners BEFORE subscribing (standard ioredis
  // pattern) so there's no window where a delivered message could arrive
  // with no listener registered to receive it.
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

  try {
    await subscriber.subscribe(redisChannel);
  } catch (err) {
    fastify.log.error(
      { err, userId, redisChannel },
      'Redis subscribe failed during WebSocket connect — closing connection to avoid a client that appears connected but cannot receive model events'
    );
    // No orphan subscriber: this connection was never registered anywhere
    // (not in userConnections, no room), so there is nothing else to clean
    // up besides the subscriber itself. Any messages queued in
    // pendingMessages become moot — the socket is being closed and was
    // never a validly-established connection to begin with.
    await subscriber.quit().catch(() => {});
    socket.close(1011, 'Redis subscription failed');
    return;
  }

  fastify.log.debug({ redisChannel }, 'Subscribed to Redis channel');

  // ── Subscription confirmed — connection is now considered active ─────────
  if (userConnections.has(userId)) {
    reconnections.inc();
  }
  connectedClients.inc();

  fastify.log.info({ userId, initialModelId }, 'WebSocket connected');

  if (!userConnections.has(userId)) userConnections.set(userId, new Set());
  userConnections.get(userId).add(client);

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

  // ── Initialization complete — flip the readiness gate and drain any
  //    messages the client sent while we were still subscribing/joining.
  connectionReady = true;
  if (pendingMessages.length > 0) {
    fastify.log.debug(
      { userId, count: pendingMessages.length },
      'Draining messages queued during connection initialization'
    );
    const queued = pendingMessages.splice(0);
    for (const raw of queued) processMessage(raw);
  }

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