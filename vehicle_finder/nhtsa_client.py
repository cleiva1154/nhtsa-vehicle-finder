"""Thin client for the two public NHTSA APIs this project uses.

* NCAP Safety Ratings  (https://api.nhtsa.gov/SafetyRatings)
  - per-trim body style, drivetrain, crash-test stars, driver-assist tech,
    recall / complaint counts.
* vPIC Vehicle API     (https://vpic.nhtsa.dot.gov/api/)
  - the official "vehicle type" for each model (Passenger Car, MPV, Truck...).
    Crossovers are registered as MPVs, which lets us catch crossovers that
    NCAP labels as wagons or hatchbacks (e.g. Subaru Crosstrek).

Responses are cached on disk as JSON so the catalog only has to be built once.
Only the standard library is used.
"""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SAFETY_BASE = "https://api.nhtsa.gov/SafetyRatings"
VPIC_BASE = "https://vpic.nhtsa.dot.gov/api/vehicles"

MPV = "multipurpose passenger vehicle (mpv)"


class NHTSAError(RuntimeError):
    pass


def ssl_context() -> ssl.SSLContext:
    """Verified TLS context that also works on python.org macOS builds.

    Those builds ship without CA certs until "Install Certificates.command" is
    run, so fall back to certifi or the OS bundle instead of failing.
    """
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    ctx = ssl.create_default_context()
    default_cafile = ssl.get_default_verify_paths().cafile
    if not (default_cafile and os.path.exists(default_cafile)):
        for bundle in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
            if os.path.exists(bundle):
                ctx.load_verify_locations(cafile=bundle)
                break
    return ctx


class NHTSAClient:
    def __init__(
        self,
        cache_dir: str | Path = ".cache",
        timeout: float = 20.0,
        retries: int = 4,
        offline: bool = False,
        min_interval: float = 0.35,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.retries = retries
        self.offline = offline
        self._ssl = ssl_context()
        # Global throttle shared by all threads: NHTSA's CDN starts returning
        # 403s if it sees bursts, so keep to roughly 3 requests per second.
        self.min_interval = min_interval
        self._throttle = threading.Lock()
        self._next_slot = 0.0
        self._throttled_streak = 0   # consecutive 403/429s across all threads

    def _wait_turn(self) -> None:
        with self._throttle:
            now = time.monotonic()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self.min_interval
        if wait > 0:
            time.sleep(wait)

    # ------------------------------------------------------------------ core
    def _get(self, url: str) -> list[dict[str, Any]]:
        """GET a NHTSA URL and return its ``Results`` list (cached)."""
        key = hashlib.sha1(url.encode()).hexdigest()
        cache_file = self.cache_dir / f"{key}.json"
        if cache_file.exists():
            return json.loads(cache_file.read_text())["Results"]
        if self.offline:
            raise NHTSAError(f"Not cached and running offline: {url}")
        if self._throttled_streak >= 6:
            raise NHTSAError(f"NHTSA is rate-limiting this machine; skipped {url}")

        last_err: Exception | None = None
        for attempt in range(self.retries):
            self._wait_turn()
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "nhtsa-vehicle-finder/1.0"}
                )
                with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl) as resp:
                    payload = json.load(resp)
                self._throttled_streak = 0
                results = payload.get("Results") or []
                cache_file.write_text(json.dumps({"url": url, "Results": results}))
                return results
            except urllib.error.HTTPError as err:
                last_err = err
                if err.code in (403, 429, 503):   # throttled: back off hard
                    self._throttled_streak += 1
                    if self._throttled_streak >= 6:
                        break
                    time.sleep(15 * (attempt + 1))
                else:
                    time.sleep(2 ** attempt)
            except (OSError, json.JSONDecodeError) as err:  # timeouts, resets
                last_err = err
                time.sleep(2 ** attempt)
        raise NHTSAError(f"Failed after {self.retries} attempts: {url}") from last_err

    @staticmethod
    def _q(part: str) -> str:
        # NHTSA rejects %2F, but accepts a literal slash ("HORNET R/T EAWD").
        return urllib.parse.quote(str(part).strip(), safe="/")

    # --------------------------------------------------------- safety (NCAP)
    def safety_makes(self, year: int) -> list[str]:
        rows = self._get(f"{SAFETY_BASE}/modelyear/{year}?format=json")
        return [r["Make"] for r in rows]

    def safety_models(self, year: int, make: str) -> list[str]:
        rows = self._get(f"{SAFETY_BASE}/modelyear/{year}/make/{self._q(make)}?format=json")
        return [r["Model"] for r in rows]

    def safety_variants(self, year: int, make: str, model: str) -> list[dict[str, Any]]:
        """Each trim NHTSA crash-tested: ``{VehicleDescription, VehicleId}``."""
        return self._get(
            f"{SAFETY_BASE}/modelyear/{year}/make/{self._q(make)}"
            f"/model/{self._q(model)}?format=json"
        )

    def safety_details(self, vehicle_id: int) -> dict[str, Any]:
        rows = self._get(f"{SAFETY_BASE}/VehicleId/{vehicle_id}?format=json")
        return rows[0] if rows else {}

    # ------------------------------------------------------------------ vPIC
    def vpic_models(self, make: str, year: int, vehicle_type: str) -> list[str]:
        rows = self._get(
            f"{VPIC_BASE}/GetModelsForMakeYear/make/{self._q(make)}"
            f"/modelyear/{year}/vehicletype/{self._q(vehicle_type)}?format=json"
        )
        return [r["Model_Name"] for r in rows]
