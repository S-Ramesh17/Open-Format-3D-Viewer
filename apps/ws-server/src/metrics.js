// apps/ws-server/src/metrics.js
/**
 * Prometheus metrics for the WebSocket server.
 * Import from server.js and call start() once at startup.
 */

import { createServer } from 'node:http';
import client from 'prom-client';

const register = new client.Registry();
client.collectDefaultMetrics({ register });

// Connected clients
export const connectedClients = new client.Gauge({
  name: 'ws_connected_clients',
  help: 'Number of currently connected WebSocket clients',
  registers: [register],
});

// Messages sent to clients
export const messagesSent = new client.Counter({
  name: 'ws_messages_sent_total',
  help: 'Total WebSocket messages sent to clients',
  registers: [register],
});

// Messages that failed to send (socket not open)
export const messagesDropped = new client.Counter({
  name: 'ws_messages_dropped_total',
  help: 'Total WebSocket messages dropped (socket not open)',
  registers: [register],
});

// Redis events received from pub/sub
export const redisEventsReceived = new client.Counter({
  name: 'ws_redis_events_received_total',
  help: 'Total Redis pub/sub events received',
  registers: [register],
});

// Redis publish failures (from worker → Redis)
export const redisPublishFailures = new client.Counter({
  name: 'ws_redis_publish_failures_total',
  help: 'Total Redis subscriber errors',
  registers: [register],
});

// Client reconnects (approximate — counted on new connection with same userId)
export const reconnections = new client.Counter({
  name: 'ws_reconnections_total',
  help: 'Total WebSocket reconnection events (new connection from existing userId)',
  registers: [register],
});

// Room broadcast messages
export const roomBroadcasts = new client.Counter({
  name: 'ws_room_broadcasts_total',
  help: 'Total room broadcast messages (cursor, annotation sync)',
  labelNames: ['event_type'],
  registers: [register],
});

// Cursor moves dropped by per-client throttle
export const cursorMovesThrottled = new client.Counter({
  name: 'ws_cursor_moves_throttled_total',
  help: 'Total CURSOR_MOVE messages dropped by per-client throttling',
  registers: [register],
});

// JOIN_MODEL authorization check outcomes (does NOT count successful
// authorizations — only failures/denials, so this being nonzero at rest
// is itself a useful alert signal).
export const authorizationFailures = new client.Counter({
  name: 'ws_authorization_failures_total',
  help: 'Total JOIN_MODEL authorization failures (denied, timeout, or network error), labeled by reason',
  labelNames: ['reason'],
  registers: [register],
});

/**
 * Start the Prometheus metrics HTTP server.
 * @param {number} port - defaults to METRICS_PORT env var or 9091
 */
export function startMetricsServer(port = parseInt(process.env.WS_METRICS_PORT || '9091', 10)) {
  const server = createServer(async (req, res) => {
    if (req.url === '/metrics') {
      const metrics = await register.metrics();
      res.writeHead(200, { 'Content-Type': register.contentType });
      res.end(metrics);
    } else if (req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'text/plain' });
      res.end('ok');
    } else {
      res.writeHead(404);
      res.end('not found');
    }
  });
  server.listen(port, '0.0.0.0', () => {
    console.log(`[metrics] WS metrics server listening on :${port}/metrics`);
  });
}