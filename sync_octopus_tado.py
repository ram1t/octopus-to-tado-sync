import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from requests.auth import HTTPBasicAuth
from playwright.async_api import async_playwright
from PyTado.interface import Tado


# ============================================================
# CONFIGURATION
# ============================================================

OCTOPUS_API_KEY = os.environ.get("OCTOPUS_API_KEY")
OCTOPUS_ACCOUNT_NUMBER = os.environ.get("OCTOPUS_ACCOUNT_NUMBER")

TADO_USERNAME = os.environ.get("TADO_EMAIL")

TADO_PASSWORD = os.environ.get("TADO_PASSWORD")

# Your gas details
MPRN = os.environ.get(
    "OCTOPUS_MPRN",
    "E6S26396281961"
)

GAS_METER_SERIAL_NUMBER = os.environ.get(
    "OCTOPUS_GAS_SERIAL",
    "7448400807"
)


TADO_TOKEN_FILE = "/tmp/tado_refresh_token"


# ============================================================
# GENERAL HELPERS
# ============================================================

def fail(message):
    print("")
    print("ERROR:")
    print(message)
    print("")
    sys.exit(1)


def check_configuration():
    print("Checking configuration...")

    missing = []

    if not OCTOPUS_API_KEY:
        missing.append("OCTOPUS_API_KEY")

    if not OCTOPUS_ACCOUNT_NUMBER:
        missing.append("OCTOPUS_ACCOUNT_NUMBER")

    if not TADO_USERNAME:
        missing.append("TADO_USERNAME")

    if not TADO_PASSWORD:
        missing.append("TADO_PASSWORD")

    if missing:
        fail(
            "The following GitHub Actions secrets/environment "
            "variables are missing:\n"
            + "\n".join(f"  - {x}" for x in missing)
        )

    print("Configuration OK.")


# ============================================================
# TADO BROWSER LOGIN
# ============================================================

async def browser_login(url, username, password):

    print("Opening Tado device authentication page...")

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True
        )

        context = await browser.new_context()

        page = await context.new_page()

        try:

            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=60000
            )

            print(
                f"Tado authentication URL after navigation: "
                f"{page.url}"
            )

            # ------------------------------------------------
            # Some versions of the Tado page initially show a
            # Submit/Continue button before the login form.
            # ------------------------------------------------

            initial_selectors = [
                'button:has-text("Submit")',
                'button:has-text("Continue")',
                'input[type="submit"]',
            ]

            for selector in initial_selectors:

                try:

                    locator = page.locator(
                        selector
                    ).first

                    if await locator.is_visible(
                        timeout=2000
                    ):

                        print(
                            f"Clicking initial Tado button: "
                            f"{selector}"
                        )

                        await locator.click()

                        await page.wait_for_timeout(
                            1500
                        )

                        break

                except Exception:
                    pass

            # ------------------------------------------------
            # Wait for login form
            # ------------------------------------------------

            print(
                "Waiting for Tado login form..."
            )

            login_selector = (
                'input[name="loginId"]'
            )

            await page.wait_for_selector(
                login_selector,
                timeout=30000
            )

            print(
                "Entering Tado credentials..."
            )

            await page.locator(
                login_selector
            ).fill(username)

            # Tado has used several password selectors.
            password_selectors = [
                'input[name="password"]',
                'input[type="password"]',
            ]

            password_filled = False

            for selector in password_selectors:

                try:

                    locator = page.locator(
                        selector
                    ).first

                    if await locator.is_visible(
                        timeout=2000
                    ):

                        await locator.fill(
                            password
                        )

                        password_filled = True
                        break

                except Exception:
                    pass

            if not password_filled:
                raise RuntimeError(
                    "Could not find Tado password field."
                )

            # ------------------------------------------------
            # Sign in
            # ------------------------------------------------

            sign_in_selectors = [
                'button:has-text("Sign in")',
                'button:has-text("Log in")',
                'input[type="submit"]',
                'button[type="submit"]',
            ]

            sign_in_clicked = False

            for selector in sign_in_selectors:

                try:

                    locator = page.locator(
                        selector
                    ).first

                    if await locator.is_visible(
                        timeout=2000
                    ):

                        print(
                            "Clicking Tado login button "
                            f"using selector: {selector}"
                        )

                        await locator.click()

                        sign_in_clicked = True
                        break

                except Exception:
                    pass

            if not sign_in_clicked:
                raise RuntimeError(
                    "Could not find Tado Sign in button."
                )

            print(
                "Tado credentials submitted. "
                "Waiting for authentication to complete..."
            )

            # ------------------------------------------------
            # Allow navigation/network activity to settle.
            # Do NOT wait for the old CSS selector that was
            # causing the original 30-second timeout.
            # ------------------------------------------------

            try:
                await page.wait_for_load_state(
                    "networkidle",
                    timeout=15000
                )
            except Exception:
                pass

            await page.wait_for_timeout(
                3000
            )

            print(
                f"Tado page after authentication: "
                f"{page.url}"
            )

            # ------------------------------------------------
            # Tado can display a separate OAuth consent screen.
            # ------------------------------------------------

            print(
                "Checking for Tado OAuth authorization/consent..."
            )

            authorization_selectors = [
                'button:has-text("Allow")',
                'button:has-text("Authorize")',
                'button:has-text("Accept")',
                'button:has-text("Approve")',
                'button:has-text("Continue")',
                'input[type="submit"][value*="Allow"]',
                'input[type="submit"][value*="Authorize"]',
                'input[type="submit"][value*="Accept"]',
                'input[type="submit"][value*="Approve"]',
            ]

            authorization_clicked = False

            for selector in authorization_selectors:

                try:

                    locator = page.locator(
                        selector
                    ).first

                    if await locator.is_visible(
                        timeout=2000
                    ):

                        print(
                            "Found Tado authorization button: "
                            f"{selector}"
                        )

                        await locator.click()

                        print(
                            "Clicked Tado authorization button: "
                            f"{selector}"
                        )

                        authorization_clicked = True

                        await page.wait_for_timeout(
                            3000
                        )

                        break

                except Exception:
                    pass

            if not authorization_clicked:

                print(
                    "No separate OAuth authorization button "
                    "found. Continuing..."
                )

            try:
                await page.wait_for_load_state(
                    "networkidle",
                    timeout=10000
                )
            except Exception:
                pass

            await page.wait_for_timeout(
                2000
            )

            print(
                "Tado page after OAuth authorization: "
                f"{page.url}"
            )

            # ------------------------------------------------
            # Diagnostic screenshot
            # ------------------------------------------------

            try:

                await page.screenshot(
                    path="/tmp/tado_authentication.png",
                    full_page=True
                )

                print(
                    "Saved Tado authentication screenshot."
                )

            except Exception as e:

                print(
                    "Could not save authentication screenshot: "
                    f"{e}"
                )

            print(
                "Tado browser authentication stage completed."
            )

        finally:

            await browser.close()


# ============================================================
# DIRECT TADO OAUTH TOKEN EXCHANGE
# ============================================================

def complete_tado_device_activation(tado):

    """
    Complete the Tado OAuth device flow ourselves rather than
    using PyTado.device_activation().

    This avoids the current PyTado/Tado HTTP 400 issue and
    exposes the actual OAuth response if Tado rejects the
    token exchange.
    """

    print(
        "Completing Tado OAuth device activation directly..."
    )

    # --------------------------------------------------------
    # PyTado stores the device-flow information internally.
    # --------------------------------------------------------

    http_object = getattr(
        tado,
        "_http",
        None
    )

    if http_object is None:
        raise RuntimeError(
            "Could not access the PyTado HTTP object."
        )

    device_data = getattr(
        http_object,
        "_device_flow_data",
        None
    )

    if not device_data:

        # Some PyTado versions use a slightly different
        # internal attribute. Try the common alternatives.

        possible_names = [
            "device_flow_data",
            "_device_auth_data",
            "device_auth_data",
        ]

        for name in possible_names:

            candidate = getattr(
                http_object,
                name,
                None
            )

            if candidate:
                device_data = candidate
                break

    if not device_data:

        raise RuntimeError(
            "Could not find Tado device-flow data inside "
            "PyTado.\n\n"
            "This means the installed PyTado version uses "
            "a different internal structure."
        )

    print(
        "Tado device-flow data located."
    )

    # --------------------------------------------------------
    # Extract OAuth values.
    # --------------------------------------------------------

    device_code = device_data.get(
        "device_code"
    )

    if not device_code:
        raise RuntimeError(
            "Tado did not provide a device_code."
        )

    interval = int(
        device_data.get(
            "interval",
            5
        )
    )

    expires_in = int(
        device_data.get(
            "expires_in",
            900
        )
    )

    # --------------------------------------------------------
    # Obtain client ID.
    # --------------------------------------------------------

    client_id = getattr(
        http_object,
        "_client_id",
        None
    )

    if not client_id:

        possible_names = [
            "client_id",
            "_clientId",
            "clientId",
        ]

        for name in possible_names:

            candidate = getattr(
                http_object,
                name,
                None
            )

            if candidate:
                client_id = candidate
                break

    if not client_id:

        raise RuntimeError(
            "Could not determine the Tado OAuth client ID "
            "from PyTado."
        )

    print(
        "Tado OAuth client ID obtained."
    )

    print(
        f"Polling every {interval} seconds..."
    )

    print(
        f"Device flow expires in approximately "
        f"{expires_in} seconds."
    )

    token_url = (
        "https://login.tado.com/oauth2/token"
    )

    start_time = time.time()

    # --------------------------------------------------------
    # Poll OAuth token endpoint.
    # --------------------------------------------------------

    while True:

        elapsed = time.time() - start_time

        if elapsed >= expires_in:

            raise RuntimeError(
                "Tado OAuth device code expired before "
                "token activation."
            )

        time.sleep(interval)

        response = requests.post(
            token_url,
            data={
                "client_id": client_id,
                "device_code": device_code,
                "grant_type":
                    "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers={
                "Content-Type":
                    "application/x-www-form-urlencoded",
            },
            timeout=30,
        )

        print(
            "Tado token poll: "
            f"HTTP {response.status_code}"
        )

        # ----------------------------------------------------
        # SUCCESS
        # ----------------------------------------------------

        if response.status_code == 200:

            try:
                token_data = response.json()

            except Exception:
                raise RuntimeError(
                    "Tado returned HTTP 200 but the response "
                    "was not valid JSON."
                )

            refresh_token = token_data.get(
                "refresh_token"
            )

            if not refresh_token:

                raise RuntimeError(
                    "Tado returned HTTP 200 but did not "
                    "provide a refresh token."
                )

            print(
                "Tado OAuth token successfully obtained."
            )

            # ------------------------------------------------
            # Save refresh token in the format PyTado expects.
            # ------------------------------------------------

            with open(
                TADO_TOKEN_FILE,
                "w",
                encoding="utf-8"
            ) as f:

                json.dump(
                    {
                        "refresh_token":
                            refresh_token
                    },
                    f
                )

            print(
                "Tado refresh token saved."
            )

            # ------------------------------------------------
            # Create fresh authenticated PyTado instance.
            # ------------------------------------------------

            print(
                "Initialising authenticated PyTado instance..."
            )

            authenticated_tado = Tado(
                token_file_path=TADO_TOKEN_FILE,
                saved_refresh_token=refresh_token,
            )

            print(
                "Authenticated PyTado instance created."
            )

            return authenticated_tado

        # ----------------------------------------------------
        # Decode OAuth error.
        # ----------------------------------------------------

        try:

            error_data = response.json()

        except Exception:

            error_data = {
                "raw_response":
                    response.text
            }

        error_code = error_data.get(
            "error"
        )

        print(
            f"Tado OAuth response: {error_data}"
        )

        # ----------------------------------------------------
        # User has not completed authentication yet.
        # ----------------------------------------------------

        if (
            response.status_code == 400
            and error_code == "authorization_pending"
        ):

            print(
                "Tado authorization still pending..."
            )

            continue

        # ----------------------------------------------------
        # Tado wants us to poll more slowly.
        # ----------------------------------------------------

        if (
            response.status_code == 400
            and error_code == "slow_down"
        ):

            interval += 5

            print(
                "Tado requested slower polling. "
                f"New interval: {interval}s"
            )

            continue

        # ----------------------------------------------------
        # Device code expired.
        # ----------------------------------------------------

        if error_code == "expired_token":

            raise RuntimeError(
                "Tado OAuth device code has expired."
            )

        # ----------------------------------------------------
        # User denied access.
        # ----------------------------------------------------

        if error_code == "access_denied":

            raise RuntimeError(
                "Tado OAuth authorization was denied."
            )

        # ----------------------------------------------------
        # Anything else: show the real Tado response.
        # ----------------------------------------------------

        raise RuntimeError(
            "Tado OAuth token exchange failed.\n"
            f"HTTP status: {response.status_code}\n"
            f"Response: {error_data}"
        )


# ============================================================
# TADO LOGIN
# ============================================================

def tado_login(username, password):

    print(
        "Initialising Tado authentication..."
    )

    # --------------------------------------------------------
    # First try an existing refresh token.
    # --------------------------------------------------------

    if os.path.exists(
        TADO_TOKEN_FILE
    ):

        print(
            "Existing Tado refresh token found. "
            "Attempting to reuse it..."
        )

        try:

            tado = Tado(
                token_file_path=TADO_TOKEN_FILE
            )

            print(
                "Existing Tado refresh token accepted."
            )

            return tado

        except Exception as e:

            print(
                "Existing Tado refresh token could not "
                "be used:"
            )

            print(
                str(e)
            )

            try:
                os.remove(
                    TADO_TOKEN_FILE
                )
            except OSError:
                pass

            print(
                "Starting a new Tado OAuth device flow..."
            )

    # --------------------------------------------------------
    # Start new PyTado device flow.
    # --------------------------------------------------------

    tado = Tado(
        token_file_path=TADO_TOKEN_FILE
    )

    status = tado.device_activation_status()

    print(
        "Initial Tado device activation status: "
        f"{status}"
    )

    if status != "PENDING":

        raise RuntimeError(
            "Unexpected Tado device activation status: "
            f"{status}"
        )

    url = tado.device_verification_url()

    if not url:

        raise RuntimeError(
            "Tado did not provide a device verification URL."
        )

    print(
        "Tado device verification URL obtained."
    )

    print(
        f"Authentication URL: {url}"
    )

    # --------------------------------------------------------
    # Browser login.
    # --------------------------------------------------------

    asyncio.run(
        browser_login(
            url,
            username,
            password
        )
    )

    print(
        "Browser authentication completed."
    )

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # Do NOT call:
    #
    #     tado.device_activation()
    #
    # This is the call currently failing with:
    #
    #     TadoException: Login failed. Reason:
    #     Bad Request
    #
    # Instead perform the OAuth token exchange ourselves.
    # --------------------------------------------------------

    print(
        "Bypassing PyTado.device_activation()..."
    )

    return complete_tado_device_activation(
        tado
    )


# ============================================================
# OCTOPUS API
# ============================================================

def octopus_request(
    endpoint,
    params=None
):

    url = (
        "https://api.octopus.energy/v1/"
        + endpoint
    )

    response = requests.get(
        url,
        auth=HTTPBasicAuth(
            OCTOPUS_API_KEY,
            ""
        ),
        params=params,
        timeout=60,
    )

    if response.status_code != 200:

        raise RuntimeError(
            "Octopus API request failed.\n"
            f"HTTP status: {response.status_code}\n"
            f"URL: {url}\n"
            f"Response: {response.text[:1000]}"
        )

    return response.json()


# ============================================================
# GET OCTOPUS GAS CONSUMPTION
# ============================================================

def get_consumption_since_date(
    start_date
):

    print(
        "Getting Octopus gas consumption since "
        f"{start_date.isoformat()}..."
    )

    endpoint = (
        f"gas-meter-points/"
        f"{MPRN}/"
        f"meters/"
        f"{GAS_METER_SERIAL_NUMBER}/"
        f"consumption/"
    )

    total_kwh = 0.0

    page_size = 2500

    params = {
        "group_by": "quarter",
        "period_from":
            start_date.astimezone(
                timezone.utc
            ).isoformat(),
        "page_size": page_size,
    }

    while True:

        data = octopus_request(
            endpoint,
            params
        )

        results = data.get(
            "results",
            []
        )

        for item in results:

            consumption = item.get(
                "consumption"
            )

            if consumption is not None:

                total_kwh += float(
                    consumption
                )

        next_url = data.get(
            "next"
        )

        if not next_url:
            break

        # The Octopus API next URL is already complete.
        response = requests.get(
            next_url,
            auth=HTTPBasicAuth(
                OCTOPUS_API_KEY,
                ""
            ),
            timeout=60,
        )

        if response.status_code != 200:

            raise RuntimeError(
                "Octopus pagination request failed.\n"
                f"HTTP status: "
                f"{response.status_code}\n"
                f"Response: "
                f"{response.text[:1000]}"
            )

        data = response.json()

        results = data.get(
            "results",
            []
        )

        for item in results:

            consumption = item.get(
                "consumption"
            )

            if consumption is not None:

                total_kwh += float(
                    consumption
                )

        next_url = data.get(
            "next"
        )

        if not next_url:
            break

        params = None

        # Continue through the URL manually.
        while next_url:

            response = requests.get(
                next_url,
                auth=HTTPBasicAuth(
                    OCTOPUS_API_KEY,
                    ""
                ),
                timeout=60,
            )

            if response.status_code != 200:

                raise RuntimeError(
                    "Octopus pagination request failed.\n"
                    f"HTTP status: "
                    f"{response.status_code}\n"
                    f"Response: "
                    f"{response.text[:1000]}"
                )

            page = response.json()

            for item in page.get(
                "results",
                []
            ):

                consumption = item.get(
                    "consumption"
                )

                if consumption is not None:

                    total_kwh += float(
                        consumption
                    )

            next_url = page.get(
                "next"
            )

        break

    print(
        f"Octopus consumption retrieved: "
        f"{total_kwh:.3f} kWh"
    )

    return total_kwh


# ============================================================
# TADO ENERGY IQ - GET LATEST READING
# ============================================================

def get_latest_tado_gas_reading(tado):

    print(
        "Getting latest Tado Energy IQ gas reading..."
    )

    try:

        readings = tado.get_eiq_meter_readings()

    except Exception as e:

        print(
            "Could not retrieve Tado Energy IQ readings:"
        )

        print(
            str(e)
        )

        return None

    if not readings:

        print(
            "No existing Tado Energy IQ readings found."
        )

        return None

    # --------------------------------------------------------
    # The PyTado response format can vary between versions.
    # Try to locate the most recent reading.
    # --------------------------------------------------------

    latest = None

    if isinstance(
        readings,
        list
    ):

        if readings:
            latest = readings[-1]

    elif isinstance(
        readings,
        dict
    ):

        if "results" in readings:

            results = readings[
                "results"
            ]

            if results:
                latest = results[-1]

        elif "readings" in readings:

            results = readings[
                "readings"
            ]

            if results:
                latest = results[-1]

    if latest is None:

        print(
            "Unable to identify the latest Tado "
            "Energy IQ reading."
        )

        return None

    print(
        f"Latest Tado Energy IQ reading: {latest}"
    )

    return latest


# ============================================================
# BUILD TOTAL CONSUMPTION
# ============================================================

def get_meter_reading_total_consumption(
    tado
):

    latest = get_latest_tado_gas_reading(
        tado
    )

    # --------------------------------------------------------
    # Existing Tado reading available.
    # --------------------------------------------------------

    if latest:

        # Try common field names.
        latest_value = None

        for key in [
            "value",
            "consumption",
            "reading",
            "amount",
            "total",
        ]:

            if key in latest:

                try:

                    latest_value = float(
                        latest[key]
                    )

                    break

                except (
                    TypeError,
                    ValueError
                ):

                    pass

        latest_date = None

        for key in [
            "date",
            "timestamp",
            "datetime",
            "time",
        ]:

            if key in latest:

                try:

                    latest_date = datetime.fromisoformat(
                        str(
                            latest[key]
                        ).replace(
                            "Z",
                            "+00:00"
                        )
                    )

                    break

                except Exception:
                    pass

        if (
            latest_value is not None
            and latest_date is not None
        ):

            print(
                "Calculating Octopus consumption "
                "after latest Tado reading..."
            )

            delta = get_consumption_since_date(
                latest_date
            )

            total = (
                latest_value
                + delta
            )

            print(
                f"Previous Tado reading: "
                f"{latest_value:.3f}"
            )

            print(
                f"Octopus delta: "
                f"{delta:.3f}"
            )

            print(
                f"New calculated total: "
                f"{total:.3f}"
            )

            return total

    # --------------------------------------------------------
    # No usable Tado reading.
    #
    # Fall back to the previous 3 years, as the original
    # script did.
    # --------------------------------------------------------

    print(
        "No usable previous Tado reading. "
        "Retrieving up to 3 years of Octopus consumption..."
    )

    start_date = (
        datetime.now(
            timezone.utc
        )
        - timedelta(
            days=365 * 3
        )
    )

    total = get_consumption_since_date(
        start_date
    )

    print(
        f"Three-year calculated total: "
        f"{total:.3f}"
    )

    return total


# ============================================================
# SET TADO ENERGY IQ METER READING
# ============================================================

def set_tado_gas_reading(
    tado,
    total_consumption
):

    print(
        "Updating Tado Energy IQ gas meter..."
    )

    print(
        f"Gas total to send: "
        f"{total_consumption:.3f}"
    )

    result = tado.set_eiq_meter_readings(
        {
            "gas": total_consumption
        }
    )

    print(
        "Tado Energy IQ meter update completed."
    )

    return result


# ============================================================
# MAIN
# ============================================================

def main():

    print("")
    print("=" * 50)
    print(
        "Octopus -> Tado Energy IQ sync"
    )
    print("=" * 50)

    print(
        f"MPRN: {MPRN}"
    )

    print(
        f"Gas meter serial: "
        f"{GAS_METER_SERIAL_NUMBER}"
    )

    # --------------------------------------------------------
    # Configuration
    # --------------------------------------------------------

    check_configuration()

    # --------------------------------------------------------
    # Tado authentication
    # --------------------------------------------------------

    print(
        "Authenticating with Tado..."
    )

    tado = tado_login(
        TADO_USERNAME,
        TADO_PASSWORD
    )

    print(
        "Tado authentication successful."
    )

    # --------------------------------------------------------
    # Get Octopus consumption
    # --------------------------------------------------------

    total_consumption = (
        get_meter_reading_total_consumption(
            tado
        )
    )

    if total_consumption is None:

        fail(
            "Could not calculate gas consumption."
        )

    # --------------------------------------------------------
    # Update Tado
    # --------------------------------------------------------

    set_tado_gas_reading(
        tado,
        total_consumption
    )

    print("")
    print("=" * 50)
    print(
        "SYNC COMPLETED SUCCESSFULLY"
    )
    print("=" * 50)
    print("")


if __name__ == "__main__":
    main()
