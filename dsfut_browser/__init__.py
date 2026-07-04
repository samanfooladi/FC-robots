"""
DSFUT board automation (browser-based).

Replaces the old partner-API poller (dsfut/): the public API served a
different queue and never returned account credentials. The real order board
lives on the dsfut.net website behind a manually-solved captcha login, so we
drive it with a persistent Playwright/Chromium context instead.

Scope of this module (step 1): detect eligible console orders on the homepage
and pick them up. Credential/account-detail extraction is a later step — for
now a successful pickup only records a minimal stub row.
"""
