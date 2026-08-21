import ccxt
import logging
import os
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from dotenv import load_dotenv


load_dotenv()

# Your own keys, by venue. Coinbase issues separate credentials for spot and
# perps, so each venue reads its own pair. Participants' keys never come from
# here - those are decrypted out of Supabase by build_exchange().
ENV_CREDENTIALS = {
    'spot': ('COINBASE_APIKEY', 'COINBASE_SECRET'),
    'perp': ('COINBASEPERP_APIKEY', 'COINBASEPERP_SECRET'),
}
# Deliberately only YOUR two venues. Other participants' keys belong in
# participant_api_keys, Fernet-encrypted - putting them in .env would keep
# live credentials in plaintext on disk, outside the encryption everything
# else depends on. To test another participant's keys ad hoc, pass them
# straight to build_exchange(key, secret, encrypted=False).

# Stablecoins and fiat-pegged currencies treated as 1:1 with USD
USD_EQUIVALENTS = {
    'USDC', 'USDT', 'USD', 'BUSD', 'DAI', 'TUSD', 'USDP', 'GUSD',
    'FDUSD', 'USDD', 'FRAX', 'LUSD', 'SUSD', 'USDN', 'USDJ', 'MAMUSD',
}
# Preferred quote currencies to try, in order
QUOTE_PRIORITY = ['USDC', 'USDT', 'USD', 'BUSD', 'FDUSD']

# When the competition starts. Everything that reads history - orders, trades,
# cash flows - falls back to this when no `since` is given, so it decides what
# counts as in-competition activity.
#
# One place, not three: with the date inlined per function, moving the start
# and missing one would give you flows from a different window than orders,
# and the returns computed from them would be quietly wrong.
#
# Override with COMPETITION_START in the environment to change it without a
# redeploy - e.g. COMPETITION_START=2020-01-01T00:00:00Z to pull full history.
COMPETITION_START = os.getenv("COMPETITION_START", "2026-01-01T00:00:00Z")

_fernet: Fernet = None


def exchange_from_env(account_type: str = 'spot') -> ccxt.Exchange:
    """
    Build an exchange from your own .env keys - for local testing and ad hoc
    scripts only. The scheduled sync builds participants' exchanges with
    build_exchange() instead.

    'perp' additionally reads COINBASEPERP_PORTFOLIO if set; without it the
    perp functions discover the portfolio UUID on first use.
    """
    if account_type not in ENV_CREDENTIALS:
        raise ValueError(
            f"Unknown account_type '{account_type}' - expected one of "
            f"{sorted(ENV_CREDENTIALS)}"
        )

    key_var, secret_var = ENV_CREDENTIALS[account_type]
    api_key, api_secret = os.getenv(key_var), os.getenv(secret_var)

    if not api_key or not api_secret:
        raise RuntimeError(
            f"{key_var} and {secret_var} must both be set in .env to use the "
            f"{account_type} account"
        )

    return build_exchange(
        api_key,
        api_secret,
        'coinbase',
        encrypted=False,  # .env keys are plaintext, unlike the Supabase ones
        portfolio_uuid=(os.getenv("COINBASEPERP_PORTFOLIO")
                        if account_type == 'perp' else None),
    )

def _get_fernet() -> MultiFernet:
    """
    Lazily build the cipher so importing this module doesn't require
    FERNET_KEY to be set (only the credential-decrypting paths need it).

    Returns a MultiFernet, not a plain Fernet, so keys can be ROTATED:

      FERNET_KEY           the current key. Everything is encrypted with this.
      FERNET_KEYS_RETIRED  comma-separated older keys, decrypt-only.

    MultiFernet encrypts with the first key and decrypts with any of them, so
    a rotation doesn't strand existing rows. Without this, changing
    FERNET_KEY orphans every stored credential at once and all 100
    participants have to issue new exchange keys - which is why a leaked key
    would otherwise be unrecoverable rather than merely urgent.

    Rotation: put the new key in FERNET_KEY, move the old one to
    FERNET_KEYS_RETIRED, run rotate_credentials.py, then drop the retired
    entry once it reports everything re-encrypted.
    """
    global _fernet # sets _fernet as a global variable
    if _fernet is None:
        key = os.getenv("FERNET_KEY")
        if not key:
            raise RuntimeError(
                "FERNET_KEY is not set - cannot decrypt participant credentials"
            )

        keys = [Fernet(key.strip().encode())]
        retired = os.getenv("FERNET_KEYS_RETIRED", "")
        keys.extend(
            Fernet(k.strip().encode()) for k in retired.split(",") if k.strip()
        )

        _fernet = MultiFernet(keys)
    return _fernet


def build_exchange(api_key: str, api_secret: str, exchange_id: str = 'coinbase',
                   encrypted: bool = True, passphrase: str = None,
                   portfolio_uuid: str = None, **options) -> ccxt.Exchange:
    """
    Build a ccxt exchange instance for ONE of a participant's venues.

    Spot and perps use different Coinbase API keys, so a participant has one
    `participant_api_keys` row per venue and this is called once per row -
    never reuse an instance built from spot keys to reach perp endpoints.

    Credentials in `participant_api_keys` are Fernet-encrypted by sign_ups.py,
    so `encrypted` defaults to True - that's the pipeline path. Pass
    encrypted=False for plaintext keys (e.g. your own from .env).

    `exchange_id` must match ccxt's exact id (see ccxt.exchanges), which is
    what the `exchange` column of participant_api_keys stores.

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
    print(price_balances_in_usdc(exchange_from_env('spot')))


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

    # Match on TYPE, not name. A user can call any portfolio anything - one
    # test account here has an ordinary CONSUMER portfolio named "perps2",
    # which a name-based match happily returns. The INTX endpoints would then
    # be called with a non-INTX portfolio UUID and fail, or worse, quietly
    # value the wrong account.
    for portfolio in portfolios:
        if (portfolio.get('type') or '').upper() == 'INTX':
            return portfolio.get('id')

    logger.warning(
        "No INTX portfolio found for this key - portfolios seen: %s",
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
    print(get_account_totals_usdc(exchange_from_env('spot'), account_type='spot'))
    print(get_account_totals_usdc(exchange_from_env('perp'), account_type='perp'))


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
    normalized to milliseconds internally. Defaults to COMPETITION_START
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
        since = exchange.parse8601(COMPETITION_START)
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
        since_ms = exchange.parse8601(COMPETITION_START)

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



def _price_at_usdc(exchange: ccxt.Exchange, coin: str, timestamp_ms: int,
                   price_cache: dict = None) -> float:
    """
    Price of `coin` in USDC AT a past moment, for valuing a historical
    transfer.

    Valuing an old deposit at today's price is the obvious shortcut and it's
    wrong: a BTC deposit made at 30k would be booked at today's price, and the
    difference lands in the participant's return as if they had traded it.

    Falls back to the current ticker only if no historical candle is
    available, and says so in the log.
    """
    logger = logging.getLogger(__name__)

    if coin in USD_EQUIVALENTS:
        return 1.0

    if price_cache is None:
        price_cache = {}

    bucket = (coin, timestamp_ms // 3_600_000)   # cache by coin and hour
    if bucket in price_cache:
        return price_cache[bucket]

    exchange.load_markets()
    for quote in QUOTE_PRIORITY:
        symbol = f"{coin}/{quote}"
        if symbol not in exchange.markets:
            continue
        try:
            candles = exchange.fetch_ohlcv(symbol, '1h', since=timestamp_ms, limit=1)
            if candles:
                price = float(candles[0][4])          # close
                price_cache[bucket] = price
                return price
        except Exception as e:
            logger.debug("OHLCV unavailable for %s at %s: %s", symbol, timestamp_ms, e)

        try:
            ticker = exchange.fetch_ticker(symbol)
            price = float(ticker.get('last') or ticker.get('close') or 0)
            if price:
                logger.warning(
                    "No historical candle for %s at %s - valuing at the CURRENT "
                    "price, which overstates or understates this transfer",
                    symbol, timestamp_ms
                )
                price_cache[bucket] = price
                return price
        except Exception as e:
            logger.warning("Could not price %s: %s", symbol, e)

    logger.warning("No market found to price %s - transfer valued at 0", coin)
    price_cache[bucket] = 0.0
    return 0.0


# Coinbase's v2 transactions endpoint returns EVERYTHING that moved value,
# and ccxt labels much of it 'deposit'/'withdrawal' - including
# 'advanced_trade_fill', which is a TRADE. Counting those as cash flows would
# subtract every trade from a participant's return and flatten performance to
# roughly zero.
#
# So this is an allowlist, never a denylist: an unrecognised type is logged
# and ignored, because wrongly counting a trade as funding is far more
# damaging than missing an unusual transfer.
EXTERNAL_TRANSFER_TYPES = {
    'send',                 # crypto sent to / received from outside
    'fiat_deposit',         # bank transfer in
    'fiat_withdrawal',      # bank transfer out
    'pro_deposit',          # moved in from Coinbase Pro / Advanced
    'pro_withdrawal',       # moved out to Coinbase Pro / Advanced
    'exchange_deposit',
    'exchange_withdrawal',
}

# Recognised but deliberately NOT flows: value moving within the account
# rather than in or out of it.
INTERNAL_ACTIVITY_TYPES = {
    'advanced_trade_fill',  # a trade
    'buy', 'sell',          # fiat<->crypto conversion inside the account
    'trade',
    # Spot <-> INTX perp portfolio. Both venues are one competition, so
    # moving collateral between them changes nothing about a participant's
    # standing. Excluded here rather than left to the netting heuristic in
    # metrics, because Coinbase reports only the spot leg - there is no
    # matching perp-side record for the netting to pair it with.
    'intx_deposit',
    'intx_withdrawal',
    # Coinbase converting one asset into another in place (a token contract
    # migration or rename). Value carries across, so nothing entered or left
    # the account.
    'asset_migration',
}


def _transfer_currencies(exchange: ccxt.Exchange) -> list[str]:
    """
    Currencies worth asking Coinbase about when fetching transfers.

    Coinbase's transfer history is per-currency, and it lists hundreds of
    them. Querying all of them would dominate the sync, so this narrows to
    what the participant actually holds plus the dollar currencies that
    funding almost always arrives in.

    The trade-off: a deposit of a coin that was later fully sold won't be
    seen, because it no longer shows in the balance.
    """
    logger = logging.getLogger(__name__)
    codes = {'USD', 'USDC'}

    try:
        balance = exchange.fetch_balance()
        codes.update(
            coin for coin, amount in (balance.get('total') or {}).items()
            if coin and amount and amount > 0
        )
    except Exception as e:
        logger.warning("Could not list held currencies, checking USD/USDC only: %s", e)

    return sorted(codes)


def _paginate_transfers(exchange: ccxt.Exchange, code: str, since: int,
                        limit: int) -> list[dict]:
    """
    Walk a full transfer history, one page at a time.

    Coinbase caps a page at 100. Without this, a participant's first backfill
    would stop at the cap and silently drop the rest - and it fails silently,
    because a short page and a full page look identical to the caller. Wrong
    funding totals then feed straight into everyone's returns.

    Mirrors the pagination in closed_orders: advance past the newest entry
    seen, since ccxt sorts these ascending by timestamp.
    """
    logger = logging.getLogger(__name__)

    entries = []
    cursor = since
    seen_ids = set()

    while True:
        page = exchange.fetch_deposits_withdrawals(code=code, since=cursor, limit=limit)
        if not page:
            break

        # Guard against an exchange that ignores `since` and re-serves the
        # same page, which would otherwise loop forever.
        fresh = [e for e in page if e.get('id') not in seen_ids]
        if not fresh:
            break
        seen_ids.update(e.get('id') for e in fresh)
        entries.extend(fresh)

        if len(page) < limit:
            break

        last_ts = page[-1].get('timestamp')
        if last_ts is None:
            logger.warning("Transfer page has no timestamp on its last entry - "
                           "stopping pagination for %s", code or 'account')
            break
        cursor = last_ts + 1

    return entries


def get_cash_flows(exchange: ccxt.Exchange, since=None, limit: int = 100) -> list[dict]:
    """
    External deposits and withdrawals, valued in USDC at the time they moved.

    This is what makes returns mean anything. Without it a participant who
    deposits $1,000 mid-competition shows the transfer as profit, and every
    return-based metric ranks funding rather than trading.

    Only settled transfers count - a pending or cancelled one hasn't changed
    the account's value.

    `since` accepts milliseconds or an ISO8601 string, matching closed_orders.
    """
    logger = logging.getLogger(__name__)

    if since is None:
        since = exchange.parse8601(COMPETITION_START)
    elif isinstance(since, str):
        since = exchange.parse8601(since)

    try:
        raw = _paginate_transfers(exchange, None, since, limit)
    except ccxt.NotSupported:
        logger.warning("%s does not support fetching transfers - returns will "
                       "not be adjusted for deposits", exchange.id)
        return []
    except ccxt.ArgumentsRequired:
        # Coinbase answers per-currency rather than for the whole account.
        # Iterating every currency it lists would be hundreds of calls per
        # participant per run, so ask only about currencies they actually
        # hold, plus the dollar ones nearly all funding arrives in.
        raw = []
        for code in _transfer_currencies(exchange):
            try:
                raw.extend(_paginate_transfers(exchange, code, since, limit))
            except Exception as e:
                logger.debug("No transfer history for %s: %s", code, e)
                continue

    price_cache: dict = {}
    flows = []

    for entry in raw:
        try:
            if entry.get('status') != 'ok':
                continue                       # pending/cancelled moved nothing

            raw_type = (entry.get('info') or {}).get('type')
            if raw_type in INTERNAL_ACTIVITY_TYPES:
                continue                       # a trade, not funding
            if raw_type not in EXTERNAL_TRANSFER_TYPES:
                logger.warning(
                    "Ignoring transfer %s of unrecognised type '%s' - add it to "
                    "EXTERNAL_TRANSFER_TYPES if it moves money in or out of the "
                    "account", entry.get('id'), raw_type
                )
                continue

            amount = entry.get('amount')
            currency = entry.get('currency')
            timestamp = entry.get('timestamp')
            if not amount or not currency or timestamp is None:
                logger.warning("Skipping transfer %s: incomplete", entry.get('id'))
                continue

            direction = 'in' if entry.get('type') == 'deposit' else 'out'
            price = _price_at_usdc(exchange, currency, timestamp, price_cache)

            flows.append({
                'participant_id': None,        # populated by the sync
                'account_type': None,          # populated by the sync
                'timestamp': timestamp,
                'datetime': entry.get('datetime'),
                'direction': direction,
                'currency': currency,
                'amount': float(amount),
                # Signed: deposits add, withdrawals subtract. Lets the metrics
                # layer simply sum a period's flows.
                'usdc_value': float(amount) * price * (1 if direction == 'in' else -1),
                'transfer_id': entry.get('id'),
                'raw_type': (entry.get('info') or {}).get('type'),
            })

        except Exception as e:
            logger.warning("Skipping malformed transfer %s: %s", entry.get('id'), e)
            continue

    return flows
