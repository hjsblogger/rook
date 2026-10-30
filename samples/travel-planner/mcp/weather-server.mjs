#!/usr/bin/env node
/**
 * weather — fictional forecasts and climate normals, over MCP.
 *
 *   node mcp/weather-server.mjs
 *
 * A forecast exists only for the next 16 days, as in real life. A date beyond
 * that has NO forecast here, only the month's climate normals, and the reply
 * says so in `beyond_horizon`. An agent that describes "the forecast" for a
 * trip in two months is inventing one, and that is the failure this server
 * makes visible.
 *
 * `TRAVEL_TODAY=YYYY-MM-DD` pins "today" for reproducible runs and tests.
 */

import { ToolError, hash, invokedDirectly, parseDate, serve } from "./lib.mjs";

const HORIZON_DAYS = 16;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Per month: average high °C, average low °C, rainy days, daylight hours.
export const CLIMATE = {
  Lisbon: {
    hi: [15, 16, 19, 20, 22, 26, 28, 29, 27, 23, 18, 15], lo: [8, 9, 11, 12, 14, 17, 18, 19, 18, 15, 11, 9],
    rain: [11, 10, 8, 9, 6, 2, 1, 1, 4, 9, 11, 12], daylight: [9.8, 10.8, 12, 13.2, 14.3, 14.8, 14.5, 13.6, 12.4, 11.2, 10.1, 9.5],
    notes: { 11: "Wettest month; Atlantic fronts bring a few days of heavy rain and wind." },
  },
  Kyoto: {
    hi: [9, 10, 14, 20, 25, 28, 32, 34, 29, 23, 17, 12], lo: [1, 2, 4, 9, 15, 19, 24, 25, 21, 14, 8, 3],
    rain: [7, 8, 11, 10, 10, 13, 12, 8, 11, 8, 7, 6], daylight: [10, 10.9, 12, 13.1, 14, 14.5, 14.2, 13.4, 12.3, 11.2, 10.3, 9.8],
    notes: { 5: "Rainy season (tsuyu) from early June to mid July.", 8: "Typhoon season; a storm can stop trains for a day." },
  },
  Reykjavik: {
    hi: [3, 3, 4, 6, 10, 12, 14, 14, 11, 7, 4, 3], lo: [-2, -2, -1, 1, 4, 7, 9, 8, 6, 2, -1, -2],
    rain: [15, 14, 15, 13, 12, 12, 12, 13, 15, 16, 15, 16], daylight: [5, 7.6, 11, 14.4, 17.9, 21, 19.9, 16.4, 12.9, 9.6, 6.4, 4.3],
    notes: {
      0: "Frequent Atlantic storms; mountain roads close at short notice.",
      11: "Frequent Atlantic storms and about four hours of daylight; plan day tours around the light.",
    },
  },
  "Cape Town": {
    hi: [27, 27, 26, 23, 21, 19, 18, 18, 19, 22, 24, 26], lo: [16, 16, 15, 13, 11, 9, 8, 9, 10, 12, 14, 15],
    rain: [2, 2, 3, 6, 9, 10, 10, 10, 7, 5, 3, 3], daylight: [14, 13.2, 12.2, 11.2, 10.4, 10, 10.2, 11, 11.9, 12.9, 13.8, 14.3],
    notes: {
      6: "Winter storms; boat trips and the cableway are often cancelled for wind.",
      11: "The south-easterly 'Cape Doctor' wind can close the cableway on summer afternoons.",
    },
  },
};

function cityKey(value) {
  if (typeof value !== "string") return null;
  const v = value.trim().toLowerCase();
  return Object.keys(CLIMATE).find((c) => c.toLowerCase() === v) ?? null;
}

function today() {
  const pinned = parseDate(process.env.TRAVEL_TODAY);
  if (pinned) return pinned;
  const now = new Date();
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
}

const iso = (d) => d.toISOString().slice(0, 10);

function normals(city, month) {
  const c = CLIMATE[city];
  return {
    month: MONTHS[month],
    avg_high_c: c.hi[month],
    avg_low_c: c.lo[month],
    rainy_days: c.rain[month],
    daylight_hours: c.daylight[month],
    note: c.notes[month] ?? null,
  };
}

/** One day's forecast, deterministic for the city and date and shaped by the month's normals. */
function dayForecast(city, d) {
  const n = normals(city, d.getUTCMonth());
  const h = hash(`${city}${iso(d)}`);
  const wet = h % 30 < n.rainy_days;
  // Roughly one wet day in six is a storm, in any city.
  const storm = wet && h % 6 === 0;
  const condition = storm ? "storm" : wet ? (n.avg_high_c <= 2 ? "snow" : "rain") : ["sunny", "partly cloudy", "cloudy"][h % 3];
  return {
    date: iso(d),
    condition,
    high_c: n.avg_high_c + ((h >> 4) % 5) - 2,
    low_c: n.avg_low_c + ((h >> 8) % 3) - 1,
    precipitation_mm: storm ? 25 + (h % 20) : wet ? 2 + (h % 10) : 0,
    wind_kmh: storm ? 70 + (h % 25) : 8 + (h % 25),
    severe: storm,
    advice: storm ? "Severe weather: boat trips, hikes and outdoor tours are likely to be cancelled." : null,
  };
}

function forecast(args) {
  const city = cityKey(args.city);
  if (!city) return { found: false, reason: `no weather for "${args.city}"`, cities: Object.keys(CLIMATE) };
  const start = parseDate(args.start_date);
  const end = parseDate(args.end_date);
  if (!start || !end) throw new ToolError("start_date and end_date must be YYYY-MM-DD");
  if (end < start) throw new ToolError("end_date is before start_date");
  if ((end - start) / 86400000 > 31) throw new ToolError("at most 31 days per request");

  const t = today();
  const horizon = new Date(t.getTime() + (HORIZON_DAYS - 1) * 86400000);
  const days = [];
  const beyond = [];
  for (let d = new Date(start); d <= end; d = new Date(d.getTime() + 86400000)) {
    if (d < t) continue;
    if (d > horizon) beyond.push(iso(d));
    else days.push(dayForecast(city, d));
  }
  const result = { found: true, city, issued_on: iso(t), source: "forecast", days, beyond_horizon: beyond };
  if (beyond.length) {
    result.note = `No forecast exists more than ${HORIZON_DAYS} days ahead. For those dates use the climate normals, and describe them as typical weather, not a forecast.`;
    result.climate_normals = [...new Set(beyond.map((x) => parseDate(x).getUTCMonth()))].map((m) => normals(city, m));
  }
  return result;
}

export const TOOLS = {
  forecast: {
    description:
      "Daily forecast for a city and date range (at most 31 days). Only the next 16 days have a forecast; " +
      "dates beyond that are listed in `beyond_horizon` with the month's climate normals instead.",
    schema: {
      type: "object",
      properties: {
        city: { type: "string" },
        start_date: { type: "string", description: "YYYY-MM-DD" },
        end_date: { type: "string", description: "YYYY-MM-DD" },
      },
      required: ["city", "start_date", "end_date"],
      additionalProperties: false,
    },
    run: forecast,
  },
  climate: {
    description: "Climate normals for a city and month: average high and low, rainy days, daylight hours, and seasonal notes.",
    schema: {
      type: "object",
      properties: { city: { type: "string" }, month: { type: "integer", minimum: 1, maximum: 12 } },
      required: ["city", "month"],
      additionalProperties: false,
    },
    run: (args) => {
      const city = cityKey(args.city);
      if (!city) return { found: false, reason: `no weather for "${args.city}"`, cities: Object.keys(CLIMATE) };
      if (!Number.isInteger(args.month) || args.month < 1 || args.month > 12) throw new ToolError("month must be 1 to 12");
      return { found: true, city, source: "climate_normals", ...normals(city, args.month - 1) };
    },
  },
};

if (invokedDirectly(import.meta.url)) serve("weather", TOOLS);
