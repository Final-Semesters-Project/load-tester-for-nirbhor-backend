from locust import HttpUser, task, between, events
import random
import json

# locust -f locustfile.py --host http://localhost:8000

# ── Seeder data ────────────────────────────────────────────────────────────────
# These must exist in your local DB before running the load test.
# Run your seed script first: the test DB should have at least:
#   - 1 seeker account with phone 01700000001
#   - 1 provider account with phone 01800000001
#   - Categories and skills seeded

SEEKER_PHONE = "01700000001"
PROVIDER_PHONE = "01800000001"
PASSWORD = "password123"

# Dhaka coordinates — used for all location-based requests
SEEKER_LAT = 23.7510
SEEKER_LNG = 90.3930


# ── Base class with login logic ────────────────────────────────────────────────

class AuthenticatedUser(HttpUser):
    """
    Base class that handles login and stores the access token.
    All subclasses inherit this — no repeated login code.
    """
    abstract = True
    phone: str = ""

    def on_start(self):
        """Called once when a simulated user starts. Logs in and saves token."""
        self.token = None
        self.category_ids = []
        self.skill_ids = []
        self._login()
        self._load_categories()

    def _login(self):
        """Login using OAuth2 form — matches your /auth/login endpoint."""
        with self.client.post(
            "/api/v1/auth/login",
            data={
                "username": self.phone,
                "password": PASSWORD,
                "grant_type": "password",
            },
            # Tell Locust not to count login failures as task failures
            # (we handle them ourselves below)
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                self.token = response.json().get("access_token")
                response.success()
            else:
                response.failure(
                    f"Login failed for {self.phone}: {response.status_code}"
                )

    def _load_categories(self):
        """Fetch real category and skill IDs from the API to use in tasks."""
        if not self.token:
            return

        r = self.client.get(
            "/api/v1/category/list",
            headers=self.auth_headers,
            # separate name so it doesn't pollute task stats
            name="/api/v1/category/list (setup)",
        )
        if r.status_code == 200:
            cats = r.json()
            self.category_ids = [c["id"] for c in cats]

            # Fetch skills for the first category
            if self.category_ids:
                r2 = self.client.get(
                    f"/api/v1/skill/{self.category_ids[0]}/skills",
                    headers=self.auth_headers,
                    name="/api/v1/skill/{id}/skills (setup)",
                )
                if r2.status_code == 200:
                    self.skill_ids = [s["id"] for s in r2.json()]

    @property
    def auth_headers(self) -> dict:
        """Returns Authorization header. Empty dict if not logged in."""
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}


# ── Seeker user ────────────────────────────────────────────────────────────────

class SeekerUser(AuthenticatedUser):
    """
    Simulates a seeker's typical session:
    - Browse categories (most common action)
    - Search for providers
    - Check booking history
    - Check active booking status

    Weight=3 means 3 seekers spawn for every 1 provider.
    This reflects real usage — more seekers than providers.
    """
    weight = 3
    wait_time = between(1, 4)
    phone = SEEKER_PHONE

    @task(5)
    def browse_categories(self):
        """Most common action — seeker opens home screen."""
        self.client.get(
            "/api/v1/category/list",
            headers=self.auth_headers,
        )

    @task(4)
    def browse_skills(self):
        """Seeker selects a category to see skills."""
        if not self.category_ids:
            return
        cat_id = random.choice(self.category_ids)
        self.client.get(
            f"/api/v1/skill/{cat_id}/skills",
            headers=self.auth_headers,
            # Group all skill requests under one name so stats aren't fragmented
            name="/api/v1/skill/{category_id}/skills",
        )

    @task(3)
    def search_providers(self):
        """Seeker searches for a provider — hits PostGIS geospatial query."""
        if not self.skill_ids:
            return
        skill_id = random.choice(self.skill_ids)
        self.client.get(
            "/api/v1/search/providers",
            params={
                "skill_id":        skill_id,
                "seeker_lat":      SEEKER_LAT,
                "seeker_lng":      SEEKER_LNG,
                "search_radius_km": 5,
            },
            headers=self.auth_headers,
        )

    @task(2)
    def check_booking_history(self):
        """Seeker checks their booking list."""
        self.client.get(
            "/api/v1/bookings/seeker/me",
            params={"page": 1, "page_size": 20},
            headers=self.auth_headers,
        )

    @task(2)
    def check_active_booking(self):
        """Seeker opens app — checks if they have an active booking (modal check)."""
        self.client.get(
            "/api/v1/bookings/seeker/last_active_initiated",
            headers=self.auth_headers,
        )

    @task(1)
    def view_own_profile(self):
        """Seeker opens profile page."""
        self.client.get(
            "/api/v1/users/me",
            headers=self.auth_headers,
        )


# ── Provider user ──────────────────────────────────────────────────────────────

class ProviderUser(AuthenticatedUser):
    """
    Simulates a provider's typical session:
    - Check dashboard
    - Check incoming bookings
    - Toggle availability (less frequent)

    Weight=1 means 1 provider per 3 seekers.
    """
    weight = 1
    wait_time = between(2, 6)  # providers check less frequently than seekers
    phone = PROVIDER_PHONE

    @task(4)
    def check_dashboard(self):
        """Provider opens their dashboard."""
        self.client.get(
            "/api/v1/provider/dashboard",
            headers=self.auth_headers,
        )

    @task(3)
    def check_incoming_bookings(self):
        """Provider checks for incoming work."""
        self.client.get(
            "/api/v1/bookings/provider/me",
            params={"page": 1, "page_size": 20},
            headers=self.auth_headers,
        )

    @task(1)
    def toggle_availability(self):
        """Provider toggles their availability — write operation."""
        # Alternate True/False to avoid getting stuck in one state
        is_available = random.choice([True, False])
        self.client.patch(
            "/api/v1/provider/me/update_profile",
            json={"is_available": is_available},
            headers=self.auth_headers,
        )

    @task(1)
    def view_own_profile(self):
        self.client.get(
            "/api/v1/users/me",
            headers=self.auth_headers,
        )


# ── Unauthenticated traffic ────────────────────────────────────────────────────

class AnonymousUser(AuthenticatedUser):
    """
    Simulates bots, health checks, and users who haven't logged in yet.
    Only hits public endpoints.
    Weight=1 — small fraction of total traffic.
    """
    weight = 1
    wait_time = between(1, 2)
    phone = SEEKER_PHONE   # not used since we override on_start

    def on_start(self):
        """Anonymous users don't log in."""
        self.token = None
        self.category_ids = []
        self.skill_ids = []

    @task(1)
    def health_check(self):
        """Render keep-alive ping / UptimeRobot simulation."""
        self.client.get("/")
