import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from dsfut_browser.poller import DsfutBrowserPoller, DsfutPollerManager


class DsfutBrowserLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_keeps_browser_open_until_poller_stops(self) -> None:
        poller = DsfutBrowserPoller()
        poller.session.start = AsyncMock()
        poller.session.goto_board = AsyncMock()
        poller.session.ensure_logged_in = AsyncMock(return_value=True)
        poller.session.export_cookies = AsyncMock(return_value=[{"name": "session"}])
        poller.session.close = AsyncMock()
        poller.http.rebuild = AsyncMock(return_value=1)

        self.assertTrue(await poller._login_and_load_cookies())

        poller.session.start.assert_awaited_once()
        poller.session.goto_board.assert_awaited_once()
        poller.session.ensure_logged_in.assert_awaited_once_with(navigate=False)
        poller.session.close.assert_not_awaited()
        self.assertTrue(poller._board_opened.is_set())

    async def test_manager_waits_for_browser_and_take_off_stops_it(self) -> None:
        class FakePoller:
            def __init__(self, bot=None) -> None:
                self._board_opened = asyncio.Event()
                self._stop = asyncio.Event()

            async def run(self) -> None:
                self._board_opened.set()
                await self._stop.wait()

            async def wait_until_board_opened(self) -> None:
                await self._board_opened.wait()

            def request_stop(self) -> None:
                self._stop.set()

        with patch("dsfut_browser.poller.DsfutBrowserPoller", FakePoller):
            manager = DsfutPollerManager(bot=None, enabled_in_env=True)
            self.assertTrue(await manager.start())
            self.assertTrue(manager.is_running())
            self.assertTrue(await manager.stop())
            self.assertFalse(manager.is_running())

    async def test_manager_reports_failure_before_board_opens(self) -> None:
        class FailingPoller:
            def __init__(self, bot=None) -> None:
                self._board_opened = asyncio.Event()

            async def run(self) -> None:
                raise RuntimeError("Chromium launch failed")

            async def wait_until_board_opened(self) -> None:
                await self._board_opened.wait()

        with patch("dsfut_browser.poller.DsfutBrowserPoller", FailingPoller):
            manager = DsfutPollerManager(bot=None, enabled_in_env=True)
            with self.assertRaisesRegex(RuntimeError, "Chromium launch failed"):
                await manager.start()
            self.assertFalse(manager.is_running())


if __name__ == "__main__":
    unittest.main()
