"""
DSFUT comfort-trade automation.

The real order board lives on the dsfut.net website behind a manually-solved
captcha login. Login is handled with a persistent Playwright/Chromium context
(session.py); everything after that is a fast HTTP loop over the site's own
endpoints (no browser in the hot path):

    session.py      Playwright login + cookie export (captcha handled by a human)
    http_client.py  httpx client seeded with those cookies (poll/pickup/active)
    parser.py       pure parsing of the comfortables JSON + <fc-comfortable> HTML
    poller.py       the loop: poll → pick up PS/Xbox orders → extract account
                    details → store in dsfut_orders + auto-create the EA account
                    + notify admins; re-login via Playwright when cookies expire
"""
