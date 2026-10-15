"""Knowledge and media skills that need no API keys: weather, news, web answers,
YouTube playback and WhatsApp drafts."""

from __future__ import annotations

import re
import webbrowser
import xml.etree.ElementTree as ET
from urllib.parse import quote, quote_plus

import httpx

TIMEOUT = 15.0
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Naman/0.5"}

WEATHER_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain",
    65: "heavy rain", 71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
    81: "rain showers", 82: "violent rain showers", 95: "thunderstorm", 96: "thunderstorm with hail",
    99: "thunderstorm with hail",
}


def weather(city: str, days: int = 1) -> dict | str:
    days = max(1, min(7, int(days)))
    geo = httpx.get("https://geocoding-api.open-meteo.com/v1/search",
                    params={"name": city, "count": 1}, timeout=TIMEOUT).json()
    if not geo.get("results"):
        return f"error: couldn't find a place called {city!r}"
    place = geo["results"][0]
    data = httpx.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto",
            "forecast_days": days,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        },
        timeout=TIMEOUT,
    ).json()
    cur, daily = data["current"], data["daily"]
    return {
        "place": f"{place['name']}, {place.get('admin1', '')}, {place.get('country', '')}".replace(", ,", ","),
        "now": {
            "temp_c": cur["temperature_2m"], "feels_like_c": cur["apparent_temperature"],
            "humidity": cur["relative_humidity_2m"], "wind_kmh": cur["wind_speed_10m"],
            "sky": WEATHER_CODES.get(cur["weather_code"], "unknown"),
        },
        "days": [
            {"date": d, "sky": WEATHER_CODES.get(c, "unknown"), "min_c": lo, "max_c": hi, "rain_chance": p}
            for d, c, lo, hi, p in zip(daily["time"], daily["weather_code"], daily["temperature_2m_min"],
                                       daily["temperature_2m_max"], daily["precipitation_probability_max"])
        ],
    }


def news(topic: str | None = None, limit: int = 6) -> list[dict] | str:
    base = "https://news.google.com/rss"
    region = "hl=en-IN&gl=IN&ceid=IN:en"
    url = f"{base}/search?q={quote(topic)}&{region}" if topic else f"{base}?{region}"
    try:
        root = ET.fromstring(httpx.get(url, headers=UA, timeout=TIMEOUT, follow_redirects=True).content)
    except (httpx.HTTPError, ET.ParseError) as exc:
        return f"error: couldn't load news ({exc.__class__.__name__})"
    items = []
    for item in root.iter("item"):
        title = item.findtext("title") or ""
        source = item.findtext("source") or ""
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        items.append({"headline": title, "source": source, "published": item.findtext("pubDate")})
        if len(items) >= limit:
            break
    return items or "no headlines found"


def web_lookup(query: str) -> list[dict] | str:
    """Search the web and return snippets, for questions about facts, people, scores, prices."""
    from ddgs import DDGS

    try:
        results = DDGS().text(query, max_results=5)
    except Exception as exc:
        return f"error: web search failed ({exc.__class__.__name__})"
    return [{"title": r.get("title"), "snippet": r.get("body"), "url": r.get("href")} for r in results] or "no results"


def first_youtube_video(query: str) -> str | None:
    html = httpx.get(f"https://www.youtube.com/results?search_query={quote_plus(query)}",
                     headers=UA, timeout=TIMEOUT, follow_redirects=True).text
    match = re.search(r'"videoId":"([\w-]{11})"', html)
    return match.group(1) if match else None


def play_youtube(query: str) -> str:
    try:
        video = first_youtube_video(query)
    except httpx.HTTPError:
        video = None
    if video:
        webbrowser.open(f"https://www.youtube.com/watch?v={video}")
        return f"playing the top YouTube result for {query!r}"
    webbrowser.open(f"https://www.youtube.com/results?search_query={quote_plus(query)}")
    return f"opened YouTube results for {query!r} (couldn't pick a video automatically)"


def whatsapp_draft(message: str, phone: str | None = None) -> str:
    """Opens WhatsApp with the message typed in. The user presses send."""
    digits = re.sub(r"\D", "", phone or "")
    if digits and len(digits) == 10:
        digits = "91" + digits  # Indian mobile without country code
    target = f"phone={digits}&" if digits else ""
    webbrowser.open(f"whatsapp://send?{target}text={quote(message)}")
    who = f"+{digits}" if digits else "a contact you pick"
    return f"opened WhatsApp with the message for {who}; it is NOT sent until the user presses send"
