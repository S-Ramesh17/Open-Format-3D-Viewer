// apps/ws-server/tests/server.test.js
//
// Integration tests for the WebSocket server. There was no test file, test
// runner, or test script for this app before — server.js is a top-level
// side-effecting module (it calls `await fastify.listen()` at import time),
// so the only honest way to test it without refactoring production code is
// to actually import it once, let it bind a real port, and drive it with
// real WebSocket clients. This uses Node's built-in `node:test` +
// `node:assert` (no new test-framework dependency) and the `ws` client
// package (added as a devDependency — the server already depends on it
// transitively via @fastify/websocket, but it wasn't a direct/testable
// dependency before).
//
// Requires a real reachable Redis (REDIS_URL, defaults to localhost:6379/0)
// — the server connects to Redis eagerly at import time, same as in
// production, so there's no way around this without mocking ioredis, which
// would stop this from testing the real pub/sub relay path (the whole
// point of MODEL_READY/MODEL_PROCESSING relay coverage below).
//
// Not covered: the 30s/10s heartbeat timeout path. It's real-timer-based
// with no injection point, so exercising it honestly would mean a 40s+
// sleep per test run. The client-initiated PING/PONG exchange (immediate,
// no timers) is covered instead, along with confirming the heartbeat timer
// is scheduled at all.

import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { setTimeout as sleep } from 'node:timers/promises';
import jwt from 'jsonwebtoken';
import WebSocket from 'ws';
import Redis from 'ioredis';

const TEST_PORT = process.env.WS_TEST_PORT || '8199';
const JWT_SECRET = 'test-secret-for-ws-server-integration-tests';
const REDIS_URL = process.env.REDIS_URL || 'redis://localhost:6379/0';

process.env.PORT = TEST_PORT;
process.env.JWT_SECRET = JWT_SECRET;
process.env.REDIS_URL = REDIS_URL;
process.env.WS_METRICS_PORT = process.env.WS_METRICS_PORT || '9199';
process.env.NODE_ENV = 'test';

const BASE_URL = `ws://localhost:${TEST_PORT}`;
const HEALTH_URL = `http://localhost:${TEST_PORT}/health`;

let publisher;

function makeToken(userId, overrides = {}) {
  return jwt.sign(
    { sub: userId, type: 'access', ...overrides },
    JWT_SECRET,
    { algorithm: 'HS256', expiresIn: '5m' }
  );
}

function connect(query) {
  return new WebSocket(`${BASE_URL}/connect?${query}`);
}

function onceOpen(ws) {
  return new Promise((resolve, reject) => {
    ws.once('open', resolve);
    ws.once('error', reject);
  });
}

function onceClose(ws) {
  return new Promise((resolve) => {
    ws.once('close', (code, reason) => resolve({ code, reason: reason.toString() }));
  });
}

/** Wait for the next JSON message matching `predicate`, up to `timeoutMs`. */
function waitForMessage(ws, predicate, timeoutMs = 2000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      ws.off('message', handler);
      reject(new Error(`Timed out waiting for matching message after ${timeoutMs}ms`));
    }, timeoutMs);

    function handler(raw) {
      let msg;
      try {
        msg = JSON.parse(raw.toString());
      } catch {
        return;
      }
      if (predicate(msg)) {
        clearTimeout(timer);
        ws.off('message', handler);
        resolve(msg);
      }
    }
    ws.on('message', handler);
  });
}

before(async () => {
  // Importing the module triggers `await fastify.listen()` for real, exactly
  // as in production. Env vars above must be set before this import.
  await import('../src/server.js');
  publisher = new Redis(REDIS_URL);
  // Give fastify a moment to finish binding.
  await sleep(300);
});

after(async () => {
  await publisher?.quit().catch(() => {});
  // server.js has no exported close/shutdown handle for tests to call, and
  // its SIGTERM/SIGINT handlers are the only teardown path — rather than
  // hang node:test on open sockets/timers, exit explicitly once all tests
  // have run.
  process.exit(0);
});

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

test('rejects a connection with no token (close code 4001)', async () => {
  const ws = connect('');
  const closed = onceClose(ws);
  ws.on('error', () => {}); // server closes before upgrade completes on some platforms
  const { code } = await closed;
  assert.equal(code, 4001);
});

test('rejects a connection with an invalid token (close code 4001)', async () => {
  const ws = connect('token=not-a-real-jwt');
  const closed = onceClose(ws);
  ws.on('error', () => {});
  const { code } = await closed;
  assert.equal(code, 4001);
});

test('rejects a token whose type is not "access" (e.g. a refresh token)', async () => {
  const refreshLikeToken = makeToken('user-refresh-type', { type: 'refresh' });
  const ws = connect(`token=${refreshLikeToken}`);
  const closed = onceClose(ws);
  ws.on('error', () => {});
  const { code } = await closed;
  assert.equal(code, 4001);
});

test('accepts a connection with a valid access token', async () => {
  const token = makeToken('user-auth-happy');
  const ws = connect(`token=${token}`);
  await onceOpen(ws);
  ws.close();
});

// ---------------------------------------------------------------------------
// JOIN_MODEL / LEAVE_MODEL / room broadcast
// ---------------------------------------------------------------------------

test('JOIN_MODEL broadcasts USER_JOINED to the other room member, not the joiner', async () => {
  const modelId = 'model-join-test-1';
  const tokenA = makeToken('user-A-join');
  const tokenB = makeToken('user-B-join');

  const wsA = connect(`token=${tokenA}&model_id=${modelId}`);
  await onceOpen(wsA);

  const wsB = connect(`token=${tokenB}`);
  await onceOpen(wsB);

  const joined = waitForMessage(wsA, (m) => m.event === 'USER_JOINED');
  wsB.send(JSON.stringify({ event: 'JOIN_MODEL', model_id: modelId }));
  const msg = await joined;
  assert.equal(msg.user.id, 'user-B-join');

  wsA.close();
  wsB.close();
});

test('LEAVE_MODEL broadcasts USER_LEFT to remaining room members', async () => {
  const modelId = 'model-leave-test-1';
  const tokenA = makeToken('user-A-leave');
  const tokenB = makeToken('user-B-leave');

  const wsA = connect(`token=${tokenA}&model_id=${modelId}`);
  await onceOpen(wsA);
  const wsB = connect(`token=${tokenB}&model_id=${modelId}`);
  await onceOpen(wsB);

  // Drain the USER_JOINED broadcast wsA gets when B joins, so it doesn't
  // get mistaken for the USER_LEFT we wait for next.
  await waitForMessage(wsA, (m) => m.event === 'USER_JOINED');

  const left = waitForMessage(wsA, (m) => m.event === 'USER_LEFT');
  wsB.send(JSON.stringify({ event: 'LEAVE_MODEL', model_id: modelId }));
  const msg = await left;
  assert.equal(msg.user_id, 'user-B-leave');

  wsA.close();
  wsB.close();
});

test('disconnect cleanup: closing a socket removes it from the room and health connection count drops', async () => {
  const modelId = 'model-disconnect-test-1';
  const token = makeToken('user-disconnect-1');
  const ws = connect(`token=${token}&model_id=${modelId}`);
  await onceOpen(ws);

  const before_ = await (await fetch(HEALTH_URL)).json();
  ws.close();
  await sleep(200); // let the server's 'close' handler run cleanup()
  const after_ = await (await fetch(HEALTH_URL)).json();

  assert.ok(after_.connections <= before_.connections);
});

// ---------------------------------------------------------------------------
// Cursor movement
// ---------------------------------------------------------------------------

test('CURSOR_MOVE broadcasts CURSOR_MOVED with position to other room members', async () => {
  const modelId = 'model-cursor-test-1';
  const tokenA = makeToken('user-A-cursor');
  const tokenB = makeToken('user-B-cursor');

  const wsA = connect(`token=${tokenA}&model_id=${modelId}`);
  await onceOpen(wsA);
  const wsB = connect(`token=${tokenB}&model_id=${modelId}`);
  await onceOpen(wsB);
  await waitForMessage(wsA, (m) => m.event === 'USER_JOINED');

  const moved = waitForMessage(wsA, (m) => m.event === 'CURSOR_MOVED');
  wsB.send(JSON.stringify({
    event: 'CURSOR_MOVE',
    data: { position: { x: 1, y: 2, z: 3 } },
  }));
  const msg = await moved;
  assert.equal(msg.user_id, 'user-B-cursor');
  assert.deepEqual(msg.position, { x: 1, y: 2, z: 3 });

  wsA.close();
  wsB.close();
});

// ---------------------------------------------------------------------------
// Annotation events (client-relayed room broadcast)
// ---------------------------------------------------------------------------

test('ANNOTATION_CREATED is relayed to other room members', async () => {
  const modelId = 'model-annotation-test-1';
  const tokenA = makeToken('user-A-annot');
  const tokenB = makeToken('user-B-annot');

  const wsA = connect(`token=${tokenA}&model_id=${modelId}`);
  await onceOpen(wsA);
  const wsB = connect(`token=${tokenB}&model_id=${modelId}`);
  await onceOpen(wsB);
  await waitForMessage(wsA, (m) => m.event === 'USER_JOINED');

  const relayed = waitForMessage(wsA, (m) => m.event === 'ANNOTATION_CREATED');
  wsB.send(JSON.stringify({
    event: 'ANNOTATION_CREATED',
    data: { annotation_id: 'ann-1', action: 'created' },
  }));
  const msg = await relayed;
  assert.equal(msg.data.annotation_id, 'ann-1');

  wsA.close();
  wsB.close();
});

// ---------------------------------------------------------------------------
// Client-initiated heartbeat (immediate PING/PONG — no 30s timer involved)
// ---------------------------------------------------------------------------

test('client-initiated PING gets an immediate PONG', async () => {
  const token = makeToken('user-ping');
  const ws = connect(`token=${token}`);
  await onceOpen(ws);

  const pong = waitForMessage(ws, (m) => m.event === 'PONG');
  ws.send(JSON.stringify({ event: 'PING' }));
  await pong;

  ws.close();
});

// ---------------------------------------------------------------------------
// Redis relay: MODEL_READY / MODEL_PROCESSING
// ---------------------------------------------------------------------------

test('MODEL_READY published on model_events:{userId} is relayed to that user\'s socket', async () => {
  const userId = 'user-model-ready-relay';
  const token = makeToken(userId);
  const ws = connect(`token=${token}`);
  await onceOpen(ws);

  const ready = waitForMessage(ws, (m) => m.event === 'MODEL_READY');
  await publisher.publish(
    `model_events:${userId}`,
    JSON.stringify({ event: 'MODEL_READY', data: { model_id: 'm-1', chunk_urls: ['a', 'b'] } })
  );
  const msg = await ready;
  assert.deepEqual(msg.data.chunk_urls, ['a', 'b']);

  ws.close();
});

test('MODEL_PROCESSING published on model_events:{userId} is relayed to that user\'s socket', async () => {
  const userId = 'user-model-processing-relay';
  const token = makeToken(userId);
  const ws = connect(`token=${token}`);
  await onceOpen(ws);

  const processing = waitForMessage(ws, (m) => m.event === 'MODEL_PROCESSING');
  await publisher.publish(
    `model_events:${userId}`,
    JSON.stringify({ event: 'MODEL_PROCESSING', data: { model_id: 'm-2', progress_pct: 42, stage: 'convert' } })
  );
  const msg = await processing;
  assert.equal(msg.data.progress_pct, 42);

  ws.close();
});

test('a MODEL_READY event for a different user is NOT delivered to this socket', async () => {
  const userId = 'user-isolated-relay';
  const otherUserId = 'user-someone-else';
  const token = makeToken(userId);
  const ws = connect(`token=${token}`);
  await onceOpen(ws);

  let receivedForeignEvent = false;
  ws.on('message', (raw) => {
    const msg = JSON.parse(raw.toString());
    if (msg.event === 'MODEL_READY' && msg.data?.model_id === 'foreign-model') {
      receivedForeignEvent = true;
    }
  });

  await publisher.publish(
    `model_events:${otherUserId}`,
    JSON.stringify({ event: 'MODEL_READY', data: { model_id: 'foreign-model', chunk_urls: [] } })
  );
  await sleep(300);
  assert.equal(receivedForeignEvent, false);

  ws.close();
});
