import argparse
import asyncio
import os
from datetime import date, datetime, timedelta
from urllib.parse import quote

import requests
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from PyTado.interface import Tado
from requests.auth import HTTPBasicAuth


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------

def call_tado_method(tado, *method_names, **kwargs):
    """Call the first available Tado client method from a list of candidates."""
    for method_name in method_names:
        method = getattr(tado, method_name, None)
        if callable(method):
            return method(**kwargs)

    raise AttributeError(
        f"None of the Tado methods exist on the client: "
        f"{', '.join(method_names)}"
    )


def parse_api_date(value):
    """Parse a date or datetime value from Octopus/Tado API responses."""
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    if isinstance(value, str):
        normalized_value = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized_value).date()

    raise TypeError(f"Unsupported date value: {value!r}")


def format_api_date(value):
    """Format a date-like value as YYYY-MM-DD."""
    return parse_api_date(value).isoformat()


# ---------------------------------------------------------------------------
# Octopus API
# ---------------------------------------------------------------------------

def fetch_paginated_results(url, api_key):
    """Fetch all results from a paginated Octopus endpoint."""
    results = []

    while url:
        response = requests.get(
            url,
            auth=HTTPBasicAuth(api_key, ""),
            timeout=30,
        )

        if response.status_code != 200:
            raise RuntimeError(
                "Failed to retrieve data from Octopus. "
                f"Status code: {response.status_code}, "
                f"Message: {response.text}"
            )

        payload = response.json()

        results.extend(payload.get("results", []))
        url = payload.get("next")

    return results


def get_octopus_account_details(api_key, account_number):
    """Retrieve Octopus account details, including active meter agreements."""
    url = f"https://api.octopus.energy/v1/accounts/{account_number}/"

    response = requests.get(
        url,
        auth=HTTPBasicAuth(api_key, ""),
        timeout=30,
    )

    if response.status_code != 200:
        raise RuntimeError(
            "Failed to retrieve Octopus account details. "
            f"Status code: {response.status_code}, "
            f"Message: {response.text}"
        )

    return response.json()


def derive_product_code_from_tariff_code(tariff_code):
    """Infer the Octopus product code from a tariff code."""
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


def get_octopus_gas_agreements(
    account_details,
    mprn,
    gas_serial_number,
):
    """Extract matching gas agreements from Octopus account details."""
    matching_agreements = []

    for property_info in account_details.get("properties", []):

        for gas_meter_point in property_info.get("gas_meter_points", []):

            meter_point_mprn = gas_meter_point.get("mprn")

            if mprn and meter_point_mprn != mprn:
                continue

            meters = gas_meter_point.get("meters", [])

            if gas_serial_number:

                serial_numbers = {
                    meter.get("serial_number")
                    or meter.get("serialNumber")
                    for meter in meters
                }

                serial_numbers.discard(None)

                if (
                    serial_numbers
                    and gas_serial_number not in serial_numbers
                ):
                    continue

            matching_agreements.extend(
                gas_meter_point.get("agreements", [])
            )

    return matching_agreements


def get_octopus_standard_unit_rates(
    api_key,
    product_code,
    tariff_code,
):
    """Retrieve all unit-rate periods for a gas tariff."""

    encoded_tariff_code = quote(
        tariff_code,
        safe="",
    )

    url = (
        f"https://api.octopus.energy/v1/products/"
        f"{product_code}/gas-tariffs/"
        f"{encoded_tariff_code}/standard-unit-rates/"
    )

    return fetch_paginated_results(
        url,
        api_key,
    )


def build_octopus_tariff_periods(
    agreement,
    unit_rates,
):
    """Convert Octopus unit-rate records into Tado-friendly tariff periods."""

    agreement_start = (
        parse_api_date(agreement.get("valid_from"))
        or date.min
    )

    agreement_end = parse_api_date(
        agreement.get("valid_to")
    )

    raw_periods = []

    for rate in unit_rates:

        tariff_pence = rate.get("value_inc_vat")
        rate_start = parse_api_date(
            rate.get("valid_from")
        )

        if tariff_pence is None or rate_start is None:
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

    merged_periods = []

    for period in raw_periods:

        if (
            merged_periods
            and merged_periods[-1]["start_date"]
            == period["start_date"]
        ):
            merged_periods[-1] = period
            continue

        if (
            merged_periods
            and merged_periods[-1]["tariff_pence_per_kwh"]
            == period["tariff_pence_per_kwh"]
        ):
            continue

        merged_periods.append(period)

    for index, period in enumerate(merged_periods):

        end_date = None

        if index + 1 < len(merged_periods):

            end_date = (
                merged_periods[index + 1]["start_date"]
                - timedelta(days=1)
            )

        elif agreement_end is not None:

            end_date = agreement_end

        period["end_date"] = end_date

    return merged_periods


# ---------------------------------------------------------------------------
# Tado tariff handling
# ---------------------------------------------------------------------------

def get_tado_last_tariff_checkpoint(tado):
    """Return the most recent tariff start date stored in Tado."""

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

        latest_start_date = None

        for tariff in tariffs:

            start_value = (
                tariff.get("startDate")
                or tariff.get("start_date")
                or tariff.get("date")
                or tariff.get("fromDate")
                or tariff.get("from_date")
            )

            if not start_value:
                continue

            start_date = parse_api_date(
                start_value
            )

            if (
                latest_start_date is None
                or start_date > latest_start_date
            ):
                latest_start_date = start_date

        if latest_start_date is not None:

            print(
                "Last Tado tariff starts on: "
                f"{latest_start_date.isoformat()}"
            )

        return latest_start_date

    except Exception as e:

        print(
            "Could not retrieve Tado tariff history: "
            f"{e}"
        )

        return None


def discover_octopus_tariff_periods(
    api_key,
    account_number,
    mprn,
    gas_serial_number,
    since_date=None,
):
    """Discover Octopus gas tariff periods that should be sent to Tado."""

    account_details = get_octopus_account_details(
        api_key,
        account_number,
    )

    agreements = get_octopus_gas_agreements(
        account_details,
        mprn,
        gas_serial_number,
    )

    if not agreements:

        raise RuntimeError(
            "No matching gas agreements found in Octopus "
            "account details for the provided MPRN / "
            "gas serial number."
        )

    periods_to_sync = []

    sorted_agreements = sorted(
        agreements,
        key=lambda agreement:
            parse_api_date(
                agreement.get("valid_from")
            )
            or date.min,
    )

    for agreement in sorted_agreements:

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

        unit_rates = get_octopus_standard_unit_rates(
            api_key,
            product_code,
            tariff_code,
        )

        agreement_periods = build_octopus_tariff_periods(
            agreement,
            unit_rates,
        )

        for period in agreement_periods:

            if (
                since_date is not None
                and period["start_date"] <= since_date
            ):
                continue

            periods_to_sync.append(period)

    periods_to_sync.sort(
        key=lambda period: period["start_date"]
    )

    return periods_to_sync


def sync_octopus_tariffs_to_tado(
    tado,
    api_key,
    account_number,
    mprn,
    gas_serial_number,
):
    """Sync missing Octopus gas tariff periods into Tado Energy IQ."""

    last_tado_tariff_start = (
        get_tado_last_tariff_checkpoint(tado)
    )

    tariff_periods = discover_octopus_tariff_periods(
        api_key,
        account_number,
        mprn,
        gas_serial_number,
        since_date=last_tado_tariff_start,
    )

    if not tariff_periods:

        print(
            "No Octopus tariff changes need to be synced "
            "to Tado"
        )

        return []

    synced_periods = []

    for period in tariff_periods:

        payload = {
            "from_date":
                format_api_date(
                    period["start_date"]
                ),

            "tariff":
                period["tariff_pence_per_kwh"] / 100,

            "unit":
                period["unit"],
        }

        if period["end_date"] is not None:

            payload["to_date"] = (
                format_api_date(
                    period["end_date"]
                )
            )

            payload["is_period"] = True

        else:

            payload["is_period"] = False

        result = call_tado_method(
            tado,
            "set_eiq_tariff",
            "setEIQTariff",
            **payload,
        )

        print(
            f"Synced tariff period to Tado: "
            f"{payload} -> {result}"
        )

        synced_periods.append(payload)

    return synced_periods


# ---------------------------------------------------------------------------
# Tado meter readings
# ---------------------------------------------------------------------------

def get_tado_last_meter_reading(tado):
    """
    Retrieve the last meter reading that was sent to Tado.

    Returns:
        Tuple of (reading_value, datetime_of_reading)
    """

    try:

        eiq_data = call_tado_method(
            tado,
            "get_eiq_meter_readings",
            "getEIQMeterReadings",
        )

        if (
            eiq_data
            and isinstance(eiq_data, dict)
            and "readings" in eiq_data
        ):

            readings = eiq_data["readings"]

            if readings:

                latest_reading = readings[0]

                reading_value = latest_reading.get(
                    "reading"
                )

                reading_date = latest_reading.get(
                    "date"
                )

                if (
                    reading_value is not None
                    and reading_date is not None
                ):

                    print(
                        "Last Tado meter reading: "
                        f"{reading_value} "
                        f"(date: {reading_date})"
                    )

                    return (
                        reading_value,
                        reading_date,
                    )

    except Exception as e:

        print(
            "Could not retrieve last Tado meter reading: "
            f"{e}"
        )

    return None, None


# ---------------------------------------------------------------------------
# Octopus consumption
# ---------------------------------------------------------------------------

def get_consumption_since_date(
    api_key,
    mprn,
    gas_serial_number,
    since_datetime,
):
    """
    Retrieve gas consumption from Octopus Energy API
    since a specific date.
    """

    if isinstance(since_datetime, str):

        since_datetime = datetime.fromisoformat(
            since_datetime.replace(
                "Z",
                "+00:00",
            )
        )

    url = (
        f"https://api.octopus.energy/v1/"
        f"gas-meter-points/{mprn}/meters/"
        f"{gas_serial_number}/consumption/"
        f"?group_by=quarter&period_from="
        f"{since_datetime.isoformat()}"
    )

    consumption_delta = 0.0

    while url:

        response = requests.get(
            url,
            auth=HTTPBasicAuth(
                api_key,
                "",
            ),
            timeout=30,
        )

        if response.status_code != 200:

            raise RuntimeError(
                "Failed to retrieve Octopus consumption delta. "
                f"MPRN: {mprn}, "
                f"Gas serial number: {gas_serial_number}, "
                f"Status code: {response.status_code}, "
                f"Message: {response.text}"
            )

        meter_readings = response.json()

        for interval in meter_readings.get(
            "results",
            [],
        ):

            consumption_delta += float(
                interval["consumption"]
            )

        url = meter_readings.get(
            "next",
            "",
        )

    return consumption_delta


def get_meter_reading_total_consumption(
    api_key,
    mprn,
    gas_serial_number,
    tado=None,
):
    """
    Calculate the cumulative gas consumption to send to Tado.

    If a previous Tado reading exists, only consumption since
    that reading is requested from Octopus.

    Otherwise, the available historical Octopus consumption
    is summed.
    """

    if tado is not None:

        last_tado_reading, last_tado_update = (
            get_tado_last_meter_reading(tado)
        )

        if (
            last_tado_reading is not None
            and last_tado_update is not None
        ):

            print(
                "Using delta sync: "
                f"last Tado reading was "
                f"{last_tado_reading}"
            )

            consumption_delta = (
                get_consumption_since_date(
                    api_key,
                    mprn,
                    gas_serial_number,
                    last_tado_update,
                )
            )

            total_consumption = (
                float(last_tado_reading)
                + consumption_delta
            )

            print(
                "Consumption delta since last reading: "
                f"{consumption_delta}"
            )

            print(
                "New total consumption: "
                f"{total_consumption}"
            )

            return total_consumption

    # -----------------------------------------------------------------------
    # First-run fallback
    # -----------------------------------------------------------------------

    print(
        "No previous Tado reading found. "
        "Falling back to available historical Octopus data."
    )

    period_from = (
        datetime.now()
        - timedelta(days=1095)
    )

    url = (
        f"https://api.octopus.energy/v1/"
        f"gas-meter-points/{mprn}/meters/"
        f"{gas_serial_number}/consumption/"
        f"?group_by=quarter&period_from="
        f"{period_from.isoformat()}"
    )

    total_consumption = 0.0

    while url:

        response = requests.get(
            url,
            auth=HTTPBasicAuth(
                api_key,
                "",
            ),
            timeout=30,
        )

        if response.status_code != 200:

            raise RuntimeError(
                "Failed to retrieve Octopus consumption data. "
                f"MPRN: {mprn}, "
                f"Gas serial number: {gas_serial_number}, "
                f"Status code: {response.status_code}, "
                f"Message: {response.text}"
            )

        meter_readings = response.json()

        for interval in meter_readings.get(
            "results",
            [],
        ):

            total_consumption += float(
                interval["consumption"]
            )

        url = meter_readings.get(
            "next",
            "",
        )

    print(
        "Total consumption from available Octopus data: "
        f"{total_consumption}"
    )

    return total_consumption


# ---------------------------------------------------------------------------
# TADO LOGIN
# ---------------------------------------------------------------------------

async def browser_login(
    url,
    username,
    password,
):
    """
    Complete the Tado device-flow browser login.

    IMPORTANT:
    We deliberately do NOT wait for Tado's old
    '.text-center.message-screen.b-bubble-screen__spaced'
    selector. That selector is no longer reliable.

    The actual Tado authentication state is checked by
    tado.device_activation() after the browser flow.
    """

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True,
        )

        context = await browser.new_context()

        page = await context.new_page()

        print(
            "Opening Tado device authentication page..."
        )

        try:

            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=60000,
            )

        except PlaywrightTimeoutError:

            print(
                "Initial Tado page load timed out, "
                "but the page may still be usable."
            )

        print(
            f"Tado authentication URL after navigation: "
            f"{page.url}"
        )

        # ---------------------------------------------------------------
        # Some versions of the Tado device-flow page have an initial
        # Submit/Continue screen. Others go straight to login.
        # ---------------------------------------------------------------

        try:

            submit_button = page.get_by_text(
                "Submit",
                exact=True,
            )

            await submit_button.click(
                timeout=5000,
            )

            print(
                "Clicked initial Tado Submit button."
            )

        except Exception:

            print(
                "No initial Submit button found. "
                "Continuing directly to login."
            )

        # ---------------------------------------------------------------
        # Wait for login form
        # ---------------------------------------------------------------

        print(
            "Waiting for Tado login form..."
        )

        try:

            await page.locator(
                'input[name="loginId"]'
            ).wait_for(
                state="visible",
                timeout=30000,
            )

        except PlaywrightTimeoutError:

            await page.screenshot(
                path="tado-login-form-timeout.png",
                full_page=True,
            )

            print(
                "ERROR: Tado login form was not found."
            )

            print(
                f"Current URL: {page.url}"
            )

            await browser.close()

            raise RuntimeError(
                "Tado login form did not appear. "
                "The saved screenshot "
                "'tado-login-form-timeout.png' "
                "contains the page seen by Playwright."
            )

        # ---------------------------------------------------------------
        # Enter credentials
        # ---------------------------------------------------------------

        print(
            "Entering Tado credentials..."
        )

        await page.locator(
            'input[name="loginId"]'
        ).fill(username)

        await page.locator(
            'input[name="password"]'
        ).fill(password)

        # ---------------------------------------------------------------
        # Submit login
        #
        # Use several possible ways of finding the button rather than
        # depending on one CSS class.
        # ---------------------------------------------------------------

        sign_in_clicked = False

        selectors = [
            'button:has-text("Sign in")',
            'button:has-text("Sign In")',
            'button[type="submit"]',
            'input[type="submit"]',
        ]

        for selector in selectors:

            try:

                button = page.locator(
                    selector
                ).first

                if await button.is_visible(
                    timeout=2000
                ):

                    await button.click()

                    sign_in_clicked = True

                    print(
                        f"Clicked Tado login button "
                        f"using selector: {selector}"
                    )

                    break

            except Exception:
                continue

        if not sign_in_clicked:

            await page.screenshot(
                path="tado-signin-button-error.png",
                full_page=True,
            )

            await browser.close()

            raise RuntimeError(
                "Could not find the Tado Sign in button. "
                "See tado-signin-button-error.png."
            )

        # ---------------------------------------------------------------
        # DO NOT wait for the old Tado CSS selector here.
        #
        # The device authentication is server-side. We just allow the
        # browser enough time to finish the authentication redirect.
        # ---------------------------------------------------------------

        print(
            "Tado credentials submitted. "
            "Waiting for authentication to complete..."
        )

        try:

            await page.wait_for_load_state(
                "domcontentloaded",
                timeout=30000,
            )

        except PlaywrightTimeoutError:

            print(
                "Tado did not finish its page navigation within "
                "30 seconds. Continuing because authentication "
                "may already have completed."
            )

        # Allow redirects / JavaScript to finish.
        await page.wait_for_timeout(
            8000
        )

        print(
            f"Tado page after authentication: "
            f"{page.url}"
        )

        # Diagnostic screenshot.
        await page.screenshot(
            path="tado-after-login.png",
            full_page=True,
        )

        print(
            "Tado browser authentication stage completed."
        )

        await browser.close()


def tado_login(
    username,
    password,
):
    """
    Authenticate with Tado using the current device-flow method.
    """

    print(
        "Initialising Tado authentication..."
    )

    tado = Tado(
        token_file_path="/tmp/tado_refresh_token"
    )

    status = tado.device_activation_status()

    print(
        f"Initial Tado device activation status: {status}"
    )

    # Already authenticated
    if status == "COMPLETED":

        print(
            "Existing Tado authentication token is valid."
        )

        return tado

    # New authentication required
    if status == "PENDING":

        url = tado.device_verification_url()

        if not url:

            raise RuntimeError(
                "Tado reported PENDING authentication "
                "but did not provide a verification URL."
            )

        print(
            "Tado device verification URL obtained."
        )

        print(
            f"Authentication URL: {url}"
        )

        asyncio.run(
            browser_login(
                url,
                username,
                password,
            )
        )

        print(
            "Browser authentication completed."
        )

        print(
            "Waiting for Tado device activation..."
        )

        # This is the important part of the device flow.
        tado.device_activation()

        status = tado.device_activation_status()

        print(
            f"Tado device activation status after login: "
            f"{status}"
        )

    if status != "COMPLETED":

        raise RuntimeError(
            f"Tado authentication did not complete. "
            f"Final status: {status}"
        )

    print(
        "Tado login successful."
    )

    return tado


# ---------------------------------------------------------------------------
# Send reading
# ---------------------------------------------------------------------------

def send_reading_to_tado(
    username,
    password,
    reading,
):
    """
    Send total consumption reading to Tado.
    """

    tado = tado_login(
        username=username,
        password=password,
    )

    result = call_tado_method(
        tado,
        "set_eiq_meter_readings",
        "setEIQMeterReadings",
        reading=int(reading),
    )

    print(
        f"Tado meter reading response: {result}"
    )


def send_reading_to_tado_client(
    tado,
    reading,
):
    """
    Send total consumption reading using an
    already authenticated Tado client.
    """

    print(
        f"Sending meter reading to Tado: "
        f"{reading}"
    )

    result = call_tado_method(
        tado,
        "set_eiq_meter_readings",
        "setEIQMeterReadings",
        reading=int(reading),
    )

    print(
        f"Tado meter reading response: {result}"
    )


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

def parse_args():
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(
        description=(
            "Sync Octopus gas consumption "
            "with Tado Energy IQ"
        )
    )

    # Tado
    parser.add_argument(
        "--tado-email",
        required=True,
        help="Tado account email",
    )

    parser.add_argument(
        "--tado-password",
        required=True,
        help="Tado account password",
    )

    # Octopus
    parser.add_argument(
        "--mprn",
        required=True,
        help=(
            "MPRN (Meter Point Reference Number) "
            "for the gas meter"
        ),
    )

    parser.add_argument(
        "--gas-serial-number",
        required=True,
        help="Gas meter serial number",
    )

    parser.add_argument(
        "--octopus-api-key",
        required=True,
        help="Octopus API key",
    )

    parser.add_argument(
        "--octopus-account-number",
        default=os.getenv(
            "OCTOPUS_ACCOUNT_NUMBER"
        ),
        help=(
            "Octopus account number. Required when "
            "--update-tariff is enabled; can also be "
            "supplied via OCTOPUS_ACCOUNT_NUMBER."
        ),
    )

    parser.add_argument(
        "--update-tariff",
        action="store_true",
        help=(
            "Also sync Octopus gas tariff periods "
            "to Tado Energy IQ."
        ),
    )

    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():

    args = parse_args()

    print(
        "=================================================="
    )

    print(
        "Octopus -> Tado Energy IQ sync"
    )

    print(
        "=================================================="
    )

    print(
        f"MPRN: {args.mprn}"
    )

    print(
        f"Gas meter serial: {args.gas_serial_number}"
    )

    # ---------------------------------------------------------------
    # Authenticate Tado
    # ---------------------------------------------------------------

    print(
        "\nAuthenticating with Tado..."
    )

    tado = tado_login(
        args.tado_email,
        args.tado_password,
    )

    # ---------------------------------------------------------------
    # Retrieve Octopus consumption
    # ---------------------------------------------------------------

    print(
        "\nRetrieving Octopus gas consumption..."
    )

    consumption = get_meter_reading_total_consumption(
        args.octopus_api_key,
        args.mprn,
        args.gas_serial_number,
        tado=tado,
    )

    print(
        f"\nCalculated cumulative consumption: "
        f"{consumption}"
    )

    # ---------------------------------------------------------------
    # Send to Tado
    # ---------------------------------------------------------------

    print(
        "\nSending reading to Tado..."
    )

    send_reading_to_tado_client(
        tado,
        consumption,
    )

    # ---------------------------------------------------------------
    # Optional tariff sync
    # ---------------------------------------------------------------

    if args.update_tariff:

        print(
            "\nUpdating Tado tariff information..."
        )

        if not args.octopus_account_number:

            print(
                "--update-tariff was enabled but no "
                "Octopus account number was provided."
            )

            print(
                "Set OCTOPUS_ACCOUNT_NUMBER or use "
                "--octopus-account-number."
            )

        else:

            try:

                sync_octopus_tariffs_to_tado(
                    tado,
                    args.octopus_api_key,
                    args.octopus_account_number,
                    args.mprn,
                    args.gas_serial_number,
                )

            except Exception as e:

                print(
                    f"Tariff sync failed: {e}"
                )

    print(
        "\n=================================================="
    )

    print(
        "Sync completed."
    )

    print(
        "=================================================="
    )


if __name__ == "__main__":
    main()
