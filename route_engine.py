"""
Route engine — self-contained, no Excel dependency.
All inputs come from Google Calendar; outputs are PDF files.
"""

import pickle
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import googlemaps
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
LATE_PENALTY_PER_MIN = 1000  # heavily penalise missing a booked window


# ── Google Calendar ──────────────────────────────────────────────────────────

def get_calendar_service(credentials_path: Path, token_path: Path):
    creds = None
    if token_path.exists():
        with open(token_path, "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "wb") as f:
            pickle.dump(creds, f)
    return build("calendar", "v3", credentials=creds)


def get_google_email(token_path: Path) -> str | None:
    if not token_path.exists():
        return None
    try:
        with open(token_path, "rb") as f:
            creds = pickle.load(f)
        if creds and creds.valid:
            service = build("oauth2", "v2", credentials=creds)
            info = service.userinfo().get().execute()
            return info.get("email")
    except Exception:
        pass
    return None


def fetch_jobs(target_date: date, service, job_keyword: str, delivery_keyword: str) -> list[dict]:
    """
    Reads the day's calendar events and returns a list of job dicts.
    An event is included if its title contains job_keyword.
    It is classified as a delivery if the title also contains delivery_keyword,
    otherwise as a pickup.
    """
    day_start = datetime.combine(target_date, datetime.min.time()).isoformat() + "Z"
    day_end = datetime.combine(target_date + timedelta(days=1), datetime.min.time()).isoformat() + "Z"

    events = service.events().list(
        calendarId="primary",
        timeMin=day_start,
        timeMax=day_end,
        singleEvents=True,
        orderBy="startTime",
    ).execute().get("items", [])

    jobs = []
    for ev in events:
        title = ev.get("summary", "").lower()
        if job_keyword.lower() not in title:
            continue

        job_type = "delivery" if delivery_keyword.lower() in title else "pickup"

        m = re.search(r"x(\d+)", title)
        qty = int(m.group(1)) if m else 1

        address = (ev.get("location", "") or ev.get("description", "")).strip()
        if not address:
            continue

        start_str = ev["start"].get("dateTime", ev["start"].get("date"))
        end_str = ev["end"].get("dateTime", ev["end"].get("date"))
        slot_start = datetime.fromisoformat(start_str.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
        slot_end = datetime.fromisoformat(end_str.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)

        jobs.append({
            "type": job_type,
            "address": address,
            "qty": qty,
            "slot_start": slot_start,
            "slot_end": slot_end,
        })

    return jobs


# ── Distance matrix ──────────────────────────────────────────────────────────

import math

def _distance_km(lat1, lng1, lat2, lng2) -> float:
    """Approximate distance in km between two lat/lng points."""
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng/2)**2
    return 6371 * 2 * math.asin(math.sqrt(a))


def _geocode_to_latlng(gmaps, address: str, region: str = "", anchor: tuple = None) -> tuple | str:
    """Geocode an address to (lat, lng). Falls back to raw string if geocoding fails.
    anchor: (lat, lng) of starting location — results >200km away are rejected and retried with country suffix."""
    import unicodedata
    normalised = unicodedata.normalize("NFKD", address).encode("ascii", "ignore").decode("ascii")

    COUNTRY_NAMES = {"sg": "Singapore", "my": "Malaysia", "au": "Australia",
                     "gb": "UK", "us": "USA", "id": "Indonesia", "th": "Thailand"}
    country_suffix = COUNTRY_NAMES.get(region, "")

    attempts = [normalised, address]
    if country_suffix:
        attempts += [f"{normalised}, {country_suffix}", f"{address}, {country_suffix}"]

    kwargs = {"region": region} if region else {}
    for attempt in attempts:
        try:
            results = gmaps.geocode(attempt, **kwargs)
            if results:
                loc = results[0]["geometry"]["location"]
                lat, lng = loc["lat"], loc["lng"]
                if anchor and _distance_km(anchor[0], anchor[1], lat, lng) > 200:
                    continue  # result is in the wrong country — try next attempt
                return (lat, lng)
        except Exception:
            pass
    raise ValueError(
        f"Could not locate \"{address}\" on Google Maps. "
        f"Please add a full address (including postal code) to that calendar event and try again."
    )


def _geocode_region(gmaps, address: str) -> tuple[str, tuple | None]:
    """Return (country_code, (lat, lng)) from geocoding the starting address."""
    try:
        results = gmaps.geocode(address)
        if results:
            loc = results[0]["geometry"]["location"]
            anchor = (loc["lat"], loc["lng"])
            for component in results[0].get("address_components", []):
                if "country" in component["types"]:
                    return component["short_name"].lower(), anchor
            return "", anchor
    except Exception:
        pass
    return "", None


def _polar_angle(depot_lat: float, depot_lng: float, lat: float, lng: float) -> float:
    """Clockwise angle from north (0°=N, 90°=E, 180°=S, 270°=W)."""
    dlat = lat - depot_lat
    dlng = lng - depot_lng
    return math.degrees(math.atan2(dlng, dlat)) % 360


def sweep_sort_jobs(jobs: list[dict], depot_coord: tuple, job_coords: list[tuple]) -> tuple[list[dict], list[tuple]]:
    """Within each distinct time window, sort stops by polar angle from depot.
    Stops in different windows keep their relative window ordering."""
    from collections import defaultdict

    window_key = lambda j: (j["slot_start"], j["slot_end"])

    # Collect unique windows in order of first appearance
    seen_windows: list = []
    seen_set: set = set()
    for job in jobs:
        k = window_key(job)
        if k not in seen_set:
            seen_windows.append(k)
            seen_set.add(k)

    groups: dict = defaultdict(list)
    for job, coord in zip(jobs, job_coords):
        groups[window_key(job)].append((job, coord))

    depot_lat, depot_lng = depot_coord
    result_jobs, result_coords = [], []
    for wk in seen_windows:
        group = groups[wk]
        if len(group) > 1:
            group.sort(key=lambda t: _polar_angle(depot_lat, depot_lng, t[1][0], t[1][1]))
        for job, coord in group:
            result_jobs.append(job)
            result_coords.append(coord)

    return result_jobs, result_coords


def _geocode_all(gmaps, locations: list[str], region: str = "", anchor: tuple = None) -> list[tuple]:
    return [_geocode_to_latlng(gmaps, loc, region, anchor) for loc in locations]


def build_matrix_from_coords(gmaps, resolved: list[tuple]) -> tuple[list[list[int]], list[list[float]]]:
    """Returns (time_matrix_minutes, dist_matrix_km). One API call per row."""
    n = len(resolved)
    time_matrix = [[0]   * n for _ in range(n)]
    dist_matrix = [[0.0] * n for _ in range(n)]
    for i, origin in enumerate(resolved):
        result = gmaps.distance_matrix([origin], resolved, mode="driving", units="metric")
        for j, el in enumerate(result["rows"][0]["elements"]):
            if el["status"] == "OK":
                time_matrix[i][j] = round(el["duration"]["value"] / 60)
                dist_matrix[i][j] = el["distance"]["value"] / 1000  # km
            else:
                time_matrix[i][j] = 9999
                dist_matrix[i][j] = 9999.0
    return time_matrix, dist_matrix


def build_matrix(gmaps, locations: list[str], region: str = "", anchor: tuple = None) -> list[list[int]]:
    resolved = _geocode_all(gmaps, locations, region, anchor)
    time_matrix, _ = build_matrix_from_coords(gmaps, resolved)
    return time_matrix


# ── Route scoring ────────────────────────────────────────────────────────────

def route_feasible_and_cost(order, time_matrix, jobs, start_time, service_time_min=15, dist_matrix=None):
    """Cost = total distance driven (km) + wait-time penalty (at 0.5 km/min = ~30 km/h).
    ETAs are computed from time_matrix; distance from dist_matrix (falls back to time_matrix)."""
    current = 0
    current_time = start_time
    total_dist = 0.0
    total_wait = 0.0
    penalty = 0
    feasible = True
    etas = []

    dm = dist_matrix if dist_matrix is not None else time_matrix

    for idx in order:
        travel_time = time_matrix[current][idx + 1]
        travel_dist = dm[current][idx + 1]
        arrival = current_time + timedelta(minutes=travel_time)
        job = jobs[idx]
        if arrival > job["slot_end"]:
            feasible = False
            penalty += int((arrival - job["slot_end"]).total_seconds() / 60) * LATE_PENALTY_PER_MIN
        wait = max(0.0, (job["slot_start"] - arrival).total_seconds() / 60)
        total_wait += wait
        visit_time = max(arrival, job["slot_start"])
        etas.append((visit_time, travel_time, travel_dist))
        total_dist += travel_dist
        current_time = visit_time + timedelta(minutes=service_time_min)
        current = idx + 1

    total_dist += dm[current][0]
    # Wait penalty: 0.5 km per idle minute (equivalent to 30 km/h average speed)
    return feasible, total_dist + total_wait * 0.5 + penalty, etas


# ── Route construction ───────────────────────────────────────────────────────

def nearest_neighbour_init(time_matrix, jobs, start_time, forced_first=None, service_time_min=15, dist_matrix=None):
    """Greedy init: feasibility checked by time, nearest-next chosen by distance."""
    dm = dist_matrix if dist_matrix is not None else time_matrix
    n = len(jobs)
    unvisited = set(range(n))
    order = []
    current = 0
    current_time = start_time

    if forced_first is not None and forced_first in unvisited:
        idx = forced_first
        travel_time = time_matrix[current][idx + 1]
        arrival = current_time + timedelta(minutes=travel_time)
        order.append(idx)
        unvisited.remove(idx)
        current = idx + 1
        current_time = max(arrival, jobs[idx]["slot_start"]) + timedelta(minutes=service_time_min)

    while unvisited:
        candidates = [
            (dm[current][idx + 1], idx, current_time + timedelta(minutes=time_matrix[current][idx + 1]))
            for idx in unvisited
            if current_time + timedelta(minutes=time_matrix[current][idx + 1]) <= jobs[idx]["slot_end"]
        ]
        if candidates:
            _, idx, arrival = min(candidates, key=lambda t: t[0])
            travel_time = time_matrix[current][idx + 1]
        else:
            idx = min(unvisited, key=lambda i: jobs[i]["slot_end"])
            travel_time = time_matrix[current][idx + 1]
            arrival = current_time + timedelta(minutes=travel_time)

        order.append(idx)
        unvisited.remove(idx)
        current = idx + 1
        current_time = max(arrival, jobs[idx]["slot_start"]) + timedelta(minutes=service_time_min)

    return order


def local_search_improve(order, time_matrix, jobs, start_time, max_iters=300, service_time_min=15, dist_matrix=None):
    _, best_score, _ = route_feasible_and_cost(order, time_matrix, jobs, start_time, service_time_min, dist_matrix)
    improved = True
    iters = 0
    n = len(order)

    while improved and iters < max_iters:
        improved = False
        iters += 1

        for i in range(n - 1):
            for j in range(i + 1, n):
                cand = order[:i] + order[i:j + 1][::-1] + order[j + 1:]
                _, score, _ = route_feasible_and_cost(cand, time_matrix, jobs, start_time, service_time_min, dist_matrix)
                if score < best_score:
                    order, best_score = cand, score
                    improved = True

        for i in range(n):
            stop = order[i]
            rest = order[:i] + order[i + 1:]
            for pos in range(len(rest) + 1):
                cand = rest[:pos] + [stop] + rest[pos:]
                if cand == order:
                    continue
                _, score, _ = route_feasible_and_cost(cand, time_matrix, jobs, start_time, service_time_min, dist_matrix)
                if score < best_score:
                    order, best_score = cand, score
                    improved = True

    return order, best_score


def multi_start_optimise(time_matrix, jobs, start_time, service_time_min=15, dist_matrix=None):
    best_order, best_score = None, float("inf")
    for first in range(len(jobs)):
        init = nearest_neighbour_init(time_matrix, jobs, start_time, forced_first=first,
                                      service_time_min=service_time_min, dist_matrix=dist_matrix)
        order, score = local_search_improve(init, time_matrix, jobs, start_time,
                                            service_time_min=service_time_min, dist_matrix=dist_matrix)
        if score < best_score:
            best_order, best_score = order, score
    return best_order, best_score


def optimise_route(gmaps, jobs: list[dict], start_address: str, service_time_min: int = 15) -> list[dict]:
    region, anchor = _geocode_region(gmaps, start_address)

    # Geocode everything once so we can use coordinates for sweep sorting
    locations = [start_address] + [j["address"] for j in jobs]
    all_coords = _geocode_all(gmaps, locations, region=region, anchor=anchor)
    depot_coord = all_coords[0]
    job_coords = all_coords[1:]

    # Within each time window, sort stops by compass direction from depot
    # to avoid geographic backtracking when multiple stops share the same window
    jobs, job_coords = sweep_sort_jobs(jobs, depot_coord, job_coords)

    time_matrix, dist_matrix = build_matrix_from_coords(gmaps, [depot_coord] + job_coords)

    start_time = min(j["slot_start"] for j in jobs)
    best_order, _ = multi_start_optimise(time_matrix, jobs, start_time, service_time_min, dist_matrix)

    first_idx = best_order[0]
    travel_to_first = time_matrix[0][first_idx + 1]
    departure_time = jobs[first_idx]["slot_start"] - timedelta(minutes=travel_to_first)

    feasible, _, etas = route_feasible_and_cost(best_order, time_matrix, jobs, departure_time,
                                                 service_time_min, dist_matrix)

    ordered = []
    total_dist_km = 0.0
    for pos, idx in enumerate(best_order):
        job = jobs[idx]
        visit_time, travel_min, travel_km = etas[pos]
        job["travel_min"] = travel_min
        job["travel_km"] = round(travel_km, 1)
        job["eta"] = visit_time
        total_dist_km += travel_km
        ordered.append(job)

    last_idx = best_order[-1]
    return_travel_min = time_matrix[last_idx + 1][0]
    return_travel_km  = dist_matrix[last_idx + 1][0]
    return_eta = etas[-1][0] + timedelta(minutes=service_time_min + return_travel_min)
    total_dist_km += return_travel_km
    ordered.append({
        "type": "office",
        "address": start_address,
        "qty": 0,
        "travel_min": return_travel_min,
        "travel_km": round(return_travel_km, 1),
        "eta": return_eta,
        "slot_start": return_eta,
        "slot_end": return_eta,
    })

    print(f"  → {total_dist_km:.1f} km total distance (feasible: {feasible})")
    return ordered


# ── Helpers ──────────────────────────────────────────────────────────────────

def format_time(dt: datetime) -> str:
    h, m = dt.hour, dt.minute
    period = "am" if h < 12 else "pm"
    h12 = h if h <= 12 else h - 12
    if h12 == 0:
        h12 = 12
    return f"{h12}{'%02d' % m if m else ''}{period}" if m else f"{h12}{period}"


def generate_route_doc(target_date: date, ordered_jobs: list[dict], output_dir: Path) -> Path:
    day_str = target_date.strftime("%A, ") + str(int(target_date.strftime("%d"))) + target_date.strftime(" %B %Y")
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{target_date.strftime('%d-%m-%y')} Route Plan.pdf"

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=20*mm, rightMargin=20*mm,
        topMargin=20*mm, bottomMargin=20*mm,
    )

    BLUE  = colors.HexColor("#1a73e8")
    DARK  = colors.HexColor("#1C1E26")
    GREY  = colors.HexColor("#5A6070")
    GREEN = colors.HexColor("#15803D")

    title_style = ParagraphStyle("title", fontSize=15, textColor=BLUE,
                                 alignment=TA_CENTER, fontName="Helvetica-Bold",
                                 spaceAfter=4)
    sub_style   = ParagraphStyle("sub", fontSize=9, textColor=GREY,
                                 alignment=TA_CENTER, spaceAfter=12)
    addr_style  = ParagraphStyle("addr", fontSize=11, textColor=DARK,
                                 fontName="Helvetica-Bold", spaceBefore=8, spaceAfter=1)
    meta_style  = ParagraphStyle("meta", fontSize=10, textColor=GREY, spaceAfter=2)
    del_style   = ParagraphStyle("del", fontSize=10, textColor=GREEN, spaceAfter=6)
    office_style= ParagraphStyle("office", fontSize=11, textColor=DARK,
                                 fontName="Helvetica-Bold", spaceBefore=10, spaceAfter=2)
    footer_style= ParagraphStyle("footer", fontSize=8, textColor=GREY,
                                 alignment=TA_CENTER, spaceBefore=16)

    story = []
    story.append(Paragraph(f"Route Plan", title_style))
    story.append(Paragraph(day_str, sub_style))
    story.append(HRFlowable(width="100%", thickness=1, color=BLUE, spaceAfter=10))

    for i, job in enumerate(ordered_jobs):
        travel = job.get("travel_min", "?")
        eta    = format_time(job["eta"]) if "eta" in job else "?"

        km = job.get("travel_km", "")
        km_str = f" / {km} km" if km != "" else ""
        if job["type"] == "office":
            story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#E2E5EA"), spaceBefore=8))
            story.append(Paragraph(f"{job['address']}", office_style))
            story.append(Paragraph(f"Arrive {eta}  ·  {travel} min{km_str}", meta_style))
        else:
            slot_label = f"Booked {format_time(job['slot_start'])}–{format_time(job['slot_end'])}"
            stop_num = i + 1
            story.append(Paragraph(f"Stop {stop_num}  ·  {job['address']}", addr_style))
            story.append(Paragraph(f"Arrive {eta}  ·  {travel} min{km_str}  ·  {slot_label}", meta_style))
            if job["type"] == "delivery":
                qty = job["qty"]
                story.append(Paragraph(f"Del {qty} bag{'s' if qty > 1 else ''}", del_style))

    story.append(Spacer(1, 8*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#E2E5EA")))
    story.append(Paragraph(f"Generated {day_str}", footer_style))

    doc.build(story)
    return out_path
