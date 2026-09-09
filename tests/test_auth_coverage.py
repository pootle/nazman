import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from nazman.main import app
from nazman.database import get_db


def _collect_routes(router):
    """Recursively collect all APIRoute objects from a router."""
    routes = []
    for route in router.routes:
        if isinstance(route, APIRoute):
            # Route paths are already absolute (include the full path)
            for method in route.methods:
                if method in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                    routes.append((method, route.path))
        elif hasattr(route, "original_router"):
            # Handle _IncludedRouter (FastAPI's internal wrapper)
            routes.extend(_collect_routes(route.original_router))
        elif hasattr(route, "routes"):
            # Recurse into sub-routers
            routes.extend(_collect_routes(route))
    return routes


@pytest.mark.asyncio
async def test_all_api_routes_require_auth(override_settings, db_engine):
    """Assert that every /api/* route (except login and health) returns 401 without a token.

    This is a safety net to catch new routes that forget the router-level auth dependency.
    """
    # Create a client WITHOUT overriding get_current_user
    from sqlalchemy.orm import sessionmaker
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=db_engine)

    def override_get_db():
        session = TestSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    # Do NOT override get_current_user - we want to test the real auth

    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            # Collect all routes from the app
            all_routes = _collect_routes(app.router)

            # Filter to /api/* routes, excluding login and health
            routes = []
            for method, path in all_routes:
                if path.startswith("/api/"):
                    if path in ("/api/auth/login", "/api/system/health"):
                        continue
                    routes.append((method, path))

            assert len(routes) > 0, "No API routes found to test"

            # Test each route without authentication
            for method, path in routes:
                # Replace path parameters with dummy values
                test_path = path.replace("{disk_id}", "1").replace("{pool_name}", "test")
                test_path = test_path.replace("{dataset_name:path}", "test/data")
                test_path = test_path.replace("{dataset_name}", "test")
                test_path = test_path.replace("{snapshot_name:path}", "test@snap")
                test_path = test_path.replace("{backup_disk_id}", "1")
                test_path = test_path.replace("{run_id}", "1")
                test_path = test_path.replace("{device_path:path}", "test")

                if method == "GET":
                    response = client.get(test_path)
                elif method == "POST":
                    response = client.post(test_path, json={})
                elif method == "PUT":
                    response = client.put(test_path, json={})
                elif method == "DELETE":
                    response = client.delete(test_path)
                elif method == "PATCH":
                    response = client.patch(test_path, json={})
                else:
                    continue

                # Should return 401 Unauthorized (not 403, not 404, not 422)
                assert response.status_code == 401, (
                    f"{method} {path} returned {response.status_code}, expected 401. "
                    f"This route may be missing the router-level auth dependency."
                )
    finally:
        app.dependency_overrides.clear()
