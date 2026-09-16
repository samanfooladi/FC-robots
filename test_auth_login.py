import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from auth.login import _fill_credentials, first_login
from auth.session import SessionData


class LoginSessionDetectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_sid_arriving_while_waiting_for_email_skips_credentials(self) -> None:
        page = AsyncMock()

        async def wait_for_selector(selector: str, **kwargs):
            self.assertEqual(selector, "#email")
            await asyncio.sleep(10)

        page.wait_for_selector.side_effect = wait_for_selector
        sid_event = asyncio.Event()
        captured = {"_sid_event": sid_event}

        async def publish_sid() -> None:
            await asyncio.sleep(0.01)
            captured["sid"] = "remembered-session"
            sid_event.set()

        publisher = asyncio.create_task(publish_sid())
        submitted = await _fill_credentials(
            page,
            "user@example.com",
            "secret",
            captured=captured,
        )
        await publisher

        self.assertFalse(submitted)
        page.fill.assert_not_awaited()
        page.click.assert_not_awaited()

    async def test_first_login_accepts_an_already_authenticated_profile(self) -> None:
        page = AsyncMock()
        captured = {
            "sid": "remembered-session",
            "_sid_event": asyncio.Event(),
        }
        captured["_sid_event"].set()
        expected = SessionData(
            account_id=11,
            sid="remembered-session",
            phishing_token="token",
            access_token="",
            nucleus_id="",
            cookies={},
        )

        with (
            patch("auth.login._register_session_listeners", return_value=captured),
            patch("auth.login._goto_webapp", new=AsyncMock()),
            patch("auth.login._capture_ut_session", new=AsyncMock(return_value=expected)),
            patch("auth.login._handle_2fa_backup_code", new=AsyncMock()) as handle_2fa,
        ):
            actual = await first_login(
                page,
                account_id=11,
                email="user@example.com",
                password="secret",
                backup_code="unused-code",
                max_attempts=1,
            )

        self.assertIs(actual, expected)
        page.fill.assert_not_awaited()
        page.click.assert_not_awaited()
        handle_2fa.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
