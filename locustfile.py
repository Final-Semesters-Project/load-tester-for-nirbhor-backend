from locust import HttpUser, task, between, events
import random
import json

# locust -f locustfile.py --host http://localhost:8000

# ── Seeder data ────────────────────────────────────────────────────────────────

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

    def on_start(self):
        # Each simulated user picks a random test account
        # This prevents concurrent login collisions even without the jti fix
        suffix = str(random.randint(1, 10)).zfill(3)  # 001 to 010
        self.phone = f"01700000{suffix}"
        self.provider_id = None
        super().on_start()
        self._load_provider_id()

    def _load_provider_id(self):
        """Fetch a real provider_id to use in booking initiation."""
        if not self.token or not self.skill_ids:
            return
        r = self.client.get(
            "/api/v1/search/providers",
            params={
                "skill_id": self.skill_ids[0],
                "seeker_lat": SEEKER_LAT,
                "seeker_lng": SEEKER_LNG,
                "search_radius_km": 5,
            },
            headers=self.auth_headers,
            name="/api/v1/search/providers (setup)",
        )
        if r.status_code == 200:
            providers = r.json().get("providers", [])
            if providers:
                # Pick random provider from results
                self.provider_id = random.choice(providers)["user_id"]

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

    @task(2)
    def initiate_and_cancel_booking(self):
        """
        Full write cycle: initiate a booking then immediately cancel it.
        Tests: INSERT into bookings + UPDATE status
        Requires: at least one provider exists in DB
        """
        if not self.skill_ids:
            return

        # Step 1: Initiate booking (POST — DB write)
        initiate_response = self.client.post(
            "/api/v1/bookings/initiate",
            json={
                "provider_id": self.provider_id,
                "skill_id": random.choice(self.skill_ids),
                "latitude": SEEKER_LAT + random.uniform(-0.01, 0.01),
                "longitude": SEEKER_LNG + random.uniform(-0.01, 0.01),
            },
            headers=self.auth_headers,
            catch_response=True,
            name="/api/v1/bookings/initiate",
        )

        if initiate_response.status_code == 201:
            booking_id = initiate_response.json().get("booking_id")
            initiate_response.success()

            # Step 2: Cancel it immediately (PATCH — DB write)
            # This simulates a seeker who called but provider didn't pick up
            if booking_id:
                self.client.patch(
                    f"/api/v1/bookings/{booking_id}/respond",
                    json={"hired": False, "work_schedule": None},
                    headers=self.auth_headers,
                    name="/api/v1/bookings/{id}/respond",
                )
        else:
            # 409 means already has an open booking — cancel existing first
            if initiate_response.status_code == 409:
                initiate_response.success()  # expected, not a failure
            else:
                initiate_response.failure(
                    f"Initiate failed: {initiate_response.status_code} "
                    f"{initiate_response.text[:100]}"
                )

    @task(1)
    def view_own_profile(self):
        """Seeker opens profile page."""
        self.client.get(
            "/api/v1/users/me",
            headers=self.auth_headers,
        )

    @task(1)
    def submit_urgent_broadcast(self):
        """
        Creates an urgent broadcast — INSERT into urgent_broadcasts + FCM query.
        One of the heaviest write operations (geospatial query + potential FCM).
        """
        if not self.skill_ids:
            return

        self.client.post(
            "/api/v1/urgentBroadcast/broadcast",
            json={
                "skill_id": random.choice(self.skill_ids),
                "latitude": SEEKER_LAT + random.uniform(-0.02, 0.02),
                "longitude": SEEKER_LNG + random.uniform(-0.02, 0.02),
            },
            headers=self.auth_headers,
            name="/api/v1/urgentBroadcast/broadcast",
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
    # phone = PROVIDER_PHONE

    def on_start(self):
        suffix = str(random.randint(1, 5)).zfill(2)   # 01 to 05
        self.phone = f"018000000{suffix}"
        super().on_start()

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

    @task(2)
    def update_location(self):
        """
        Updates provider location — writes to provider_profiles.
        Has a 7-day rate limit in your business logic, so most will 400.
        Still loads the DB with UPDATE attempts and business logic checks.
        """
        self.client.patch(
            "/api/v1/provider/me/update_profile",
            json={
                "latitude": 23.7540 + random.uniform(-0.05, 0.05),
                "longitude": 90.3950 + random.uniform(-0.05, 0.05),
                "working_radius_km": random.choice([3, 5, 7, 10]),
            },
            headers=self.auth_headers,
            name="/api/v1/provider/me/update_profile (location)",
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
    # phone = SEEKER_PHONE   # not used since we override on_start

    def on_start(self):
        """Anonymous users don't log in."""
        self.token = None
        self.category_ids = []
        self.skill_ids = []

    @task(1)
    def health_check(self):
        """Render keep-alive ping / UptimeRobot simulation."""
        self.client.get("/")
