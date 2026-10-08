from fastapi import FastAPI, Query, Request, HTTPException
from datetime import datetime, timedelta
from fastapi.responses import RedirectResponse
from dotenv import load_dotenv
from typing import Optional, Dict, Any
from pydantic import BaseModel

import os
import json
import psycopg
import base64
from email.mime.text import MIMEText

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google_calendar import create_google_flow, get_gmail_service

load_dotenv()

app = FastAPI()

class Requirement(BaseModel):
    value: Any
    priority: str
    source: str


class SmartMatchRequest(BaseModel):
    purpose: str
    listing_type: str
    requirements: Dict[str, Requirement]

@app.post("/properties/smart-match")
def smart_match(request: SmartMatchRequest):

    # ---------------------------------------------------------
    # 1. Validate requirement priorities and sources
    # ---------------------------------------------------------

    allowed_priorities = {
        "required",
        "preferred",
        "flexible"
    }

    allowed_sources = {
        "explicit",
        "inferred"
    }

    for field_name, requirement in request.requirements.items():
        effective_field_name = "community" if field_name == "location" else field_name

        if requirement.priority not in allowed_priorities:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid priority '{requirement.priority}' "
                    f"for requirement '{field_name}'. "
                    f"Allowed values: required, preferred, flexible."
                )
            )

        if requirement.source not in allowed_sources:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid source '{requirement.source}' "
                    f"for requirement '{field_name}'. "
                    f"Allowed values: explicit, inferred."
                )
            )

        # Inferred requirements must never become hard constraints.
        if (
            requirement.source == "inferred"
            and requirement.priority == "required"
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Inferred requirement '{field_name}' "
                    f"cannot use priority 'required'. "
                    f"Customer confirmation is required before "
                    f"making an inferred requirement hard."
                )
            )

    # ---------------------------------------------------------
    # 2. Database connection
    # ---------------------------------------------------------

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise HTTPException(
            status_code=500,
            detail="DATABASE_URL is not configured."
        )

    conn = psycopg.connect(database_url)
    cur = conn.cursor()

    # ---------------------------------------------------------
    # 3. Retrieve available properties
    #
    # listing_type is always a hard constraint.
    # ---------------------------------------------------------

    cur.execute(
        """
        SELECT
            property_id,
            listing_id,
            listing_type,
            community,
            building,
            property_type,
            bedrooms,
            bathrooms,
            area_sqft,
            price,
            currency,
            rent_period,
            price_per_sqft,
            furnishing,
            condition,
            floor,
            view,
            parking,
            amenities,
            occupancy,
            status,
            agent_id
        FROM public.properties
        WHERE LOWER(listing_type) = LOWER(%s)
        AND LOWER(status) = 'available'
        """,
        (request.listing_type,)
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    # ---------------------------------------------------------
    # 4. Convert database rows into dictionaries
    # ---------------------------------------------------------

    properties = []

    for row in rows:

        properties.append({
            "property_id": row[0],
            "listing_id": row[1],
            "listing_type": row[2],
            "community": row[3],
            "building": row[4],
            "property_type": row[5],
            "bedrooms": row[6],
            "bathrooms": row[7],
            "area_sqft": row[8],
            "price": row[9],
            "currency": row[10],
            "rent_period": row[11],
            "price_per_sqft": row[12],
            "furnishing": row[13],
            "condition": row[14],
            "floor": row[15],
            "view": row[16],
            "parking": row[17],
            "amenities": row[18],
            "occupancy": row[19],
            "status": row[20],
            "agent_id": row[21]
        })

    # ---------------------------------------------------------
    # 5. Helper functions
    # ---------------------------------------------------------

    def normalize(value):

        if value is None:
            return None

        return str(value).strip().lower()


    def number(value):

        try:
            return float(value)

        except (TypeError, ValueError):
            return None

    # ---------------------------------------------------------
    # 6. Purpose-aware weights
    # ---------------------------------------------------------

    purpose = normalize(request.purpose)

    purpose_weights = {

        "invest": {
            "community": 30,
            "property_type": 20,
            "bedrooms": 15,
            "budget": 20,
            "price_per_sqft": 15
        },

        "buy": {
            "community": 30,
            "property_type": 25,
            "bedrooms": 25,
            "budget": 20
        },

        "rent": {
            "community": 30,
            "property_type": 25,
            "bedrooms": 25,
            "budget": 20
        },

        "family": {
            "community": 30,
            "property_type": 25,
            "bedrooms": 30,
            "budget": 15
        }
    }

    weights = purpose_weights.get(
        purpose,
        {
            "community": 30,
            "property_type": 25,
            "bedrooms": 25,
            "budget": 20
        }
    )

    # ---------------------------------------------------------
    # 7. Determine investment price-per-sqft benchmark
    #
    # Lower price-per-sqft is treated as more competitive.
    # This is NOT an ROI or rental-yield calculation.
    # ---------------------------------------------------------

    price_per_sqft_values = []

    if purpose == "invest":

        for property_data in properties:

            value = number(
                property_data.get("price_per_sqft")
            )

            if value is not None and value > 0:
                price_per_sqft_values.append(value)

    if price_per_sqft_values:

        best_price_per_sqft = min(
            price_per_sqft_values
        )

        highest_price_per_sqft = max(
            price_per_sqft_values
        )

    else:

        best_price_per_sqft = None
        highest_price_per_sqft = None
        

           # ---------------------------------------------------------
    # 8. Hard filtering
    #
    # Required requirements are NEVER silently relaxed.
    # Customer-facing "location" maps to database "community".
    # ---------------------------------------------------------

    filtered_properties = []

    for property_data in properties:

        passes_required = True

        for field_name, requirement in request.requirements.items():

            # Customer-facing "location" maps to database "community"
            effective_field_name = (
                "community"
                if field_name == "location"
                else field_name
            )

            # Only required requirements are hard filters.
            if requirement.priority != "required":
                continue

            requested_value = requirement.value

            property_value = property_data.get(
                effective_field_name
            )

            # -------------------------------------------------
            # Required community / location
            # -------------------------------------------------

            if effective_field_name == "community":

                if normalize(property_value) != normalize(
                    requested_value
                ):
                    passes_required = False
                    break

            # -------------------------------------------------
            # Required property type
            # -------------------------------------------------

            elif effective_field_name == "property_type":

                if normalize(property_value) != normalize(
                    requested_value
                ):
                    passes_required = False
                    break

            # -------------------------------------------------
            # Required bedrooms
            # -------------------------------------------------

            elif effective_field_name == "bedrooms":

                property_bedrooms = number(
                    property_value
                )

                requested_bedrooms = number(
                    requested_value
                )

                if (
                    property_bedrooms is None
                    or requested_bedrooms is None
                    or property_bedrooms != requested_bedrooms
                ):
                    passes_required = False
                    break

            # -------------------------------------------------
            # Required budget
            # -------------------------------------------------

            elif effective_field_name == "budget":

                property_price = number(
                    property_data.get("price")
                )

                requested_budget = number(
                    requested_value
                )

                if (
                    property_price is None
                    or requested_budget is None
                    or property_price > requested_budget
                ):
                    passes_required = False
                    break

            # -------------------------------------------------
            # Generic required exact match
            # -------------------------------------------------

            else:

                if normalize(property_value) != normalize(
                    requested_value
                ):
                    passes_required = False
                    break


       
        # -----------------------------------------------------
        # Keep property if ALL required requirements passed
        # -----------------------------------------------------

        if passes_required:
            filtered_properties.append(property_data)
            


    # ---------------------------------------------------------
    # 9. Score candidates
    # ---------------------------------------------------------

    scored_properties = []

    for property_data in filtered_properties:

        total_score = 0.0
        total_weight = 0.0

        match_reasons = []

        for field_name, requirement in request.requirements.items():

            effective_field_name = (
                "community"
                if field_name == "location"
                else field_name
            )

            requested_value = requirement.value
            priority = requirement.priority

            field_score = 0


            # -------------------------------------------------
            # COMMUNITY
            # -------------------------------------------------

            if effective_field_name == "community":

                property_value = property_data.get(
                    "community"
                )

                if normalize(property_value) == normalize(
                    requested_value
                ):

                    field_score = 100

                    match_reasons.append(
                        "Exact community match"
                    )

                else:

                    field_score = 0

                    if priority != "required":

                        match_reasons.append(
                            "Different community alternative"
                        )


            # -------------------------------------------------
            # PROPERTY TYPE
            # -------------------------------------------------

            elif effective_field_name == "property_type":

                property_value = property_data.get(
                    "property_type"
                )

                if normalize(property_value) == normalize(
                    requested_value
                ):

                    field_score = 100

                    match_reasons.append(
                        "Exact property type match"
                    )

                else:

                    field_score = 0

                    if priority != "required":

                        match_reasons.append(
                            "Different property type alternative"
                        )


            # -------------------------------------------------
            # BEDROOMS
            # -------------------------------------------------

            elif effective_field_name == "bedrooms":

                property_bedrooms = number(
                    property_data.get("bedrooms")
                )

                requested_bedrooms = number(
                    requested_value
                )

                if (
                    property_bedrooms is None
                    or requested_bedrooms is None
                ):

                    field_score = 0

                else:

                    difference = (
                        property_bedrooms
                        - requested_bedrooms
                    )

                    absolute_difference = abs(
                        difference
                    )

                    # Exact match
                    if absolute_difference == 0:

                        field_score = 100

                        match_reasons.append(
                            "Exact bedroom match"
                        )

                    # One-bedroom alternative
                    elif absolute_difference == 1:

                        if purpose == "invest":

                            field_score = 70

                        elif purpose == "family":

                            field_score = 35

                        else:

                            field_score = 60

                        if difference > 0:

                            match_reasons.append(
                                "One-bedroom upgrade"
                            )

                        else:

                            match_reasons.append(
                                "One-bedroom alternative"
                            )

                    # Two-bedroom difference
                    elif absolute_difference == 2:

                        if purpose == "invest":

                            field_score = 40

                        elif purpose == "family":

                            field_score = 10

                        else:

                            field_score = 25

                        if difference > 0:

                            match_reasons.append(
                                "Larger-bedroom alternative"
                            )

                        else:

                            match_reasons.append(
                                "Smaller-bedroom alternative"
                            )

                    # Large mismatch
                    else:

                        field_score = 0

                        match_reasons.append(
                            "Bedroom count differs significantly"
                        )


            # -------------------------------------------------
            # BUDGET
            # -------------------------------------------------

            elif effective_field_name == "budget":

                property_price = number(
                    property_data.get("price")
                )

                requested_budget = number(
                    requested_value
                )

                if (
                    property_price is None
                    or requested_budget is None
                    or requested_budget <= 0
                ):

                    field_score = 0

                else:

                    if property_price <= requested_budget:

                        difference_ratio = (
                            requested_budget - property_price
                        ) / requested_budget

                        field_score = min(
                            100,
                            85 + (difference_ratio * 15)
                        )

                        match_reasons.append(
                            "Within target budget"
                        )

                    else:

                        over_ratio = (
                            property_price - requested_budget
                        ) / requested_budget

                        if over_ratio <= 0.05:

                            field_score = 65

                            match_reasons.append(
                                "Slightly above target budget"
                            )

                        elif over_ratio <= 0.10:

                            field_score = 45

                            match_reasons.append(
                                "Above target budget"
                            )

                        else:

                            field_score = 0


            # -------------------------------------------------
            # PRICE PER SQFT
            # -------------------------------------------------

            elif effective_field_name == "price_per_sqft":

                property_price_per_sqft = number(
                    property_data.get(
                        "price_per_sqft"
                    )
                )

                if (
                    property_price_per_sqft is not None
                    and best_price_per_sqft is not None
                ):

                    if highest_price_per_sqft == best_price_per_sqft:

                        field_score = 100

                    else:

                        price_range = (
                            highest_price_per_sqft
                            - best_price_per_sqft
                        )

                        relative_position = (
                            property_price_per_sqft
                            - best_price_per_sqft
                        ) / price_range

                        field_score = max(
                            0,
                            100 - (
                                relative_position * 100
                            )
                        )

                    if field_score >= 80:

                        match_reasons.append(
                            "Competitive price per square foot"
                        )

                    elif field_score >= 60:

                        match_reasons.append(
                            "Reasonable price per square foot"
                        )


            # -------------------------------------------------
            # OTHER FIELDS
            # -------------------------------------------------

            else:

                property_value = property_data.get(
                    effective_field_name
                )

                if normalize(property_value) == normalize(
                    requested_value
                ):

                    field_score = 100

                else:

                    field_score = 0


            # -------------------------------------------------
            # Priority weighting
            # -------------------------------------------------

            base_weight = weights.get(
                effective_field_name,
                10
            )

            if priority == "required":

                priority_multiplier = 1.0

            elif priority == "preferred":

                priority_multiplier = 1.0

            else:

                priority_multiplier = 0.35

            final_weight = (
                base_weight
                * priority_multiplier
            )

            # -------------------------------------------------
            # Add field score to total score
            # -------------------------------------------------

            total_score += (
                field_score
                * final_weight
            )

            total_weight += final_weight


        # -----------------------------------------------------
        # Add investment-specific price-per-sqft score
        #
        # Only when investment purpose is used.
        # -----------------------------------------------------

        if purpose == "invest":

            property_price_per_sqft = number(
                property_data.get(
                    "price_per_sqft"
                )
            )

            investment_weight = weights.get(
                "price_per_sqft",
                15
            )

            if (
                property_price_per_sqft is not None
                and best_price_per_sqft is not None
            ):

                if highest_price_per_sqft == best_price_per_sqft:

                    investment_score = 100

                else:

                    price_range = (
                        highest_price_per_sqft
                        - best_price_per_sqft
                    )

                    if price_range > 0:

                        relative_position = (
                            property_price_per_sqft
                            - best_price_per_sqft
                        ) / price_range

                        investment_score = max(
                            0,
                            100 - (
                                relative_position * 100
                            )
                        )

                    else:

                        investment_score = 100

                total_score += (
                    investment_score
                    * investment_weight
                )

                total_weight += investment_weight

                if investment_score >= 80:

                    match_reasons.append(
                        "Competitive investment price per square foot"
                    )

                elif investment_score >= 60:

                    match_reasons.append(
                        "Reasonable investment price per square foot"
                    )


        # -----------------------------------------------------
        # Normalize score to 0–100
        # -----------------------------------------------------

        if total_weight > 0:

            match_score = round(
                total_score / total_weight
            )

        else:

            match_score = 0


        # -----------------------------------------------------
        # Match level
        # -----------------------------------------------------

        if match_score >= 85:

            match_level = "strong"

        elif match_score >= 70:

            match_level = "good"

        elif match_score >= 55:

            match_level = "moderate"

        else:

            match_level = "weak"


        # -----------------------------------------------------
        # Investment context
        # -----------------------------------------------------

        if purpose == "invest":

            price_per_sqft = number(
                property_data.get(
                    "price_per_sqft"
                )
            )

            if price_per_sqft is not None:

                match_reasons.append(
                    "Price-per-square-foot considered for investment comparison"
                )


        # -----------------------------------------------------
        # Remove duplicate reasons
        # -----------------------------------------------------

        match_reasons = list(
            dict.fromkeys(match_reasons)
        )

        property_data["match_score"] = match_score

        property_data["match_level"] = match_level

        property_data["match_reasons"] = match_reasons

        scored_properties.append(
            property_data
        )


    # ---------------------------------------------------------
    # 10. Sort by highest score
    # ---------------------------------------------------------

    scored_properties.sort(
        key=lambda property_data: (
            property_data["match_score"],
            number(
                property_data.get("price_per_sqft")
            ) or 999999999
        ),
        reverse=True
    )


    # ---------------------------------------------------------
    # 11. Quality threshold
    #
    # Do not return weak/random properties.
    # ---------------------------------------------------------

    strong_matches = [
        property_data
        for property_data in scored_properties
        if property_data["match_score"] >= 55
    ]


    # ---------------------------------------------------------
    # 12. Return top 3
    # ---------------------------------------------------------

    top_matches = strong_matches[:3]


    # ---------------------------------------------------------
    # 13. No strong match
    # ---------------------------------------------------------

    if not top_matches:

        return {
            "success": True,
            "match_count": 0,
            "matches": [],
            "message": (
                "No strong matches found based on "
                "the current requirements."
            )
        }
    # ---------------------------------------------------------
    # 14. Final response
    # ---------------------------------------------------------

    return {
        "success": True,
        "match_count": len(top_matches),
        "matches": top_matches
    }

class SmartMatchBuilderRequest(BaseModel):
    purpose: str
    listing_type: str
    community: str
    property_type: str
    bedrooms: int
    budget: float
    community_priority: str
    property_type_priority: str
    bedrooms_priority: str
    budget_priority: str


@app.post("/properties/smart-match-builder")
def smart_match_builder(request: SmartMatchBuilderRequest):

    smart_request = SmartMatchRequest(
        purpose=request.purpose,
        listing_type=request.listing_type,
        requirements={
            "community": Requirement(
                value=request.community,
                priority=request.community_priority,
                source="explicit",
            ),
            "property_type": Requirement(
                value=request.property_type,
                priority=request.property_type_priority,
                source="explicit",
            ),
            "bedrooms": Requirement(
                value=request.bedrooms,
                priority=request.bedrooms_priority,
                source="explicit",
            ),
            "budget": Requirement(
                value=request.budget,
                priority=request.budget_priority,
                source="explicit",
            ),
        },
    )

    return smart_match(smart_request)
@app.get("/")
def home():
    return {
        "message": "Northstar Property API is running"
    }


@app.get("/db-test")
def db_test():
    database_url = os.getenv("DATABASE_URL")

    conn = psycopg.connect(database_url)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM public.properties")
    total_properties = cur.fetchone()[0]

    cur.close()
    conn.close()

    return {
        "database_connected": True,
        "total_properties": total_properties
    }


@app.get("/properties/search")
def search_properties(
    community: str = Query(...),
    property_type: str = Query(...),
    bedrooms: int = Query(...),
    listing_type: str = Query(...),
    max_price: float = Query(...)
):
    database_url = os.getenv("DATABASE_URL")

    conn = psycopg.connect(database_url)
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            property_id,
            listing_id,
            listing_type,
            community,
            building,
            property_type,
            bedrooms,
            bathrooms,
            area_sqft,
            price,
            currency,
            rent_period,
            price_per_sqft,
            furnishing,
            condition,
            floor,
            view,
            parking,
            amenities,
            occupancy,
            status,
            agent_id
        FROM public.properties
        WHERE community = %s
          AND property_type = %s
          AND bedrooms = %s
          AND listing_type = %s
          AND price <= %s
          AND status = 'Available'
        ORDER BY price ASC;
        """,
        (
            community,
            property_type,
            bedrooms,
            listing_type,
            max_price
        )
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    properties = []

    for row in rows:
        properties.append({
            "property_id": row[0],
            "listing_id": row[1],
            "listing_type": row[2],
            "community": row[3],
            "building": row[4],
            "property_type": row[5],
            "bedrooms": row[6],
            "bathrooms": row[7],
            "area_sqft": row[8],
            "price": row[9],
            "currency": row[10],
            "rent_period": row[11],
            "price_per_sqft": row[12],
            "furnishing": row[13],
            "condition": row[14],
            "floor": row[15],
            "view": row[16],
            "parking": row[17],
            "amenities": row[18],
            "occupancy": row[19],
            "status": row[20],
            "agent_id": row[21]
        })

    return {
        "count": len(properties),
        "results": properties
    }
from fastapi.responses import RedirectResponse
from google_calendar import create_google_flow


google_flow = None


@app.get("/authorize")
def authorize():
    global google_flow

    google_flow = create_google_flow()

    authorization_url, state = google_flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent"
    )

    return RedirectResponse(authorization_url)


@app.get("/oauth2callback")
def oauth2callback(request: Request):
    global google_flow

    if google_flow is None:
        return {
            "error": "OAuth flow not found",
            "details": "Please start again from /authorize"
        }

    try:
        google_flow.fetch_token(
            authorization_response=str(request.url)
        )

        credentials = google_flow.credentials

        with open("token.json", "w") as token:
            token.write(credentials.to_json())

        return {
            "message": "Google Calendar authorization successful"
        }

    except Exception as e:
        return {
            "error": type(e).__name__,
            "details": str(e)
        }

        
@app.get("/calendar-test")
def calendar_test():
    credentials = Credentials.from_authorized_user_file(
        "token.json",
        ["https://www.googleapis.com/auth/calendar"]
    )

    service = build(
        "calendar",
        "v3",
        credentials=credentials
    )

    events_result = service.events().list(
        calendarId="primary",
        maxResults=10,
        singleEvents=True,
        orderBy="startTime"
    ).execute()

    events = events_result.get("items", [])

    results = []

    for event in events:
        results.append({
            "summary": event.get("summary"),
            "start": event.get("start"),
            "end": event.get("end")
        })

    return {
        "calendar_connected": True,
        "events": results
    }

from datetime import datetime, timedelta
from fastapi import HTTPException


@app.get("/availability/check")
def check_availability(
    property_id: str,
    requested_date: str,
    requested_time: str
):
    # 1. Get assigned agent for this property
    database_url = os.getenv("DATABASE_URL")

    conn = psycopg.connect(database_url)
    cur = conn.cursor()

    cur.execute(
        """
        SELECT agent_id
        FROM public.properties
        WHERE property_id = %s
        """,
        (property_id,)
    )

    row = cur.fetchone()

    cur.close()
    conn.close()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="Property not found"
        )

    agent_id = row[0]

       # 2. Load Google Calendar credentials
    token_json = os.getenv("GOOGLE_TOKEN_JSON")

    if not token_json:
        raise HTTPException(
            status_code=500,
            detail="Google Calendar credentials are not configured."
        )

    credentials = Credentials.from_authorized_user_info(
        json.loads(token_json),
        ["https://www.googleapis.com/auth/calendar"]
    )

    service = build(
        "calendar",
        "v3",
        credentials=credentials
    )

    # 3. Build requested viewing time
    start_time = datetime.strptime(
        f"{requested_date} {requested_time}",
        "%Y-%m-%d %H:%M"
    )

    end_time = start_time + timedelta(minutes=30)

    # Pakistan timezone
    start_time = start_time.isoformat() + "+05:00"
    end_time = end_time.isoformat() + "+05:00"

    # 4. Check Google Calendar
    freebusy_result = service.freebusy().query(
        body={
            "timeMin": start_time,
            "timeMax": end_time,
            "items": [
                {
                    "id": "primary"
                }
            ]
        }
    ).execute()

    calendar_busy = freebusy_result["calendars"]["primary"]["busy"]

    # 5. Return availability
    if calendar_busy:
        return {
            "available": False,
            "property_id": property_id,
            "agent_id": agent_id,
            "requested_date": requested_date,
            "requested_time": requested_time,
            "message": "The requested viewing time is busy."
        }

    return {
        "available": True,
        "property_id": property_id,
        "agent_id": agent_id,
        "requested_date": requested_date,
        "requested_time": requested_time,
        "message": "The requested viewing time is available."
    }

@app.post("/availability/book")
def book_viewing(
    property_id: str,
    requested_date: str,
    requested_time: str,
    customer_name: str,
    customer_phone: str
):
    # 1. Get assigned agent for this property
    database_url = os.getenv("DATABASE_URL")

    conn = psycopg.connect(database_url)
    cur = conn.cursor()

    cur.execute(
        """
        SELECT agent_id
        FROM public.properties
        WHERE property_id = %s
        """,
        (property_id,)
    )

    row = cur.fetchone()

    cur.close()
    conn.close()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="Property not found"
        )

    agent_id = row[0]

    # 2. Load Google Calendar credentials
    credentials = Credentials.from_authorized_user_file(
        "token.json",
        ["https://www.googleapis.com/auth/calendar"]
    )

    service = build(
        "calendar",
        "v3",
        credentials=credentials
    )

    # 3. Build viewing time
    start_time = datetime.strptime(
        f"{requested_date} {requested_time}",
        "%Y-%m-%d %H:%M"
    )

    end_time = start_time + timedelta(minutes=30)

    start_time_iso = start_time.isoformat() + "+05:00"
    end_time_iso = end_time.isoformat() + "+05:00"

    # 4. Create Google Calendar event
    event = {
        "summary": f"Property Viewing - {property_id}",
        "description": (
            f"Customer: {customer_name}\n"
            f"Phone: {customer_phone}\n"
            f"Property ID: {property_id}\n"
            f"Assigned Agent: {agent_id}"
        ),
        "start": {
            "dateTime": start_time_iso,
            "timeZone": "Asia/Karachi"
        },
        "end": {
            "dateTime": end_time_iso,
            "timeZone": "Asia/Karachi"
        }
    }

    created_event = service.events().insert(
        calendarId="primary",
        body=event
    ).execute()

    # 5. Notify assigned agent through company email
    notification = notify_agent(
        property_id=property_id,
        agent_id=agent_id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        requested_date=requested_date,
        requested_time=requested_time
    )

    # 6. Return booking + notification result
    return {
        "booked": True,
        "property_id": property_id,
        "agent_id": agent_id,
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "requested_date": requested_date,
        "requested_time": requested_time,
        "event_id": created_event.get("id"),
        "notification_sent": notification["notification_sent"],
        "notification_message_id": notification["message_id"],
        "message": "Property viewing booked successfully and agent notification sent."
    }

@app.get("/gmail-test")
def gmail_test():
    service = get_gmail_service()

    message = MIMEText(
        "This is a test email from the Northstar FastAPI backend."
    )

    message["to"] = "abdullah3742new@gmail.com"
    message["subject"] = "Northstar Gmail API Test"

    raw_message = base64.urlsafe_b64encode(
        message.as_bytes()
    ).decode()

    result = service.users().messages().send(
        userId="me",
        body={"raw": raw_message}
    ).execute()

    return {
        "email_sent": True,
        "message_id": result.get("id")
    }
@app.post("/notify-agent")
def notify_agent(
    property_id: str,
    agent_id: str,
    customer_name: str,
    customer_phone: str,
    requested_date: str,
    requested_time: str
):
    service = get_gmail_service()

    message_body = f"""
New Property Viewing Booking

Property ID: {property_id}
Assigned Agent ID: {agent_id}

Customer Name: {customer_name}
Customer Phone: {customer_phone}

Viewing Date: {requested_date}
Viewing Time: {requested_time}
"""

    message = MIMEText(message_body)

    message["to"] = "abdullah3742new@gmail.com"
    message["subject"] = f"New Property Viewing - {property_id}"

    raw_message = base64.urlsafe_b64encode(
        message.as_bytes()
    ).decode()

    result = service.users().messages().send(
        userId="me",
        body={"raw": raw_message}
    ).execute()

    return {
        "notification_sent": True,
        "message_id": result.get("id"),
        "property_id": property_id,
        "agent_id": agent_id
    }