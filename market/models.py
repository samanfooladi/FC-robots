from dataclasses import dataclass


@dataclass
class CardListing:
    """One auction entry returned by the Transfer Market search endpoint."""
    trade_id: int
    item_id: int       # itemData.id  — needed to list the card after buying
    buy_now_price: int
    resource_id: int   # itemData.resourceId / maskedDefId — the player template
    start_price: int   # current / starting bid
    player_name: str = ""  # commonName / lastName from itemData, empty if absent
    rating: int = 0    # itemData.rating — player overall, 0 if absent


@dataclass
class BuyResult:
    success: bool
    item_id: int | None = None
    price_paid: int | None = None
    error: str | None = None  # "session_expired" | httpx error string | None


@dataclass
class ListResult:
    success: bool
    trade_id: str | None = None   # idStr from the auctionhouse response
    listed_price: int | None = None
    error: str | None = None      # "session_expired" | error string | None
