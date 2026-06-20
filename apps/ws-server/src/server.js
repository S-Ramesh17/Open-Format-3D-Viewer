import Fastify from 'fastify';
import websocketPlugin from '@fastify/websocket';
import jwt from 'jsonwebtoken';
import 'dotenv/config';

const PORT = process.env.PORT || 8001;
const JWT_SECRET = process.env.JWT_SECRET || 'dev-secret-change-in-prod';

const fastify = Fastify({ logger: true });

await fastify.register(websocketPlugin);

// Map of modelId -> Set of { socket, userId }
const rooms = new Map();

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

  const modelId = req.query?.model_id || 'default';
  if (!rooms.has(modelId)) rooms.set(modelId, new Set());
  const room = rooms.get(modelId);

  const client = { socket: connection.socket, userId };
  room.add(client);

  broadcast(modelId, { event: 'user:join', userId }, client);

  connection.socket.on('message', (raw) => {
    let msg;
    try {
      msg = JSON.parse(raw.toString());
    } catch {
      return;
    }

    if (!['annotation:update', 'model:sync'].includes(msg.event)) return;

    broadcast(modelId, { event: msg.event, userId, data: msg.data }, client);
  });

  connection.socket.on('close', () => {
    room.delete(client);
    broadcast(modelId, { event: 'user:leave', userId }, client);
    if (room.size === 0) rooms.delete(modelId);
  });
});

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