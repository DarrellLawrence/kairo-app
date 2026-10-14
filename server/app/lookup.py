"""'Use my current location' (only when the owner taps it): weather and the
nearest public transport departures. Read-only, nothing is stored.

Privacy: the coordinates are rounded before they leave the PC (weather to
~10 km, stations to ~1 km), are not logged and are never kept. Only these two
hosts are ever called from here.
"""

from __future__ import annotations

import httpx

WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
TRANSPORT = "https://transport.opendata.ch/v1"
ALLOWED_HOSTS = {"api.open-meteo.com", "transport.opendata.ch"}
WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "cloudy", 45: "fog", 48: "fog", 51: "light drizzle",
       53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain",
       67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "showers",
       81: "showers", 82: "heavy showers", 85: "snow showers", 86: "snow showers", 95: "thunderstorm",
       96: "thunderstorm with hail", 99: "thunderstorm with hail"}


class LookupError_(Exception):
    pass


def check_coords(lat, lon) -> tuple[float, float]:
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        raise LookupError_("latitude and longitude must be numbers")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise LookupError_("coordinates out of range")
    return lat, lon


class Lookup:
    def __init__(self, *, timezone: str, transport: httpx.BaseTransport | None = None):
        self.timezone = timezone
        self._transport = transport

    def _client(self) -> httpx.Client:
        def only_allowed(request: httpx.Request):
            if request.url.host not in ALLOWED_HOSTS:
                raise LookupError_(f"host not allowed: {request.url.host}")
        return httpx.Client(timeout=8, transport=self._transport, event_hooks={"request": [only_allowed]})

    def weather(self, lat: float, lon: float) -> dict:
        with self._client() as c:
            r = c.get(WEATHER_URL, params={
                "latitude": round(lat, 1), "longitude": round(lon, 1), "timezone": self.timezone, "forecast_days": 1,
                "current": "temperature_2m,weather_code,precipitation",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"})
            r.raise_for_status()
            d = r.json()
        cur, day = d.get("current") or {}, d.get("daily") or {}

        def first(key):
            v = day.get(key) or [None]
            return v[0]

        return {"now_c": cur.get("temperature_2m"), "now": WMO.get(cur.get("weather_code"), "–"),
                "min_c": first("temperature_2m_min"), "max_c": first("temperature_2m_max"),
                "rain_pct": first("precipitation_probability_max"), "today": WMO.get(first("weather_code"), "–")}

    def stations(self, lat: float, lon: float, *, n: int = 2, departures: int = 4) -> list[dict]:
        out = []
        with self._client() as c:
            r = c.get(f"{TRANSPORT}/locations", params={"x": round(lat, 2), "y": round(lon, 2), "type": "station"})
            r.raise_for_status()
            stations = [st for st in (r.json().get("stations") or []) if st.get("id")][:n]
            for st in stations:
                b = c.get(f"{TRANSPORT}/stationboard", params={"id": st["id"], "limit": departures})
                b.raise_for_status()
                deps = []
                for j in b.json().get("stationboard") or []:
                    stop = j.get("stop") or {}
                    deps.append({"line": f"{j.get('category') or ''}{j.get('number') or ''}".strip(), "to": j.get("to"),
                                 "time": stop.get("departure"), "platform": stop.get("platform"), "delay": stop.get("delay")})
                out.append({"name": st.get("name"), "distance_m": st.get("distance"), "departures": deps})
        return out
