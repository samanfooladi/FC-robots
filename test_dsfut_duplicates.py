import unittest
from unittest.mock import AsyncMock, patch

from dsfut_browser.poller import DsfutBrowserPoller


class DsfutDuplicateOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_is_loaded_into_handled_ids(self) -> None:
        poller = DsfutBrowserPoller()

        with patch(
            "dsfut_browser.poller.get_known_dsfut_order_ids",
            AsyncMock(return_value={"109086", "109001"}),
        ):
            await poller._load_handled_history()

        self.assertEqual(poller._handled, {"109086", "109001"})

    async def test_cancelled_old_order_is_skipped_before_pickup(self) -> None:
        poller = DsfutBrowserPoller()
        poller._handled.add("109086")
        poller._attempt = AsyncMock(return_value=True)

        await poller._handle_batch([
            {"id": 109086, "hash": "old-order"},
            {"id": 109087, "hash": "fresh-order"},
        ])

        poller._attempt.assert_awaited_once()
        attempted_order = poller._attempt.await_args.args[0]
        self.assertEqual(attempted_order["id"], 109087)


if __name__ == "__main__":
    unittest.main()
