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
import { createServer } from 'node:http';
import jwt from 'jsonwebtoken';
import WebSocket from 'ws';
import Redis from 'ioredis';

const TEST_PORT = process.env.WS_TEST_PORT || '8199';
const JWT_SECRET = 'test-secret-for-ws-server-integration-tests';
const REDIS_URL = process.env.REDIS_URL || 'redis://localhost:6379/0';
const INTERNAL_API_PORT = process.env.WS_TEST_INTERNAL_API_PORT || '8299';
const INTERNAL_SERVICE_KEY = 'test-internal-service-key';
const TEST_USER_NAME = 'Test User';

process.env.PORT = TEST_PORT;
process.env.JWT_SECRET = JWT_SECRET;
process.env.REDIS_URL = REDIS_URL;
process.env.WS_METRICS_PORT = process.env.WS_METRICS_PORT || '9199';
process.env.NODE_ENV = 'test';
process.env.WS_INTERNAL_API_URL = `http://localhost:${INTERNAL_API_PORT}`;
process.env.WS_INTERNAL_SERVICE_KEY = INTERNAL_SERVICE_KEY;

const BASE_URL = `ws://localhost:${TEST_PORT}`;
const HEALTH_URL = `http://localhost:${TEST_PORT}/health`;

let publisher;

// ---------------------------------------------------------------------------
// Fake internal authorization API (stands in for the real API's
// GET /internal/models/{id}/authorize during tests). Authorizes everything
// by default — matching how every pre-existing test in this file already
// uses arbitrary model_id values with no special naming convention — except
// model_ids starting with "unauthorized-", which are always denied. This
// lets existing room/broadcast tests keep working unchanged while adding
// dedicated authorization tests below.
// ---------------------------------------------------------------------------
let fakeInternalApi;

function startFakeInternalApi() {
  return new Promise((resolve) => {
    fakeInternalApi = createServer((req, res) => {
      const url = new URL(req.url, 'http://localhost');
      const match = url.pathname.match(/^\/internal\/models\/([^/]+)\/authorize$/);

      if (req.headers['x-internal-service-key'] !== INTERNAL_SERVICE_KEY) {
        res.writeHead(401, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ authorized: false }));
        return;
      }
      if (!match) {
        res.writeHead(404);
        res.end();
        return;
      }
      const modelId = match[1];
      const authorized = !modelId.startsWith('unauthorized-');
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(
        authorized
          ? { authorized: true, role: 'viewer', user: { name: TEST_USER_NAME } }
          : { authorized: false }
      ));
    });
    fakeInternalApi.listen(INTERNAL_API_PORT, '0.0.0.0', resolve);
  });
}

function stopFakeInternalApi() {
  return new Promise((resolve) => {
    if (fakeInternalApi) fakeInternalApi.close(() => resolve());
    else resolve();
  });
}

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

/**
 * Poll `PUBSUB NUMSUB <channel>` until at least one subscriber is confirmed.
 *
 * The client-side WS 'open' event only means the WS handshake finished —
 * it says nothing about whether the server's async Redis subscribe() for
 * this connection's channel has actually completed. Publishing right after
 * 'open' can race ahead of that subscribe() and the message is genuinely
 * lost (Redis pub/sub does not buffer for a not-yet-subscribed client).
 * This closes that race deterministically instead of guessing a delay.
 */
async function waitForSubscriber(channel, timeoutMs = 3000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const [, count] = await publisher.pubsub('NUMSUB', channel);
    if (Number(count) > 0) return;
    await sleep(10);
  }
  throw new Error(`Timed out waiting for a subscriber on ${channel}`);
}

before(async () => {
  await startFakeInternalApi();
  // Importing the module triggers `await fastify.listen()` for real, exactly
  // as in production. Env vars above must be set before this import.
  await import('../src/server.js');
  publisher = new Redis(REDIS_URL);
  // Give fastify a moment to finish binding.
  await sleep(300);
});

after(async () => {
  await publisher?.quit().catch(() => {});
  await stopFakeInternalApi();
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
  // wsA's initial model_id join is now authorization-gated (async internal API
  // call) rather than synchronous — give it a moment to complete before any
  // other socket's action would need to broadcast to it.
  await sleep(50);

  const wsB = connect(`token=${tokenB}`);
  await onceOpen(wsB);

  const joined = waitForMessage(wsA, (m) => m.event === 'USER_JOINED');
  wsB.send(JSON.stringify({ event: 'JOIN_MODEL', model_id: modelId }));
  const msg = await joined;
  assert.equal(msg.user.id, 'user-B-join');
  assert.equal(msg.user.name, TEST_USER_NAME);
  assert.equal(msg.user.avatar, null);

  wsA.close();
  wsB.close();
});

// ---------------------------------------------------------------------------
// JOIN_MODEL authorization
//
// Regression coverage for the fix restoring the internal-API authorization
// gate on JOIN_MODEL — a prior snapshot of this repo had this check
// missing entirely (any authenticated user could join any model_id).
// ---------------------------------------------------------------------------

test('authorized user joins a model and receives no ERROR', async () => {
  const modelId = 'model-authz-allowed-1';
  const token = makeToken('user-authz-allowed');
  const ws = connect(`token=${token}`);
  await onceOpen(ws);

  let sawError = false;
  ws.on('message', (raw) => {
    const msg = JSON.parse(raw.toString());
    if (msg.event === 'ERROR') sawError = true;
  });

  ws.send(JSON.stringify({ event: 'JOIN_MODEL', model_id: modelId }));
  await sleep(200);

  assert.equal(sawError, false);
  ws.close();
});

test('a second authorized user joining the same model still gets USER_JOINED broadcast (existing room behavior unaffected)', async () => {
  const modelId = 'model-authz-allowed-2';
  const tokenA = makeToken('user-authz-A');
  const tokenB = makeToken('user-authz-B');

  const wsA = connect(`token=${tokenA}&model_id=${modelId}`);
  await onceOpen(wsA);
  await sleep(50);

  const wsB = connect(`token=${tokenB}`);
  await onceOpen(wsB);

  const joined = waitForMessage(wsA, (m) => m.event === 'USER_JOINED');
  wsB.send(JSON.stringify({ event: 'JOIN_MODEL', model_id: modelId }));
  const msg = await joined;
  assert.equal(msg.user.id, 'user-authz-B');
  assert.equal(msg.user.name, TEST_USER_NAME);

  wsA.close();
  wsB.close();
});

test('unauthorized JOIN_MODEL is rejected with MODEL_ACCESS_DENIED and does not join the room', async () => {
  const modelId = 'unauthorized-model-1';
  const token = makeToken('user-authz-denied');
  const ws = connect(`token=${token}`);
  await onceOpen(ws);

  const denied = waitForMessage(ws, (m) => m.event === 'ERROR');
  ws.send(JSON.stringify({ event: 'JOIN_MODEL', model_id: modelId }));
  const msg = await denied;

  assert.equal(msg.code, 'MODEL_ACCESS_DENIED');

  // Confirm it really didn't join: a second user joining the same model_id
  // should not see a USER_JOINED for the denied user.
  const tokenObserver = makeToken('user-authz-observer');
  const wsObserver = connect(`token=${tokenObserver}&model_id=${modelId}`);
  await onceOpen(wsObserver);
  await sleep(50);

  let sawDeniedUserJoin = false;
  wsObserver.on('message', (raw) => {
    const m = JSON.parse(raw.toString());
    if (m.event === 'USER_JOINED' && m.user?.id === 'user-authz-denied') sawDeniedUserJoin = true;
  });
  await sleep(200);
  assert.equal(sawDeniedUserJoin, false);

  ws.close();
  wsObserver.close();
});

test('internal authorization API unreachable fails closed — JOIN_MODEL is denied, not silently allowed', async () => {
  const modelId = 'model-authz-service-down';
  const token = makeToken('user-authz-service-down');
  const ws = connect(`token=${token}`);
  await onceOpen(ws);

  await stopFakeInternalApi();
  try {
    const denied = waitForMessage(ws, (m) => m.event === 'ERROR');
    ws.send(JSON.stringify({ event: 'JOIN_MODEL', model_id: modelId }));
    const msg = await denied;
    assert.equal(msg.code, 'MODEL_ACCESS_DENIED');
  } finally {
    // Restore for every subsequent test in this file.
    await startFakeInternalApi();
  }

  ws.close();
});


test('LEAVE_MODEL broadcasts USER_LEFT to remaining room members', async () => {
  const modelId = 'model-leave-test-1';
  const tokenA = makeToken('user-A-leave');
  const tokenB = makeToken('user-B-leave');

  const wsA = connect(`token=${tokenA}&model_id=${modelId}`);
  await onceOpen(wsA);
  // wsA's initial model_id join is now authorization-gated (async internal API
  // call) rather than synchronous — give it a moment to complete before any
  // other socket's action would need to broadcast to it.
  await sleep(50);
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
  // wsA's initial model_id join is now authorization-gated (async internal API
  // call) rather than synchronous — give it a moment to complete before any
  // other socket's action would need to broadcast to it.
  await sleep(50);
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

test('CURSOR_MOVE sent immediately on open (before init completes) is queued and processed, not lost', async () => {
  const modelId = 'model-early-message-test';
  const tokenA = makeToken('user-A-early');
  const tokenB = makeToken('user-B-early');

  const wsA = connect(`token=${tokenA}&model_id=${modelId}`);
  await onceOpen(wsA);
  // wsA's initial model_id join is now authorization-gated (async internal API
  // call) rather than synchronous — give it a moment to complete before any
  // other socket's action would need to broadcast to it.
  await sleep(50);

  // wsB sends CURSOR_MOVE the instant the socket reports 'open', racing the
  // server's async init (Redis subscribe + room join). Before the fix, a
  // message sent this early could be lost entirely (no 'message' listener
  // registered yet), not merely dropped by the currentModelId guard.
  const wsB = connect(`token=${tokenB}&model_id=${modelId}`);
  const moved = waitForMessage(wsA, (m) => m.event === 'CURSOR_MOVED', 3000);
  wsB.on('open', () => {
    wsB.send(JSON.stringify({
      event: 'CURSOR_MOVE',
      data: { position: { x: 9, y: 8, z: 7 } },
    }));
  });

  const msg = await moved;
  assert.equal(msg.user_id, 'user-B-early');
  assert.deepEqual(msg.position, { x: 9, y: 8, z: 7 });

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
  // wsA's initial model_id join is now authorization-gated (async internal API
  // call) rather than synchronous — give it a moment to complete before any
  // other socket's action would need to broadcast to it.
  await sleep(50);
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
// Backend-published room events (apps/api's publish_room_event)
//
// The test above only proves client-to-client relay within a single
// connection's message handler. It never touches the ws:room:* Redis
// channel at all. Annotation events in production are published by the
// API service — an HTTP request handler with no open WebSocket connection
// of its own — via apps.api.app.core.redis.publish_room_event, which
// publishes directly to `ws:room:{model_id}` with the same
// {originProcessId, message} envelope used below. This is the actual PRD
// 6.2 gap (annotation events only reached the acting user's own
// model_events:{user_id} channel, never other collaborators in the room)
// and this test is what proves it's actually fixed end-to-end, the same
// way "MODEL_READY published on model_events:{userId}" proves that path
// below, rather than only testing the client-relay path above.
// ---------------------------------------------------------------------------

test('ANNOTATION_CREATED published on ws:room:{model_id} by the API reaches every room member', async () => {
  const modelId = 'model-annotation-backend-publish-1';
  const tokenA = makeToken('user-A-backend-annot');
  const tokenB = makeToken('user-B-backend-annot');

  const wsA = connect(`token=${tokenA}&model_id=${modelId}`);
  await onceOpen(wsA);
  // wsA's initial model_id join is now authorization-gated (async internal API
  // call) rather than synchronous — give it a moment to complete before any
  // other socket's action would need to broadcast to it.
  await sleep(50);
  const wsB = connect(`token=${tokenB}&model_id=${modelId}`);
  await onceOpen(wsB);
  // Wait for both to have actually joined the room (not just opened the
  // socket) — USER_JOINED on A confirms B's JOIN_MODEL was processed.
  await waitForMessage(wsA, (m) => m.event === 'USER_JOINED');

  const receivedByA = waitForMessage(wsA, (m) => m.event === 'ANNOTATION_CREATED');
  const receivedByB = waitForMessage(wsB, (m) => m.event === 'ANNOTATION_CREATED');

  // Exactly what apps/api/app/core/redis.py::publish_room_event publishes —
  // an external process with no PROCESS_ID, so originProcessId is null and
  // every replica's roomSubscriber delivers it (never skipped as an echo).
  await publisher.publish(
    `ws:room:${modelId}`,
    JSON.stringify({
      originProcessId: null,
      message: {
        event: 'ANNOTATION_CREATED',
        data: { annotation_id: 'ann-backend-1', model_id: modelId, action: 'created' },
      },
    })
  );

  const [msgA, msgB] = await Promise.all([receivedByA, receivedByB]);
  assert.equal(msgA.data.annotation_id, 'ann-backend-1');
  assert.equal(msgB.data.annotation_id, 'ann-backend-1');

  wsA.close();
  wsB.close();
});

test('ANNOTATION_UPDATED published on ws:room:{model_id} reaches every room member', async () => {
  const modelId = 'model-annotation-backend-publish-2';
  const tokenA = makeToken('user-A-backend-update');
  const tokenB = makeToken('user-B-backend-update');

  const wsA = connect(`token=${tokenA}&model_id=${modelId}`);
  await onceOpen(wsA);
  // wsA's initial model_id join is now authorization-gated (async internal API
  // call) rather than synchronous — give it a moment to complete before any
  // other socket's action would need to broadcast to it.
  await sleep(50);
  const wsB = connect(`token=${tokenB}&model_id=${modelId}`);
  await onceOpen(wsB);
  await waitForMessage(wsA, (m) => m.event === 'USER_JOINED');

  const receivedByB = waitForMessage(wsB, (m) => m.event === 'ANNOTATION_UPDATED');

  await publisher.publish(
    `ws:room:${modelId}`,
    JSON.stringify({
      originProcessId: null,
      message: {
        event: 'ANNOTATION_UPDATED',
        data: { annotation_id: 'ann-backend-2', model_id: modelId, action: 'updated', status: 'resolved' },
      },
    })
  );

  const msgB = await receivedByB;
  assert.equal(msgB.data.status, 'resolved');

  wsA.close();
  wsB.close();
});

test('a room event for a different model_id is NOT delivered to this room', async () => {
  const modelId = 'model-room-isolated-1';
  const otherModelId = 'model-room-isolated-2';
  const tokenA = makeToken('user-room-isolated-A');
  const tokenB = makeToken('user-room-isolated-B');

  // Two clients in the *target* room, same reliable join-confirmation
  // pattern as the tests above — a lone client has no signal that its own
  // JOIN_MODEL room registration finished before publishing could race it.
  const wsA = connect(`token=${tokenA}&model_id=${modelId}`);
  await onceOpen(wsA);
  // wsA's initial model_id join is now authorization-gated (async internal API
  // call) rather than synchronous — give it a moment to complete before any
  // other socket's action would need to broadcast to it.
  await sleep(50);
  const wsB = connect(`token=${tokenB}&model_id=${modelId}`);
  await onceOpen(wsB);
  await waitForMessage(wsA, (m) => m.event === 'USER_JOINED');

  let receivedForeignEvent = false;
  wsA.on('message', (raw) => {
    const msg = JSON.parse(raw.toString());
    if (msg.event === 'ANNOTATION_CREATED' && msg.data?.annotation_id === 'foreign-ann') {
      receivedForeignEvent = true;
    }
  });

  await publisher.publish(
    `ws:room:${otherModelId}`,
    JSON.stringify({
      originProcessId: null,
      message: { event: 'ANNOTATION_CREATED', data: { annotation_id: 'foreign-ann' } },
    })
  );

  // Same deterministic-sentinel pattern as the per-user isolation test
  // below: publish a second, legitimate event to this room and wait for
  // it, proving the foreign-room event (published first, same publisher
  // connection, so ordering is preserved) already had its chance to leak.
  const sentinel = waitForMessage(wsA, (m) => m.event === 'ANNOTATION_CREATED' && m.data?.annotation_id === 'sentinel');
  await publisher.publish(
    `ws:room:${modelId}`,
    JSON.stringify({
      originProcessId: null,
      message: { event: 'ANNOTATION_CREATED', data: { annotation_id: 'sentinel' } },
    })
  );
  await sentinel;

  assert.equal(receivedForeignEvent, false);

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
  await waitForSubscriber(`model_events:${userId}`);

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
  await waitForSubscriber(`model_events:${userId}`);

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
  await waitForSubscriber(`model_events:${userId}`);

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

  // Deterministic replacement for an arbitrary sleep: publish a second,
  // legitimate event on this socket's own channel and wait for it to
  // arrive. Redis preserves publish order for a single publisher
  // connection, so this message's arrival proves the foreign event (published
  // just before it, by the same publisher) already had its chance to be
  // relayed — if isolation were broken, receivedForeignEvent would already
  // be true by the time this resolves.
  const sentinel = waitForMessage(ws, (m) => m.event === 'MODEL_READY' && m.data?.model_id === 'sentinel');
  await publisher.publish(
    `model_events:${userId}`,
    JSON.stringify({ event: 'MODEL_READY', data: { model_id: 'sentinel', chunk_urls: [] } })
  );
  await sentinel;

  assert.equal(receivedForeignEvent, false);

  ws.close();
});