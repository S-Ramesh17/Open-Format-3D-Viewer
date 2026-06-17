### Response Format

All responses follow a standard envelope:

**Success**
```json
{
  "data": {},
  "meta": {
    "request_id": "36fa753d-08a2-4efb-a7e3-d0b394221f3d"
  }
}
```

**Error**
```json
{
  "error": {
    "code": "AUTHENTICATION_ERROR",
    "message": "Invalid credentials",
    "details": {}
  },
  "meta": {
    "request_id": "efa8b771-546e-44db-bf9e-a5423da23110"
  }
}
```

### Authentication Endpoints

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | /auth/register | No | Create account |
| POST | /auth/login | No | Login, get token pair |
| POST | /auth/refresh | No | Rotate refresh token |
| POST | /auth/logout | No | Revoke refresh token |
| GET | /auth/me | JWT | Current user profile |
| GET | /auth/google | No | Google OAuth redirect |
| GET | /auth/google/callback | No | Google OAuth callback |
| POST | /auth/keys | JWT | Create API key |
| GET | /auth/keys | JWT | List API keys |
| DELETE | /auth/keys/{id} | JWT | Revoke API key |
| GET | /health | No | Health check |

### Authorization Header

```http
Authorization: Bearer <access_token>
```

---

## Authentication

### JWT Tokens

| Token | Algorithm | TTL | Storage |
|-------|-----------|-----|---------|
| Access token | HS256 | 1 hour | Memory only |
| Refresh token | HS256 | 30 days | Redis + client localStorage |

### API Keys

- Prefix: `ofv_`
- Storage: SHA-256 hash only — plaintext shown once at creation
- Usage: same `Authorization: Bearer ofv_...` header as JWT

### Rate Limits

| Plan | Limit | Headers |
|------|-------|---------|
| Free | 100 req/hour | X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset |
| Pro | 10,000 req/hour | same |
| Enterprise | Unlimited | X-RateLimit-Limit: unlimited |

### Error Codes

| Code | HTTP | Description |
|------|------|-------------|
| VALIDATION_ERROR | 422 | Bad request body |
| AUTHENTICATION_ERROR | 401 | Invalid/missing/expired token |
| AUTHORIZATION_ERROR | 403 | Insufficient permissions |
| NOT_FOUND | 404 | Resource not found |
| CONFLICT | 409 | Resource already exists |
| RATE_LIMIT_EXCEEDED | 429 | Too many requests |
| STORAGE_ERROR | 502 | External storage failure |
| PROCESSING_ERROR | 422 | Model conversion failure |
| INTERNAL_ERROR | 500 | Unexpected server error |

---

## Database Schema

10 tables implemented:

| Table | Purpose |
|-------|---------|
| users | User accounts |
| projects | Project containers |
| project_members | Project access control (roles: viewer, editor, admin) |
| models | 3D model file records |
| model_metadata | BIM metadata (JSONB) |
| model_elements | IFC elements (one row per element) |
| annotations | 3D pins on models |
| annotation_comments | Comments on annotations |
| api_keys | Hashed API keys |
| webhooks | User webhook registrations |

---

## Development Progress

### Day 1 — Database Foundation ✅
- Monorepo structure
- PostgreSQL schema — 10 tables, 28 indexes
- SQLAlchemy async engine
- Alembic migrations

### Day 2 — Authentication System ✅
- JWT access + refresh tokens
- Redis refresh token storage with rotation
- Google OAuth2
- API key generation and validation
- 10 auth endpoints

### Day 3 — Security and Platform Foundation ✅
- Request ID middleware
- Standard response envelope
- Centralized error handling (9 error codes)
- Security headers (CSP, HSTS, X-Frame-Options)
- CORS — explicit origin allowlist
- Pydantic schema hardening
- Redis-backed rate limiting

### Upcoming
- Day 4: Projects API, RBAC
- Day 5: Model upload, S3 integration
- Day 6: Annotations API
- Day 7: WebSockets, real-time collaboration
- Day 8: Webhooks, deployment

---

## Contributing

### Branch Naming

```text
feature/day-4-projects-api
feature/day-5-model-upload
feature/day-6-annotations

fix/refresh-token-rotation
fix/rate-limit-cache
```

### Commit Format

```text
feat(auth): add Google OAuth account linking
feat(projects): implement project member invitations

fix(rate-limit): correct plan cache TTL
fix(jwt): validate refresh token expiry

docs(readme): update setup instructions
```
