Domain: FastAPI + PostgreSQL service.
Criteria hints: real endpoint(s) with request/response models (Pydantic),
a DB layer (SQLAlchemy or raw + migrations note), input validation at the
boundary, and a health/status check. Include at least one integration-style
test that exercises the endpoint, not just the function underneath it.
