# OpenFormat Backend

Backend service for the OpenFormat platform built with FastAPI, PostgreSQL, Redis, SQLAlchemy, and JWT authentication.

## Features

### Completed (Day 1–3)

- FastAPI backend setup
- PostgreSQL integration
- Alembic migrations
- Async SQLAlchemy
- JWT Authentication
- Refresh Token Management
- Redis Integration
- Google OAuth2
- API Key Authentication
- Request ID Tracking
- Standard Response Envelopes
- Centralized Error Handling
- Security Headers
- CORS Protection
- Rate Limiting

---

## Tech Stack

- Python 3.12
- FastAPI
- SQLAlchemy Async
- Alembic
- PostgreSQL
- Redis
- Pydantic v2
- Authlib
- Poetry

---

## Project Structure

```text
openformat/
├── apps/
│   ├── api/
│   │   ├── app/
│   │   ├── alembic/
│   │   ├── pyproject.toml
│   │   └── poetry.lock
│   ├── worker/
│   └── ws-server/
├── README.md
├── .env.example
└── .gitignore
```

---

## Installation

### Clone Repository

```bash
git clone <repository-url>
cd openformat/apps/api
```

### Install Dependencies

```bash
poetry install
```

### Activate Virtual Environment

```bash
poetry shell
```

---

## Environment Setup

Create environment file:

```bash
cp .env.example .env
```

Update values as needed.

---

## Database Setup

Create PostgreSQL database:

```sql
CREATE DATABASE openformat;
```

Run migrations:

```bash
alembic upgrade head
```

---

## Redis Setup

Using Docker:

```bash
docker run -d --name openformat-redis -p 6379:6379 redis:7-alpine
```

---

## Run Application

```bash
uvicorn app.main:app --reload
```

Application URL:

```text
http://localhost:8000
```

Swagger Documentation:

```text
http://localhost:8000/docs
```

---

## Authentication

### JWT

Access Token:
- Algorithm: HS256
- Expiry: 1 Hour

Refresh Token:
- Expiry: 30 Days
- Stored in Redis

### Authorization Header

```http
Authorization: Bearer <access_token>
```

---

## API Endpoints

### Authentication

| Method | Endpoint |
|----------|----------|
| POST | /auth/register |
| POST | /auth/login |
| POST | /auth/refresh |
| POST | /auth/logout |
| GET | /auth/me |

### API Keys

| Method | Endpoint |
|----------|----------|
| POST | /auth/keys |
| DELETE | /auth/keys/{id} |

---

## Response Format

### Success

```json
{
  "data": {},
  "meta": {
    "request_id": "uuid"
  }
}
```

### Error

```json
{
  "error": {
    "code": "ERROR_CODE",
    "message": "Description",
    "details": {}
  },
  "meta": {
    "request_id": "uuid"
  }
}
```

---

## Security

- JWT Authentication
- Refresh Tokens
- API Key Authentication
- Password Hashing
- Security Headers
- Rate Limiting
- Request Tracking
- CORS Protection

---

## Rate Limits

| Plan | Limit |
|--------|--------|
| Free | 100 requests/hour |
| Pro | 10,000 requests/hour |
| Enterprise | Unlimited |

Response Headers:

```http
X-RateLimit-Limit
X-RateLimit-Remaining
X-RateLimit-Reset
```

---

## Current Progress

### Day 1
- Database foundation
- Alembic setup
- PostgreSQL schema
- Index creation

### Day 2
- Authentication system
- JWT implementation
- Redis integration
- OAuth integration
- API keys

### Day 3
- Response standardization
- Error handling
- Security middleware
- Rate limiting

---

## Upcoming Work

- Projects Module
- RBAC
- Model Upload APIs
- Annotations
- Webhooks
- Realtime Collaboration
