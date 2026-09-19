import json
import time
import webbrowser
from pathlib import Path

import requests


TADO_CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"

DEVICE_AUTHORIZE_URL = (
    "https://login.tado.com/oauth2/device_authorize"
)

TOKEN_URL = (
    "https://login.tado.com/oauth2/token"
)

OUTPUT_FILE = Path("tado_refresh_token.txt")


def main():
    print()
    print("==============================================")
    print("TADO REFRESH TOKEN SETUP")
    print("==============================================")
    print()

    print("Requesting a Tado device code...")

    response = requests.post(
        DEVICE_AUTHORIZE_URL,
        params={
            "client_id": TADO_CLIENT_ID,
            "scope": "offline_access",
        },
        timeout=30,
    )

    if response.status_code != 200:
        raise RuntimeError(
            "Could not start Tado device authorization.\n"
            f"HTTP {response.status_code}\n"
            f"{response.text}"
        )

    data = response.json()

    device_code = data.get("device_code")
    user_code = data.get("user_code")
    verification_uri = data.get(
        "verification_uri"
    )
    verification_uri_complete = data.get(
        "verification_uri_complete"
    )
    expires_in = int(
        data.get("expires_in", 300)
    )
    interval = int(
        data.get("interval", 5)
    )

    if not device_code:
        raise RuntimeError(
            "Tado did not return a device_code."
        )

    print()
    print("Tado authorization URL:")
    print()
    print(verification_uri_complete)
    print()
    print(f"User code: {user_code}")
    print()
    print(
        "Opening the authorization page in your "
        "normal browser..."
    )
    print()

    try:
        webbrowser.open(
            verification_uri_complete
        )
    except Exception:
        pass

    print(
        "Log into Tado and confirm the authorization."
    )
    print()
    print(
        "Waiting for Tado authorization..."
    )
    print(
        f"Authorization expires in "
        f"approximately {expires_in} seconds."
    )
    print()

    deadline = time.time() + expires_in

    while time.time() < deadline:
        response = requests.post(
            TOKEN_URL,
            params={
                "client_id": TADO_CLIENT_ID,
                "device_code": device_code,
                "grant_type": (
                    "urn:ietf:params:oauth:"
                    "grant-type:device_code"
                ),
            },
            timeout=30,
        )

        try:
            result = response.json()
        except Exception:
            result = {}

        if response.status_code == 200:
            refresh_token = result.get(
                "refresh_token"
            )

            if not refresh_token:
                raise RuntimeError(
                    "Tado authorization succeeded but "
                    "no refresh token was returned."
                )

            OUTPUT_FILE.write_text(
                refresh_token,
                encoding="utf-8",
            )

            print()
            print(
                "=============================================="
            )
            print(
                "SUCCESS - TADO REFRESH TOKEN OBTAINED"
            )
            print(
                "=============================================="
            )
            print()
            print(
                f"Saved to: {OUTPUT_FILE.resolve()}"
            )
            print()
            print(
                "DO NOT commit this file to GitHub."
            )
            print()
            print(
                "Now add the contents of this file as "
                "the GitHub repository secret:"
            )
            print()
            print(
                "TADO_REFRESH_TOKEN"
            )
            print()

            return

        error = result.get("error")

        if error == "authorization_pending":
            print(
                "Still waiting for Tado authorization..."
            )

        elif error == "slow_down":
            print(
                "Tado requested slower polling..."
            )
            interval += 5

        elif error == "access_denied":
            raise RuntimeError(
                "Tado authorization was denied."
            )

        elif error == "expired_token":
            raise RuntimeError(
                "The Tado device authorization expired."
            )

        else:
            raise RuntimeError(
                "Unexpected Tado OAuth response:\n"
                + json.dumps(
                    result,
                    indent=2,
                )
            )

        time.sleep(interval)

    raise RuntimeError(
        "Tado authorization timed out."
    )


if __name__ == "__main__":
    main()
