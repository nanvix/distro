"""SDK release-contract tests."""

from __future__ import annotations

import subprocess
import unittest
import urllib.error
from http.client import HTTPMessage
from pathlib import Path
from unittest.mock import MagicMock, patch

import nanvix_distro.sdk as sdk
from nanvix_distro.sdk import SDKContract
from z import PORTS

REPO_ROOT = Path(__file__).parents[1]


class SDKContractTests(unittest.TestCase):
    """Verify the committed SDK coordinate and representative consumers."""

    def test_committed_contract(self) -> None:
        """The committed contract and Nanvix gitlink select the same release."""
        contract = SDKContract.load(REPO_ROOT / "config" / "sdk-release.json")
        nanvix_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT / "nanvix",
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        self.assertEqual(contract.nanvix_commit, nanvix_commit)
        self.assertEqual(
            contract.qualified_release_tag("package-version"),
            f"package-version-nanvix-{contract.sdk_version.removeprefix('v')}",
        )

    def test_all_port_locks_match(self) -> None:
        """Every SDK consumer agrees with the committed contract."""
        contract = SDKContract.load(REPO_ROOT / "config" / "sdk-release.json")
        consumers = [spec[0] for spec in PORTS.values()]

        for relative in consumers:
            with self.subTest(port=relative):
                contract.validate_port(REPO_ROOT / relative)

    def test_authenticated_fetch_uses_bearer_token(self) -> None:
        """GitHub requests authenticate with the caller's token."""
        response = MagicMock()
        response.read.return_value = b'{"value": "ok"}'
        response.__enter__.return_value = response

        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            sdk._fetch_json(  # pyright: ignore[reportPrivateUsage]
                "https://api.github.com/example",
                token="test-token",
            )

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")

    def test_transient_fetch_failure_is_retried(self) -> None:
        """Temporary GitHub service failures do not abort an upgrade."""
        response = MagicMock()
        response.read.return_value = b'{"value": "ok"}'
        response.__enter__.return_value = response
        unavailable = urllib.error.HTTPError(
            "https://api.github.com/example",
            503,
            "Service Unavailable",
            HTTPMessage(),
            None,
        )

        with (
            patch.object(sdk, "_FETCH_RETRY_DELAYS", (0.0,)),
            patch(
                "urllib.request.urlopen", side_effect=[unavailable, response]
            ) as urlopen,
            patch("nanvix_distro.sdk.time.sleep") as sleep,
        ):
            document, _ = sdk._fetch_json(  # pyright: ignore[reportPrivateUsage]
                "https://api.github.com/example",
                token=None,
            )

        self.assertEqual(document, {"value": "ok"})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(0.0)

    def test_permanent_fetch_failure_is_not_retried(self) -> None:
        """Permanent GitHub errors fail immediately."""
        not_found = urllib.error.HTTPError(
            "https://api.github.com/example",
            404,
            "Not Found",
            HTTPMessage(),
            None,
        )

        with (
            patch.object(sdk, "_FETCH_RETRY_DELAYS", (0.0,)),
            patch("urllib.request.urlopen", side_effect=not_found) as urlopen,
            patch("nanvix_distro.sdk.time.sleep") as sleep,
            self.assertRaises(sdk.ContractError),
        ):
            sdk._fetch_json(  # pyright: ignore[reportPrivateUsage]
                "https://api.github.com/example",
                token=None,
            )

        urlopen.assert_called_once()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
