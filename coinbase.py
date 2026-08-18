import ccxt
import logging
import os
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from readfile import *


load_dotenv()

exchange = ccxt.coinbase({
    'apiKey': os.getenv("COINBASE_APIKEY"),
    'secret': os.getenv("COINBASE_SECRET"),
    'enableRateLimit': True,
})

_fernet: Fernet = None

def _get_fernet() -> Fernet:
    """
    Lazily build the Fernet cipher so importing this module doesn't require
    FERNET_KEY to be set (only the credential-decrypting paths need it).
    """
    global _fernet # sets _fernet as a global variable
    if _fernet is None:
        key = os.getenv("FERNET_KEY")
        if not key:
            raise RuntimeError(
                "FERNET_KEY is not set - cannot decrypt participant credentials"
            )
        _fernet = Fernet(key.encode())
    return _fernet


def build_exchange(api_key: str, api_secret: str, exchange_id: str = 'coinbase',
                   encrypted: bool = True, passphrase: str = None,
                   portfolio_uuid: str = None, **options) -> ccxt.Exchange:
    """
    Build a ccxt exchange instance for a single participant.

    Credentials in the `participants_credentials` table are Fernet-encrypted
    by sign_ups.py, so `encrypted` defaults to True - that's the pipeline path.
    Pass encrypted=False for plaintext keys (e.g. your own from .env).

    `exchange_id` must match ccxt's exact id (see ccxt.exchanges), which is
    what the `exchange` column of participants_credentials stores.

    `passphrase` is only needed by venues whose requiredCredentials include
    'password' - coinbaseinternational does, plain coinbase does not.

    `portfolio_uuid` scopes the instance to one Coinbase portfolio. The perp
    (INTX) endpoints are portfolio-scoped and ccxt reads the UUID from
    options['portfolio'], so setting it here means fetch_positions() works
    without repeating params at every call site.
    """
    if not api_key or not api_secret:
        raise ValueError("Both api_key and api_secret are required")

    if encrypted:
        fernet = _get_fernet()
        try:
            api_key = fernet.decrypt(api_key.encode()).decode()
            api_secret = fernet.decrypt(api_secret.encode()).decode()
            if passphrase:
                passphrase = fernet.decrypt(passphrase.encode()).decode()
        except InvalidToken as e:
            # Either the value was stored in plaintext or FERNET_KEY has rotated.
            raise ValueError(
                "Could not decrypt credentials - wrong FERNET_KEY, or the stored "
                "values aren't encrypted (try encrypted=False)"
            ) from e

    if not hasattr(ccxt, exchange_id):
        raise ValueError(
            f"'{exchange_id}' is not a valid ccxt exchange id - check ccxt.exchanges"
        )

    config = {
        'apiKey': api_key,
        'secret': api_secret,
        'enableRateLimit': True,
        **options,
    }
    if passphrase:
        config['password'] = passphrase  # ccxt's name for the passphrase

    exchange = getattr(ccxt, exchange_id)(config)

    if portfolio_uuid:
        exchange.options['portfolio'] = portfolio_uuid

    return exchange


def price_balances_in_usdc(exchange: ccxt.Exchange, balances: dict = None, price_cache: dict = None) -> float:
    """
    Convert a {coin: amount} balance dict into a total USDC value.
    If balances is None, fetches the exchange's default account balance
    (i.e. exchange.fetch_balance() with no type override) and prices that.
    """

    logger = logging.getLogger(__name__)

    USD_EQUIVALENTS = {
            'USDC', 'USDT', 'USD', 'BUSD', 'DAI', 'TUSD', 'USDP', 'GUSD',
            'FDUSD', 'USDD', 'FRAX', 'LUSD', 'SUSD', 'USDN', 'USDJ', 'MAMUSD',
        }
    QUOTE_PRIORITY = ['USDC', 'USDT', 'USD', 'BUSD', 'FDUSD']

    if price_cache is None:
        price_cache = {}

    exchange.load_markets()  # cheap no-op if already loaded - safe to call standalone

    if balances is None:
        balance = exchange.fetch_balance(params={'v3': True})
        balances = balance.get('total') or {}

    markets = exchange.markets
    total_usdc_value = 0.0

    for coin, amount in balances.items(): # coin is a the key and amount is the value of the key in the dictionary, {coin: amount}.
        if not amount or amount <= 0:
            continue
        if coin in USD_EQUIVALENTS: # is the coin in the list of USD_EQUIVALENTS? If yes, add the amount to total_usdc_value and continue to the next coin.
            total_usdc_value += amount
            continue
        if coin in price_cache: # is the coin in the price_cache? If yes, multiply the amount by the price in the cache and add it to total_usdc_value and continue to the next coin.
            total_usdc_value += amount * price_cache[coin]
            continue

        price = None

        for quote in QUOTE_PRIORITY:
            symbol = f"{coin}/{quote}" # i.e., SOL/USDT, ETH/USDC, etc.
            if symbol not in markets:
                continue
            try:
                ticker = exchange.fetch_ticker(symbol)
            except Exception as e:
                logger.warning("Ticker error for %s: %s", symbol, e)
                continue

            last = ticker.get('last') or ticker.get('close') # get the last traded price or the closing price from the ticker
            if last:
                price = float(last)
                break

        price_cache[coin] = price or 0.0
        total_usdc_value += amount * (price or 0.0)
        if price is None:
            logger.info("Skipped %s (%s): no convertible market found", coin, amount)

    return total_usdc_value

if __name__ == "__main__":
    print(price_balances_in_usdc(exchange))


def _amount(field, default: float = 0.0) -> float:
    """
    Unwrap a Coinbase money field. INTX returns amounts either bare
    ("collateral": "12.0897") or wrapped ({"value": "12.0897",
    "currency": "USDC"}), so both shapes are handled here.
    """
    if field is None:
        return default
    if isinstance(field, dict):
        field = field.get('value')
    try:
        return float(field)
    except (TypeError, ValueError):
        return default


def find_perp_portfolio_uuid(exchange: ccxt.Exchange) -> str:
    """
    Discover the perpetuals (INTX) portfolio UUID for a set of credentials.

    Meant to be called ONCE at signup and stored in
    participant_api_keys.portfolio_uuid - it costs an extra API call, so
    don't put it in the per-run sync path.

    Returns None if the credentials can't see a perp portfolio, which is the
    normal answer for a spot-only key.
    """
    logger = logging.getLogger(__name__)

    portfolios = exchange.fetch_portfolios()
    for portfolio in portfolios:
        label = f"{portfolio.get('type') or ''} {(portfolio.get('info') or {}).get('name') or ''}".upper()
        if 'INTX' in label or 'PERP' in label:
            return portfolio.get('id')

    logger.warning(
        "No perp portfolio found for this key - portfolios seen: %s",
        [(p.get('type'), (p.get('info') or {}).get('name')) for p in portfolios]
    )
    return None


def get_perp_account_value(exchange: ccxt.Exchange, portfolio_uuid: str = None) -> dict:
    """
    Value a Coinbase perpetuals (INTX) portfolio in a single API call.

    Equity is read from summary.total_balance, which is the portfolio's net
    liquidation value. Unrealized PnL is reported alongside it but NOT added -
    see the warning below.

    Position notional is reported separately and is deliberately not treated
    as account value: a 5x levered position carries ~5x the notional of the
    equity backing it, so totalling notionals would let perp traders dwarf
    spot traders on a number measuring leverage rather than performance.

    Note neither fetch_portfolio_details() nor fetch_balance() works here -
    ccxt's parse_portfolio_details only reads `spot_positions`, and
    type='future' routes to the CFM (US futures) endpoint, not INTX.
    ----------------------------------------------------------------------
    {'timestamp': ..., 'datetime': ..., 'account_type': 'perp',
     'total_usdc': 12.0897, 'collateral_usdc': 12.0897,
     'unrealized_pnl_usdc': 0.0, 'notional_usdc': 10.0997,
     'buying_power_usdc': 10.06976}

    For per-position detail (symbol, side, leverage, liquidation price) call
    exchange.fetch_positions() separately - this endpoint only reports
    portfolio-level aggregates.
    """
    logger = logging.getLogger(__name__)

    timestamp = exchange.milliseconds()

    if not portfolio_uuid:
        portfolio_uuid = exchange.options.get('portfolio')
    if not portfolio_uuid:
        # Fall back to discovery so ad hoc calls work, but say so loudly: this
        # is an extra API call on every sync if the UUID isn't stored.
        logger.warning(
            "No portfolio UUID given - discovering it. Store it in "
            "participant_api_keys.portfolio_uuid to skip this lookup."
        )
        portfolio_uuid = find_perp_portfolio_uuid(exchange)
    if not portfolio_uuid:
        raise ValueError(
            "No perp portfolio available for these credentials - pass "
            "portfolio_uuid, or build the exchange with build_exchange(..., "
            "portfolio_uuid=...)"
        )

    response = exchange.v3PrivateGetBrokerageIntxPortfolioPortfolioUuid(
        {'portfolio_uuid': portfolio_uuid}
    )

    summary = response.get('summary') or {}
    portfolios = response.get('portfolios') or []
    # Per-portfolio detail (notional, collateral) lives in the list; the
    # top-level summary carries the headline balances.
    portfolio = portfolios[0] if portfolios else {}

    if not summary and not portfolio:
        logger.warning(
            "INTX portfolio %s returned no summary or portfolios (keys: %s)",
            portfolio_uuid, list(response.keys())
        )

    return {
        'timestamp': timestamp,
        'datetime': exchange.iso8601(timestamp),
        'account_type': 'perp',
        # Net liquidation value - the figure to rank participants on
        'total_usdc': _amount(summary.get('total_balance') or portfolio.get('total_balance')),
        'collateral_usdc': _amount(portfolio.get('collateral')),
        'unrealized_pnl_usdc': _amount(summary.get('unrealized_pnl') or portfolio.get('unrealized_pnl')),
        'notional_usdc': _amount(portfolio.get('position_notional')),
        'buying_power_usdc': _amount(summary.get('buying_power')),
    }

# print(get_perp_account_value(exchange))


def get_account_totals_usdc(exchange: ccxt.Exchange, account_type: str = 'spot',
                            portfolio_uuid: str = None) -> dict:
    """
    Value a participant's account in USDC.

    `account_type` selects the venue: 'spot' walks the wallet types and prices
    each coin; 'perp' hands off to get_perp_account_value(). Both return a
    'total_usdc' headline figure so callers (and the balance_snapshots table)
    can treat them the same way.

    On exchanges where spot/margin/future share a single Unified Trading
    Account, ccxt can return identical balances for each wallet type -
    exact repeats are skipped so the same funds aren't counted twice.
    This only catches byte-for-byte identical snapshots; drop it if your
    exchange doesn't actually use a UTA.
    ----------------------------------------------------------------------
    {'timestamp': 1783159194622, 'datetime': '2026-07-04T09:59:54.622Z',
     'account_type': 'spot', 'total_usdc': 129.18400834, 'spot_total_usdc': 129.18400834}
    """
    logger = logging.getLogger(__name__)

    if account_type == 'perp':
        return get_perp_account_value(exchange, portfolio_uuid)

    USD_EQUIVALENTS = {
        'USDC', 'USDT', 'USD', 'BUSD', 'DAI', 'TUSD', 'USDP', 'GUSD',
        'FDUSD', 'USDD', 'FRAX', 'LUSD', 'SUSD', 'USDN', 'USDJ', 'MAMUSD',
    }
    QUOTE_PRIORITY = ['USDC', 'USDT', 'USD', 'BUSD', 'FDUSD']
    # Only ask for wallet types the exchange actually has. Coinbase has no
    # options product, and 'future' resolves to the CFM (US futures) balance
    # endpoint, which PERMISSION_DENIEDs for every spot-only key - two
    # guaranteed errors per participant per run if left in.
    WALLET_TYPES = exchange.options.get('walletTypes') or ['spot']

    exchange.load_markets()
    timestamp = exchange.milliseconds()

    account_totals = {
        'timestamp': timestamp,
        'datetime': exchange.iso8601(timestamp),
        'account_type': account_type,
        'total_usdc': 0.0,
        **{f'{wt}_total_usdc': 0.0 for wt in WALLET_TYPES},
    }

    price_cache: dict = {}
    seen_wallet_signatures = set()

    for wallet_type in WALLET_TYPES:
        try:
            balance = exchange.fetch_balance(params={'type': wallet_type, 'v3': True})
        except ccxt.NotSupported:
            logger.debug("%s: %s wallet type not supported", exchange.id, wallet_type)
            continue
        except ccxt.ExchangeError as e:
            # Don't assume this means "not supported" - could be auth/permissions.
            logger.warning("%s: could not fetch %s balance: %s", exchange.id, wallet_type, e)
            continue
        except Exception as e:
            logger.warning("%s: unexpected error fetching %s balance: %s", exchange.id, wallet_type, e)
            continue

        raw_total = balance.get('total') or {}
        active_balances = {
            coin: amount for coin, amount in raw_total.items() if coin and amount and amount > 0
        }
        if not active_balances:
            continue

        signature = frozenset(active_balances.items())
        if signature in seen_wallet_signatures:
            continue
        seen_wallet_signatures.add(signature)

        wallet_total = price_balances_in_usdc(exchange, active_balances, price_cache)
        account_totals[f'{wallet_type}_total_usdc'] = wallet_total
        account_totals['total_usdc'] += wallet_total

    return account_totals                

if __name__ == "__main__":
     print(get_account_totals_usdc(exchange))                                 


def account_type_from_order(order: dict) -> str:
    """
    Read the venue off an order rather than assuming it from whichever key
    fetched it. Coinbase tags every order with `product_type`, and perps are
    reported as futures carrying a perpetual expiry type.

    Returns 'spot', 'perp', 'future', or None if Coinbase didn't say.
    """
    info = order.get('info') or {}
    product_type = (info.get('product_type') or '').upper()

    if product_type == 'SPOT':
        return 'spot'
    if product_type in ('FUTURE', 'PERPETUAL'):
        expiry_type = (info.get('contract_expiry_type') or '').upper()
        if product_type == 'PERPETUAL' or 'PERPETUAL' in expiry_type:
            return 'perp'
        return 'future'

    return product_type.lower() or None



def closed_orders(exchange: ccxt.Exchange, symbol: str = None, since=None, limit: int = 200,
                  portfolio_uuid: str = None) -> list[dict]:
    """
    Fetches Coinbase closed orders.

    `since` accepts either a millisecond timestamp (int - matches
    get_account_totals_usdc's `timestamp` field and ccxt's own convention,
    what the automated pipeline passes) or an ISO8601 string like
    '2026-05-01T00:00:00Z' (convenient for manual/ad hoc calls) - both get
    normalized to milliseconds internally. Defaults to 2026-01-01 if not
    given. Paginates automatically if a participant has more closed orders
    than fit in one page.

    `portfolio_uuid` scopes the fetch to one Coinbase portfolio. Without it
    the Advanced Trade API answers for the key's default portfolio only, so a
    participant's perp orders would be silently missing.
    """
    logger = logging.getLogger(__name__)

    request_params = {}
    if portfolio_uuid:
        request_params['retail_portfolio_id'] = portfolio_uuid

    if since is None:
        since = exchange.parse8601('2026-01-01T00:00:00Z')
    elif isinstance(since, str):
        since = exchange.parse8601(since)

    if since is None:
        raise ValueError(
            "since could not be resolved to a valid timestamp - pass milliseconds (int) "
            "or a parseable ISO8601 string like '2026-05-01T00:00:00Z'"
        )

    # symbol=None returns all symbols in one call on Coinbase
    raw_orders = []
    cursor = since
    while True:
        batch = exchange.fetch_closed_orders(symbol=symbol, since=cursor, limit=limit,
                                             params=request_params)
        if not batch:
            break
        raw_orders.extend(batch)
        if len(batch) < limit:
            break
        last_ts = batch[-1].get('timestamp')
        if last_ts is None:
            logger.warning("Last order in page has no timestamp, can't paginate further - stopping")
            break
        cursor = last_ts + 1

    clean_orders = []

    for order in raw_orders:
        try:
            filled = order.get('filled')
            amount = filled if filled is not None else order.get('amount')

            average = order.get('average')
            price = average if average is not None else order.get('price')

            fee_info = order.get('fee') or {}
            fee_cost = fee_info.get('cost')
            fee_currency = fee_info.get('currency')

            if fee_cost is None:
                fee_cost = (order.get('info') or {}).get('total_fees')

            if fee_currency is None and order.get('symbol'):
                parts = order['symbol'].split('/')
                if len(parts) == 2:
                    fee_currency = parts[1]

            if amount is None:
                logger.warning("Order %s has no amount/filled data, skipping", order.get('id'))
                continue
            if amount == 0:
                continue

            clean_order = {
                "participant_id": None,
                'account_type': account_type_from_order(order),
                'timestamp': order.get('timestamp'),
                'datetime': order.get('datetime'),
                'symbol': order.get('symbol'),
                'type': order.get('type'),
                'side': order.get('side'),
                'price': price,
                'amount': amount,
                'fee_cost': fee_cost,
                'fee_currency': fee_currency,
                "order_id": order.get("id")
            }

            clean_orders.append(clean_order)

        except Exception as e:
            logger.warning("Skipping malformed order %s: %s", order.get('id'), e)
            continue

    return clean_orders

# print(closed_orders(exchange, symbol=None, since = '2026-05-01T00:00:00Z'))

def closed_trades(exchange: ccxt.Exchange, symbol: str = None, since: str = None,
                  portfolio_uuid: str = None) -> list:
    if since:
        since_ms = exchange.parse8601(since)
    else:
        since_ms = exchange.parse8601('2026-01-01T00:00:00Z')

    request_params = {}
    if portfolio_uuid:
        request_params['retail_portfolio_id'] = portfolio_uuid

    raw_trades = exchange.fetch_my_trades(symbol=symbol, since=since_ms, params=request_params)

    clean_trades = []
    for order in raw_trades:
        fee_info = order.get('fee') or {}
        clean_trades.append({
            'account_type': account_type_from_order(order),
            'timestamp': order.get('timestamp'),
            'datetime': order.get('datetime'),
            'symbol': order.get('symbol'),
            'type': order.get('type'),
            'side': order.get('side'),
            'price': order.get('price'),
            'amount': order.get('amount'),
            'fee_cost': fee_info.get('cost'),
            'fee_currency': fee_info.get('currency'),
            'order_id': order.get('order'),
            'trade_id': order.get('id'),
        })
    return clean_trades

#print(closed_trades(exchange))
#print(log_to_csv('closed_order_test.csv', closed_trades, exchange=exchange, since = '2026-05-01T00:00:00Z'))


