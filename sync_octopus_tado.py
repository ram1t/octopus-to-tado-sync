import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import requests
from requests.auth import HTTPBasicAuth

from PyTado.interface import Tado


# ============================================================
# CONFIGURATION
# ============================================================

OCTOPUS_API_KEY = os.environ.get("OCTOPUS_API_KEY")
OCTOPUS_ACCOUNT_NUMBER = os.environ.get("OCTOPUS_ACCOUNT_NUMBER")
OCTOPUS_MPRN = os.environ.get("OCTOPUS_MPRN")
OCTOPUS_GAS_SERIAL = os.environ.get("OCTOPUS_GAS_SERIAL")

# Current Tado OAuth client ID documented by Tado.
TADO_CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"

TADO_TOKEN_URL = "https://login.tado.com/oauth2/token"

# Local file used only while the script is running.
TADO_TOKEN_FILE = "/tmp/tado_refresh_token"

# File containing a newly rotated Tado refresh token.
NEW_TADO_REFRESH_TOKEN_FILE = "/tmp/new_tado_refresh_token"

OCTOPUS_GRAPHQL_URL = "https://api.octopus.energy/v1/graphql/"


# ============================================================
# GENERAL HELPERS
# ============================================================

def die(message):
    print(f"ERROR: {message}")
    sys.exit(1)


def check_configuration():
    required = {
        "OCTOPUS_API_KEY": OCTOPUS_API_KEY,
        "OCTOPUS_ACCOUNT_NUMBER": OCTOPUS_ACCOUNT_NUMBER,
        "OCTOPUS_MPRN": OCTOPUS_MPRN,
        "OCTOPUS_GAS_SERIAL": OCTOPUS_GAS_SERIAL,
    }

    missing = [name for name, value in required.items() if not value]

    if missing:
        die(
            "Missing required configuration: "
            + ", ".join(missing)
        )

    print("Configuration OK.")
    print(f"Octopus account: {OCTOPUS_ACCOUNT_NUMBER}")
    print(f"Octopus MPRN: {OCTOPUS_MPRN}")
    print(f"Octopus gas meter: {OCTOPUS_GAS_SERIAL}")


def parse_api_date(value):
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    if isinstance(value, str):
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).date()

    raise TypeError(f"Unsupported date value: {value!r}")


def format_api_date(value):
    return parse_api_date(value).isoformat()


def call_tado_method(tado, *method_names, **kwargs):
    for method_name in method_names:
        method = getattr(tado, method_name, None)

        if callable(method):
            return method(**kwargs)

    raise AttributeError(
        "None of the Tado methods exist on the client: "
        + ", ".join(method_names)
    )


# ============================================================
# TADO AUTHENTICATION
# ============================================================

def load_refresh_token():
    """
    Get the current refresh token.

    In GitHub Actions this comes from TADO_REFRESH_TOKEN.

    The local file is also supported so the script is easy to
    test locally.
    """

    environment_token = os.environ.get("TADO_REFRESH_TOKEN")

    if environment_token:
        return environment_token.strip()

    if Path(TADO_TOKEN_FILE).exists():
        token = Path(TADO_TOKEN_FILE).read_text().strip()

        if token:
            return token

    return None


def save_refresh_token(refresh_token):
    """
    Save the newly rotated Tado refresh token locally.

    The GitHub workflow will subsequently copy this value into
    the TADO_REFRESH_TOKEN repository secret.
    """

    if not refresh_token:
        return

    Path(TADO_TOKEN_FILE).write_text(refresh_token)
    Path(NEW_TADO_REFRESH_TOKEN_FILE).write_text(refresh_token)

    print("New Tado refresh token saved for secret rotation.")


def refresh_tado_access_token(refresh_token):
    """
    Exchange a Tado refresh token for a new access token.

    Tado uses refresh-token rotation, so the new refresh token
    returned by this request must be persisted.
    """

    print("Refreshing Tado access token...")

    response = requests.post(
        TADO_TOKEN_URL,
        params={
            "client_id": TADO_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )

    try:
        payload = response.json()
    except Exception:
        payload = {}

    if response.status_code != 200:
        safe_payload = {
            key: value
            for key, value in payload.items()
            if key not in {
                "access_token",
                "refresh_token",
            }
        }

        raise RuntimeError(
            "Tado refresh-token request failed. "
            f"HTTP {response.status_code}: {safe_payload}"
        )

    access_token = payload.get("access_token")
    new_refresh_token = payload.get("refresh_token")

    if not access_token:
        raise RuntimeError(
            "Tado returned no access token."
        )

    if new_refresh_token:
        save_refresh_token(new_refresh_token)
        refresh_token = new_refresh_token
    else:
        print(
            "WARNING: Tado did not return a new refresh token."
        )

    print("Tado access token obtained successfully.")

    return access_token, refresh_token


def create_tado_client(access_token, refresh_token):
    """
    Create PyTado client using the current refresh token.

    PyTado handles the normal API calls while we handle the
    current Tado OAuth authentication ourselves.
    """

    save_refresh_token(refresh_token)

    try:
        tado = Tado(
            token_file_path=TADO_TOKEN_FILE,
            saved_refresh_token=refresh_token,
        )
    except TypeError:
        # Compatibility with older PyTado versions.
        tado = Tado(
            token_file_path=TADO_TOKEN_FILE
        )

    return tado


def tado_login():
    """
    Authenticate using the stored Tado refresh token.

    There is deliberately NO Playwright/browser login here.
    """

    print("Authenticating with Tado...")

    refresh_token = load_refresh_token()

    if not refresh_token:
        die(
            "No TADO_REFRESH_TOKEN is configured.\n"
            "Run get_tado_refresh_token.py locally first and "
            "add the resulting token as the TADO_REFRESH_TOKEN "
            "GitHub repository secret."
        )

    access_token, refresh_token = refresh_tado_access_token(
        refresh_token
    )

    tado = create_tado_client(
        access_token,
        refresh_token,
    )

    print("Tado authentication completed.")

    return tado


# ============================================================
# OCTOPUS REST HELPERS
# ============================================================

def get_octopus_account_details():
    """
    Get Octopus account information.

    Used to verify the account/meter and for tariff syncing.
    """

    url = (
        f"https://api.octopus.energy/v1/accounts/"
        f"{OCTOPUS_ACCOUNT_NUMBER}/"
    )

    response = requests.get(
        url,
        auth=HTTPBasicAuth(OCTOPUS_API_KEY, ""),
        timeout=30,
    )

    if response.status_code != 200:
        raise RuntimeError(
            "Failed to retrieve Octopus account details. "
            f"HTTP {response.status_code}: {response.text}"
        )

    return response.json()


def fetch_paginated_results(url):
    results = []

    while url:
        response = requests.get(
            url,
            auth=HTTPBasicAuth(OCTOPUS_API_KEY, ""),
            timeout=30,
        )

        if response.status_code != 200:
            raise RuntimeError(
                "Failed to retrieve Octopus data. "
                f"HTTP {response.status_code}: {response.text}"
            )

        payload = response.json()

        results.extend(payload.get("results", []))

        url = payload.get("next")

    return results


# ============================================================
# OCTOPUS GRAPHQL AUTHENTICATION
# ============================================================

def get_octopus_graphql_token():
    """
    Convert the Octopus API key into a Kraken GraphQL token.

    Octopus documents obtainKrakenToken as the mechanism for
    authenticating GraphQL requests, including API-key auth.
    """

    mutation = """
    mutation ObtainKrakenToken(
        $input: ObtainJSONWebTokenInput!
    ) {
        obtainKrakenToken(input: $input) {
            token
            refreshToken
            refreshExpiresIn
        }
    }
    """

    variables = {
        "input": {
            "APIKey": OCTOPUS_API_KEY
        }
    }

    response = requests.post(
        OCTOPUS_GRAPHQL_URL,
        json={
            "query": mutation,
            "variables": variables,
        },
        timeout=30,
    )

    if response.status_code != 200:
        raise RuntimeError(
            "Octopus GraphQL authentication failed. "
            f"HTTP {response.status_code}: {response.text}"
        )

    payload = response.json()

    if payload.get("errors"):
        raise RuntimeError(
            "Octopus GraphQL authentication failed: "
            + json.dumps(payload["errors"])
        )

    token_data = (
        payload.get("data", {})
        .get("obtainKrakenToken")
    )

    if not token_data or not token_data.get("token"):
        raise RuntimeError(
            "Octopus GraphQL authentication returned no token."
        )

    print("Octopus GraphQL authentication successful.")

    return token_data["token"]


# ============================================================
# OCTOPUS METER IDENTIFICATION
# ============================================================

def get_octopus_meter_id(graphql_token):
    """
    Find the Octopus gas meter ID corresponding to the supplied
    MPRN and gas meter serial number.
    """

    query = """
    query MeterPoints($mprn: ID) {
        meterPoints(mprn: $mprn) {
            meters {
                id
                serialNumber
            }
        }
    }
    """

    variables = {
        "mprn": OCTOPUS_MPRN
    }

    response = requests.post(
        OCTOPUS_GRAPHQL_URL,
        headers={
            "Authorization": graphql_token,
            "Content-Type": "application/json",
        },
        json={
            "query": query,
            "variables": variables,
        },
        timeout=30,
    )

    if response.status_code != 200:
        raise RuntimeError(
            "Octopus meter lookup failed. "
            f"HTTP {response.status_code}: {response.text}"
        )

    payload = response.json()

    if payload.get("errors"):
        raise RuntimeError(
            "Octopus meter lookup failed: "
            + json.dumps(payload["errors"])
        )

    meter_points = (
        payload.get("data", {})
        .get("meterPoints")
    )

    if not meter_points:
        raise RuntimeError(
            f"No Octopus meter point found for MPRN {OCTOPUS_MPRN}."
        )

    meters = meter_points.get("meters", [])

    for meter in meters:
        serial = (
            meter.get("serialNumber")
            or meter.get("serial_number")
        )

        if serial == OCTOPUS_GAS_SERIAL:
            meter_id = meter.get("id")

            if meter_id is not None:
                print(
                    "Found Octopus gas meter: "
                    f"{OCTOPUS_GAS_SERIAL} "
                    f"(ID {meter_id})"
                )

                return meter_id

    available = [
        meter.get("serialNumber")
        for meter in meters
    ]

    raise RuntimeError(
        "Could not find the configured gas meter serial "
        f"{OCTOPUS_GAS_SERIAL} on MPRN {OCTOPUS_MPRN}. "
        f"Octopus returned meters: {available}"
    )


# ============================================================
# OCTOPUS ACTUAL METER READINGS
# ============================================================

def get_actual_octopus_meter_reading(graphql_token, meter_id):
    """
    Retrieve the latest ACTUAL gas meter reading from Octopus.

    This is deliberately NOT the consumption endpoint.

    We want the cumulative meter index recorded by Octopus,
    rather than reconstructing a cumulative reading by adding
    interval consumption.
    """

    query = """
    query GasMeterReadings(
        $accountNumber: String!
        $meterId: String!
        $first: Int
    ) {
        gasMeterReadings(
            accountNumber: $accountNumber
            meterId: $meterId
            first: $first
        ) {
            edges {
                node {
                    __typename
                    value
                    readAt
                    typeOfRead
                    reasonForRead
                }
            }
        }
    }
    """

    variables = {
        "accountNumber": OCTOPUS_ACCOUNT_NUMBER,
        "meterId": str(meter_id),
        "first": 20,
    }

    response = requests.post(
        OCTOPUS_GRAPHQL_URL,
        headers={
            "Authorization": graphql_token,
            "Content-Type": "application/json",
        },
        json={
            "query": query,
            "variables": variables,
        },
        timeout=30,
    )

    if response.status_code != 200:
        raise RuntimeError(
            "Octopus gas meter-reading query failed. "
            f"HTTP {response.status_code}: {response.text}"
        )

    payload = response.json()

    if payload.get("errors"):
        raise RuntimeError(
            "Octopus gas meter-reading query failed: "
            + json.dumps(payload["errors"])
        )

    edges = (
        payload.get("data", {})
        .get("gasMeterReadings", {})
        .get("edges", [])
    )

    if not edges:
        raise RuntimeError(
            "Octopus returned no gas meter readings."
        )

    for edge in edges:
        node = edge.get("node") or {}

        value = node.get("value")
        read_at = node.get("readAt")

        if value is None or read_at is None:
            continue

        print(
            "Latest Octopus actual meter reading:"
        )
        print(f"  Reading: {value} m³")
        print(f"  Read at: {read_at}")
        print(
            f"  Type: {node.get('typeOfRead')}"
        )
        print(
            f"  Reason: {node.get('reasonForRead')}"
        )

        return float(value), read_at

    raise RuntimeError(
        "Octopus returned meter-reading records but none "
        "contained a usable meter index."
    )


# ============================================================
# FALLBACK / DIAGNOSTIC METER READING QUERY
# ============================================================

def get_latest_octopus_rest_consumption():
    """
    Diagnostic only.

    This is NOT used as the Tado meter reading.

    It lets us confirm that the REST consumption API is still
    returning data if the GraphQL meter-reading endpoint ever
    changes.
    """

    url = (
        f"https://api.octopus.energy/v1/gas-meter-points/"
        f"{OCTOPUS_MPRN}/meters/"
        f"{OCTOPUS_GAS_SERIAL}/consumption/"
        f"?page_size=1&order_by=period"
    )

    response = requests.get(
        url,
        auth=HTTPBasicAuth(OCTOPUS_API_KEY, ""),
        timeout=30,
    )

    if response.status_code != 200:
        print(
            "REST consumption diagnostic unavailable: "
            f"HTTP {response.status_code}"
        )
        return

    payload = response.json()

    if payload.get("results"):
        result = payload["results"][0]

        print(
            "Octopus REST consumption diagnostic:"
        )
        print(
            f"  Period: {result.get('interval_start')} "
            f"to {result.get('interval_end')}"
        )
        print(
            f"  Consumption: {result.get('consumption')}"
        )


# ============================================================
# TADO ENERGY IQ
# ============================================================

def get_tado_last_meter_reading(tado):
    """
    Retrieve the last meter reading stored in Tado Energy IQ.

    Used only as a safety check so we never intentionally submit
    a lower reading than the one already stored.
    """

    try:
        eiq_data = call_tado_method(
            tado,
            "get_eiq_meter_readings",
            "getEIQMeterReadings",
        )

        if (
            isinstance(eiq_data, dict)
            and "readings" in eiq_data
        ):
            readings = eiq_data["readings"]

            if readings:
                latest = readings[0]

                reading = latest.get("reading")
                reading_date = latest.get("date")

                if reading is not None:
                    print(
                        "Current Tado Energy IQ reading: "
                        f"{reading}"
                    )

                    return float(reading), reading_date

    except Exception as exc:
        print(
            "Could not retrieve existing Tado meter reading: "
            f"{exc}"
        )

    return None, None


def send_reading_to_tado(tado, reading):
    """
    Submit the actual Octopus cumulative meter reading to
    Tado Energy IQ.
    """

    integer_reading = int(round(reading))

    print(
        "Preparing to send meter reading to Tado:"
    )
    print(
        f"  Octopus reading: {reading:.3f} m³"
    )
    print(
        f"  Tado submission: {integer_reading} m³"
    )

    existing_reading, existing_date = (
        get_tado_last_meter_reading(tado)
    )

    if existing_reading is not None:
        print(
            f"Tado's existing reading: "
            f"{existing_reading:.3f} m³"
        )

        if integer_reading < existing_reading:
            raise RuntimeError(
                "SAFETY STOP: Octopus actual meter reading "
                f"({integer_reading}) is lower than the "
                f"existing Tado reading ({existing_reading}). "
                "Nothing was sent to Tado."
            )

        if integer_reading == int(round(existing_reading)):
            print(
                "Tado already contains this meter reading. "
                "Nothing to update."
            )
            return

    result = call_tado_method(
        tado,
        "set_eiq_meter_readings",
        "setEIQMeterReadings",
        reading=integer_reading,
    )

    print(
        "Tado Energy IQ meter reading successfully updated."
    )
    print(f"Tado response: {result}")


# ============================================================
# OPTIONAL OCTOPUS TARIFF SYNC
# ============================================================

def derive_product_code_from_tariff_code(tariff_code):
    parts = tariff_code.split("-")

    if len(parts) <= 2:
        return tariff_code

    product_parts = parts[2:]

    if (
        product_parts
        and len(product_parts[-1]) == 1
        and product_parts[-1].isalpha()
    ):
        product_parts = product_parts[:-1]

    return "-".join(product_parts)


def get_octopus_gas_agreements(account_details):
    agreements = []

    for property_info in account_details.get(
        "properties", []
    ):
        for gas_meter_point in property_info.get(
            "gas_meter_points", []
        ):
            if (
                gas_meter_point.get("mprn")
                != OCTOPUS_MPRN
            ):
                continue

            meters = gas_meter_point.get(
                "meters", []
            )

            serial_numbers = {
                meter.get("serial_number")
                or meter.get("serialNumber")
                for meter in meters
            }

            if (
                serial_numbers
                and OCTOPUS_GAS_SERIAL not in serial_numbers
            ):
                continue

            agreements.extend(
                gas_meter_point.get(
                    "agreements", []
                )
            )

    return agreements


def get_octopus_standard_unit_rates(
    product_code,
    tariff_code,
):
    encoded_tariff_code = quote(
        tariff_code,
        safe="",
    )

    url = (
        f"https://api.octopus.energy/v1/products/"
        f"{product_code}/gas-tariffs/"
        f"{encoded_tariff_code}/standard-unit-rates/"
    )

    return fetch_paginated_results(url)


def build_octopus_tariff_periods(
    agreement,
    unit_rates,
):
    agreement_start = (
        parse_api_date(
            agreement.get("valid_from")
        )
        or date.min
    )

    agreement_end = parse_api_date(
        agreement.get("valid_to")
    )

    raw_periods = []

    for rate in unit_rates:
        tariff_pence = rate.get(
            "value_inc_vat"
        )

        rate_start = parse_api_date(
            rate.get("valid_from")
        )

        if (
            tariff_pence is None
            or rate_start is None
        ):
            continue

        start_date = max(
            agreement_start,
            rate_start,
        )

        if (
            agreement_end is not None
            and start_date > agreement_end
        ):
            continue

        raw_periods.append(
            {
                "start_date": start_date,
                "tariff_pence_per_kwh": tariff_pence,
                "unit": "kWh",
            }
        )

    raw_periods.sort(
        key=lambda period: period["start_date"]
    )

    merged = []

    for period in raw_periods:
        if (
            merged
            and merged[-1]["start_date"]
            == period["start_date"]
        ):
            merged[-1] = period
            continue

        if (
            merged
            and merged[-1]["tariff_pence_per_kwh"]
            == period["tariff_pence_per_kwh"]
        ):
            continue

        merged.append(period)

    for index, period in enumerate(merged):
        if index + 1 < len(merged):
            period["end_date"] = (
                merged[index + 1]["start_date"]
                - timedelta(days=1)
            )
        else:
            period["end_date"] = agreement_end

    return merged


def get_tado_last_tariff_checkpoint(tado):
    try:
        tariff_data = call_tado_method(
            tado,
            "get_eiq_tariffs",
            "getEIQTariffs",
        )

        if isinstance(tariff_data, dict):
            tariffs = tariff_data.get(
                "tariffs",
                [],
            )
        elif isinstance(tariff_data, list):
            tariffs = tariff_data
        else:
            tariffs = []

        latest = None

        for tariff in tariffs:
            value = (
                tariff.get("startDate")
                or tariff.get("start_date")
                or tariff.get("date")
                or tariff.get("fromDate")
                or tariff.get("from_date")
            )

            if not value:
                continue

            parsed = parse_api_date(value)

            if latest is None or parsed > latest:
                latest = parsed

        if latest:
            print(
                "Last Tado tariff starts: "
                f"{latest}"
            )

        return latest

    except Exception as exc:
        print(
            "Could not retrieve Tado tariff history: "
            f"{exc}"
        )
        return None


def sync_octopus_tariffs_to_tado(tado):
    print("Checking Octopus gas tariff...")

    account_details = get_octopus_account_details()

    agreements = get_octopus_gas_agreements(
        account_details
    )

    if not agreements:
        print(
            "No matching Octopus gas agreement found. "
            "Skipping tariff sync."
        )
        return

    tariff_periods = []

    for agreement in agreements:
        tariff_code = (
            agreement.get("tariff_code")
            or agreement.get("tariffCode")
        )

        if not tariff_code:
            continue

        product_code = (
            agreement.get("product_code")
            or derive_product_code_from_tariff_code(
                tariff_code
            )
        )

        try:
            unit_rates = (
                get_octopus_standard_unit_rates(
                    product_code,
                    tariff_code,
                )
            )
        except Exception as exc:
            print(
                "Could not retrieve tariff rates for "
                f"{tariff_code}: {exc}"
            )
            continue

        tariff_periods.extend(
            build_octopus_tariff_periods(
                agreement,
                unit_rates,
            )
        )

    tariff_periods.sort(
        key=lambda period: period["start_date"]
    )

    if not tariff_periods:
        print(
            "No Octopus tariff periods found."
        )
        return

    last_tado = get_tado_last_tariff_checkpoint(
        tado
    )

    for period in tariff_periods:
        if (
            last_tado is not None
            and period["start_date"] <= last_tado
        ):
            continue

        payload = {
            "from_date": format_api_date(
                period["start_date"]
            ),
            "tariff": (
                period["tariff_pence_per_kwh"]
                / 100
            ),
            "unit": period["unit"],
        }

        if period.get("end_date") is not None:
            payload["to_date"] = format_api_date(
                period["end_date"]
            )
            payload["is_period"] = True
        else:
            payload["is_period"] = False

        try:
            result = call_tado_method(
                tado,
                "set_eiq_tariff",
                "setEIQTariff",
                **payload,
            )

            print(
                "Synced Tado tariff:"
                f" {payload}"
            )
            print(
                f"Response: {result}"
            )

        except Exception as exc:
            print(
                "Tariff upload failed:"
                f" {exc}"
            )


# ============================================================
# MAIN
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Sync actual Octopus gas meter readings "
            "to Tado Energy IQ."
        )
    )

    parser.add_argument(
        "--update-tariff",
        action="store_true",
        help=(
            "Also synchronise Octopus gas tariffs "
            "to Tado Energy IQ."
        ),
    )

    return parser.parse_args()


def main():
    args = parse_args()

    check_configuration()

    print()
    print("Authenticating with Tado...")

    tado = tado_login()

    print()
    print("Authenticating with Octopus GraphQL...")

    graphql_token = get_octopus_graphql_token()

    print()
    print("Finding Octopus gas meter...")

    meter_id = get_octopus_meter_id(
        graphql_token
    )

    print()
    print("Reading actual Octopus meter index...")

    actual_reading, read_at = (
        get_actual_octopus_meter_reading(
            graphql_token,
            meter_id,
        )
    )

    print()
    print(
        "IMPORTANT: This value is the actual cumulative "
        "meter reading returned by Octopus."
    )
    print(
        f"Actual meter reading: "
        f"{actual_reading:.3f} m³"
    )
    print(
        f"Octopus reading timestamp: {read_at}"
    )

    print()
    print("Updating Tado Energy IQ...")

    send_reading_to_tado(
        tado,
        actual_reading,
    )

    if args.update_tariff:
        print()
        sync_octopus_tariffs_to_tado(
            tado
        )

    print()
    print("SYNC COMPLETED SUCCESSFULLY.")


if __name__ == "__main__":
    main()
