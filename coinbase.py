import ccxt
import logging
import os
from cryptography.fernet import InvalidToken
from dotenv import load_dotenv

# Competition-wide, not Coinbase's. See venue_common for why these don't live
# here any more: lighter.py used to import them from this module, which made
# Coinbase a dependency of a Lighter-only sync.
from venue_common import (COMPETITION_START, QUOTE_PRIORITY, USD_EQUIVALENTS,
                          get_fernet, load_shared_markets,
                          load_shared_tickers, money_amount, resolve_since)

load_dotenv()

logger = logging.getLogger(__name__)

# ccxt's id for this venue, matching the key in venues.VENUES and the value
# stored in participant_api_keys.exchange.
EXCHANGE_ID = 'coinbase'

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
        fernet = get_fernet()
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


def verify_credential(row: dict) -> dict:
    """
    Prove a Coinbase credential works, at signup, and resolve its account
    handle. Part of the venue contract - see venues.REQUIRED_FUNCTIONS.

    `row` is a signup record with api_key, api_secret, account_type and an
    optional api_passphrase; returns {'account_type', 'portfolio_uuid',
    'passphrase'} for storage.

    A perp credential additionally resolves its INTX portfolio UUID. That
    costs one API call, paid once here rather than on every sync - and it
    doubles as the auth check, since an unauthorised key can't list
    portfolios.
    """
    account_type = (str(row.get('account_type') or 'spot')).strip().lower()
    passphrase = row.get('api_passphrase') or None
    exchange_id = row.get('exchange') or 'coinbase'

    # Plaintext here; encrypted by the caller once proven to work.
    exchange = build_exchange(
        row['api_key'],
        row.get('api_secret'),
        exchange_id,
        encrypted=False,
        passphrase=passphrase,
    )

    portfolio_uuid = None
    if account_type == 'perp':
        portfolio_uuid = find_perp_portfolio_uuid(exchange)
        if not portfolio_uuid:
            raise ValueError(
                "No perp portfolio visible to these credentials - the key may "
                "be scoped to the spot portfolio instead"
            )
    else:
        # Cheap auth check, no exchange-specific params.
        exchange.fetch_balance()

    return {'account_type': account_type, 'portfolio_uuid': portfolio_uuid,
            'passphrase': passphrase}


def build_from_credential(credential: dict) -> ccxt.Exchange:
    """
    Build an exchange from one participant_api_keys row.

    The uniform entry point every venue adapter provides, so the sync can
    construct any participant's client without knowing which exchange it is.
    Credential SHAPES differ - Coinbase needs a key/secret pair and an
    optional passphrase, Lighter needs a single token - and this is the one
    place per venue where that difference lives.
    """
    exchange = build_exchange(
        credential["api_key"],
        credential.get("api_secret"),
        credential.get("exchange") or "coinbase",
        passphrase=credential.get("api_passphrase"),
        portfolio_uuid=credential.get("portfolio_uuid"),
    )

    # Prime the market map HERE, before any other call. ccxt calls
    # load_markets() itself at the top of fetch_balance, fetch_closed_orders
    # and the rest - so priming lazily inside those functions is too late:
    # the first ccxt call of the sync has already paid for a full download.
    # Doing it at construction is what actually makes the shared cache work.
    load_shared_markets(exchange)

    return exchange


def price_balances_in_usdc(exchange: ccxt.Exchange, balances: dict = None, price_cache: dict = None) -> float:
    """
    Convert a {coin: amount} balance dict into a total USDC value.
    If balances is None, fetches the exchange's default account balance
    (i.e. exchange.fetch_balance() with no type override) and prices that.
    """

    if price_cache is None:
        price_cache = {}

    # Shared across credentials: the product list is identical for every
    # participant, and re-fetching it per credential is the single largest
    # avoidable cost in the sync.
    load_shared_markets(exchange)

    if balances is None:
        balance = exchange.fetch_balance(params={'v3': True})
        balances = balance.get('total') or {}

    markets = exchange.markets

    # One bulk price snapshot for the whole run, instead of a fetch_ticker
    # per coin per participant. Besides being ~30x fewer requests for a
    # diversified holder, it values every participant at the SAME instant -
    # per-coin calls spread a leaderboard's pricing over minutes of market
    # movement.
    tickers = load_shared_tickers(exchange)

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

            ticker = tickers.get(symbol)
            if ticker is None:
                # Not in the bulk snapshot - a thinly traded market, or the
                # bulk call failed. Ask for this one symbol rather than
                # writing the holding off at zero.
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



def find_perp_portfolio_uuid(exchange: ccxt.Exchange) -> str:
    """
    Discover the perpetuals (INTX) portfolio UUID for a set of credentials.

    Meant to be called ONCE at signup and stored in
    participant_api_keys.portfolio_uuid - it costs an extra API call, so
    don't put it in the per-run sync path.

    Returns None if the credentials can't see a perp portfolio, which is the
    normal answer for a spot-only key.
    """
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
        'total_usdc': money_amount(summary.get('total_balance') or portfolio.get('total_balance')),
        'collateral_usdc': money_amount(portfolio.get('collateral')),
        'unrealized_pnl_usdc': money_amount(summary.get('unrealized_pnl') or portfolio.get('unrealized_pnl')),
        'notional_usdc': money_amount(portfolio.get('position_notional')),
        'buying_power_usdc': money_amount(summary.get('buying_power')),
    }

# print(get_perp_account_value(exchange))


# Coinbase's account list is paged. ccxt's fetch_balance asks for one page of
# 250 and reads only that page - there is no cursor loop in it - so wallet 251
# onwards is invisible and valued at zero.
_ACCOUNTS_PAGE_LIMIT = 250

# Refuse to walk more than this many pages. A participant with 25,000 wallets
# is a bug or an attack, not a trader.
_MAX_ACCOUNT_PAGES = 100


def _wallet_totals(exchange: ccxt.Exchange, wallet_type: str) -> dict:
    """
    {coin: amount} for one wallet type, with NO silent truncation.

    ccxt's fetch_balance sends limit=250 and parses a single page. Coinbase
    creates a wallet per asset a participant has ever held or enabled, so an
    active account passes 250 easily - and everything past the first page is
    then absent from the balance with nothing to say so. That is money valued
    at zero on the leaderboard, silently, and it gets worse the more assets a
    participant trades.

    The fast path is unchanged: one fetch_balance, and for almost every
    participant `has_next` is false and we are done. Only an account that is
    actually truncated pays for the extra pages.
    """
    balance = exchange.fetch_balance(params={'type': wallet_type, 'v3': True})
    totals = dict(balance.get('total') or {})

    info = balance.get('info') or {}
    if not info.get('has_next'):
        return totals

    cursor = info.get('cursor')
    pages = 1
    while cursor and pages < _MAX_ACCOUNT_PAGES:
        response = exchange.v3PrivateGetBrokerageAccounts(
            {'limit': _ACCOUNTS_PAGE_LIMIT, 'cursor': cursor}
        )
        for account in response.get('accounts') or []:
            available = money_amount((account.get('available_balance') or {}).get('value'))
            held = money_amount((account.get('hold') or {}).get('value'))
            code = ((account.get('available_balance') or {}).get('currency')
                    or account.get('currency'))
            if not code:
                continue
            totals[code] = (totals.get(code) or 0.0) + available + held

        pages += 1
        if not response.get('has_next'):
            break
        cursor = response.get('cursor')

    logger.info("%s: %s wallet list spanned %d pages - ccxt reads only the "
                "first, so the rest were fetched explicitly",
                exchange.id, wallet_type, pages)
    return totals


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
    if account_type == 'perp':
        return get_perp_account_value(exchange, portfolio_uuid)

    # Only ask for wallet types the exchange actually has. Coinbase has no
    # options product, and 'future' resolves to the CFM (US futures) balance
    # endpoint, which PERMISSION_DENIEDs for every spot-only key - two
    # guaranteed errors per participant per run if left in.
    WALLET_TYPES = exchange.options.get('walletTypes') or ['spot']

    load_shared_markets(exchange)
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
    held_currencies: set = set()

    for wallet_type in WALLET_TYPES:
        try:
            raw_total = _wallet_totals(exchange, wallet_type)
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

        active_balances = {
            coin: amount for coin, amount in raw_total.items() if coin and amount and amount > 0
        }
        if not active_balances:
            continue

        signature = frozenset(active_balances.items())
        if signature in seen_wallet_signatures:
            continue
        seen_wallet_signatures.add(signature)

        held_currencies.update(active_balances)

        wallet_total = price_balances_in_usdc(exchange, active_balances, price_cache)
        account_totals[f'{wallet_type}_total_usdc'] = wallet_total
        account_totals['total_usdc'] += wallet_total

    # Leave the coins we just saw where _transfer_currencies can find them, so
    # get_cash_flows doesn't re-paginate every wallet to learn the same thing.
    exchange.options[_HELD_CURRENCIES_KEY] = sorted(held_currencies)

    return account_totals


def account_type_from_order(order: dict) -> str:
    """
    Best-effort venue for an order, for callers that have no credential to
    hand (closed_trades, ad hoc scripts).

    The sync does NOT rely on this - it stamps the credential's own venue,
    which is authoritative because orders are fetched with a portfolio-scoped
    key.

    Coinbase is unhelpful here: INTX perpetuals come back as
    product_type='FUTURE' with contract_expiry_type=None, indistinguishable
    from a dated future by those fields alone. ccxt's unified symbol does
    carry the distinction, so that's what decides it:

        PUMP/USDC:USDC          perpetual swap  -> 'perp'
        BTC/USD:USD-260327      dated future    -> 'future'

    Returns 'spot', 'perp', 'future', or None if nothing identifies it.
    """
    info = order.get('info') or {}
    product_type = (info.get('product_type') or '').upper()

    if product_type == 'SPOT':
        return 'spot'

    if product_type in ('FUTURE', 'PERPETUAL'):
        expiry_type = (info.get('contract_expiry_type') or '').upper()
        if product_type == 'PERPETUAL' or 'PERPETUAL' in expiry_type:
            return 'perp'

        # Fall back to the unified symbol: a settle suffix with no expiry
        # date is ccxt's notation for a perpetual swap.
        symbol = order.get('symbol') or ''
        if ':' in symbol:
            return 'future' if '-' in symbol.split(':', 1)[1] else 'perp'

        return 'future'

    return product_type.lower() or None


# A page walk can't safely be open-ended. If an exchange ignores `since` and
# re-serves the same full page, the id guard in _paginate catches it - but
# only for entries that HAVE ids. This is the backstop for the case where they
# don't. At limit=200 this allows 200,000 records, far past any real
# participant.
_MAX_PAGES = 1000


def _paginate(fetch_page, since: int, limit: int, label: str) -> list[dict]:
    """
    Walk a paginated ccxt history endpoint to the end.

    `fetch_page(since, limit)` returns one page of dicts carrying 'id' and
    'timestamp'. Advances past the newest entry seen, since ccxt sorts these
    ascending.

    This exists because a truncated history fails SILENTLY: a short page and a
    full page look identical to the caller, so a participant's first backfill
    would simply stop at the cap and nobody would learn the rest was missing.
    Orders, trades and transfers all had this shape, and only the transfer
    path had the loop guard - now they share one implementation.
    """
    entries: list[dict] = []
    cursor = since
    seen_ids: set = set()
    pages = 0

    while True:
        page = fetch_page(cursor, limit)
        if not page:
            break

        # Only dedupe entries that have an id. Treating a missing id as a
        # duplicate would break after the first page on any endpoint that
        # doesn't set one.
        fresh = [e for e in page
                 if e.get('id') is None or e.get('id') not in seen_ids]
        if not fresh:
            break
        seen_ids.update(e['id'] for e in page if e.get('id') is not None)
        entries.extend(fresh)

        if len(page) < limit:
            break

        pages += 1
        if pages >= _MAX_PAGES:
            logger.warning(
                "Stopped paginating %s after %d pages - the exchange may be "
                "ignoring `since`. History may be incomplete.", label, pages
            )
            break

        last_ts = page[-1].get('timestamp')
        if last_ts is None:
            logger.warning(
                "Last entry of a %s page has no timestamp, can't paginate "
                "further - stopping. History may be incomplete.", label
            )
            break
        cursor = last_ts + 1

    return entries


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
    request_params = {}
    if portfolio_uuid:
        request_params['retail_portfolio_id'] = portfolio_uuid

    since = resolve_since(exchange, since)

    # symbol=None returns all symbols in one call on Coinbase
    raw_orders = _paginate(
        lambda cursor, page_size: exchange.fetch_closed_orders(
            symbol=symbol, since=cursor, limit=page_size, params=request_params),
        since, limit, 'closed orders',
    )

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

def closed_trades(exchange: ccxt.Exchange, symbol: str = None, since=None,
                  limit: int = 200, portfolio_uuid: str = None) -> list[dict]:
    """
    Individual FILLS, as opposed to the orders that produced them.

    NOT USED BY THE SYNC - trade_metrics stores orders. This is here for ad
    hoc analysis where the distinction matters: one order can fill in many
    pieces at different prices, so fills are what you want for slippage or
    execution-quality work, and orders are what you want for counting trades.

    `account_type` comes from account_type_from_order(), which is best-effort
    (see its docstring). Anything writing these to the database should stamp
    the credential's venue instead, the way sync_orders does.

    Paginated and per-row guarded to match closed_orders. It previously had
    neither, so it silently returned only the first page.
    """
    request_params = {}
    if portfolio_uuid:
        request_params['retail_portfolio_id'] = portfolio_uuid

    since = resolve_since(exchange, since)

    raw_trades = _paginate(
        lambda cursor, page_size: exchange.fetch_my_trades(
            symbol=symbol, since=cursor, limit=page_size, params=request_params),
        since, limit, 'trades',
    )

    clean_trades = []
    for trade in raw_trades:
        try:
            fee_info = trade.get('fee') or {}
            clean_trades.append({
                'account_type': account_type_from_order(trade),
                'timestamp': trade.get('timestamp'),
                'datetime': trade.get('datetime'),
                'symbol': trade.get('symbol'),
                'type': trade.get('type'),
                'side': trade.get('side'),
                'price': trade.get('price'),
                'amount': trade.get('amount'),
                'fee_cost': fee_info.get('cost'),
                'fee_currency': fee_info.get('currency'),
                'order_id': trade.get('order'),
                'trade_id': trade.get('id'),
            })
        except Exception as e:
            logger.warning("Skipping malformed trade %s: %s", trade.get('id'), e)
            continue

    return clean_trades



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
    if coin in USD_EQUIVALENTS:
        return 1.0

    if price_cache is None:
        price_cache = {}

    bucket = (coin, timestamp_ms // 3_600_000)   # cache by coin and hour
    if bucket in price_cache:
        return price_cache[bucket]

    load_shared_markets(exchange)
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


# Coinbase's transfer history is reported for the whole ACCOUNT, not per
# portfolio - see the note in get_cash_flows(). A participant with both a spot
# and a perp credential therefore gets the identical list of transfers from
# each, and the sync would store every deposit twice: once under
# account_type='spot' and once under 'perp'. The unique key includes
# account_type, so nothing rejects it, and metrics.mark_internal_transfers()
# only pairs OPPOSITE directions - two identical deposits never cancel. A
# $1,000 deposit would be subtracted from the participant's returns as $2,000.
#
# The sync reads this flag and calls get_cash_flows() once per (participant,
# exchange) rather than once per credential. Lighter sets it False: its
# transfer history really is per account index, so each credential must be
# asked separately.
CASH_FLOWS_ARE_ACCOUNT_WIDE = True

# ...and which of a participant's credentials can actually read it.
#
# Not just any of them. The history lives behind the v2 transactions endpoint,
# which is reached through the ordinary Advanced Trade key. An INTX (perp) key
# sees no v2 brokerage accounts at all, so asking it returns nothing - not an
# error, just an empty result indistinguishable from "this participant has
# never deposited".
#
# Without this, the planner picks whichever credential comes first and can
# hand the job to the one credential guaranteed to fail at it. That is how
# cash flow collection stops silently: no exception, no failed step, the run
# still green, and every participant's return quietly no longer adjusted for
# funding.
CASH_FLOWS_ACCOUNT_TYPE = 'spot'


# Where get_account_totals_usdc leaves the coins it just saw, so
# _transfer_currencies doesn't have to ask for the same balance again. Stored
# on the exchange INSTANCE, which the sync builds fresh per credential per
# run - so the lifetime is exactly one credential's sync and it can never
# leak one participant's holdings into another's.
_HELD_CURRENCIES_KEY = '_setapi_held_currencies'


def _transfer_currencies(exchange: ccxt.Exchange) -> list[str]:
    """
    Currencies worth asking Coinbase about when fetching transfers.

    Coinbase's transfer history is per-currency, and it lists hundreds of
    them. Querying all of them would dominate the sync, so this narrows to
    what the participant actually holds plus the dollar currencies that
    funding almost always arrives in.

    The trade-off: a deposit of a coin that was later fully sold won't be
    seen, because it no longer shows in the balance.

    Reuses the holdings get_account_totals_usdc already fetched, if it ran
    first - which it does in the sync. fetch_balance() on Coinbase paginates
    every wallet a participant owns, so calling it twice per credential was
    the largest single cost in a run: 39 requests and 11.8s across five
    credentials, roughly half of it this duplicate.
    """
    codes = {'USD', 'USDC'}

    held = exchange.options.get(_HELD_CURRENCIES_KEY)
    if held is not None:
        codes.update(held)
        return sorted(codes)

    try:
        balance = exchange.fetch_balance()
        codes.update(
            coin for coin, amount in (balance.get('total') or {}).items()
            if coin and amount and amount > 0
        )
    except Exception as e:
        logger.warning("Could not list held currencies, checking USD/USDC only: %s", e)

    return sorted(codes)


def _account_index_is_usable(exchange: ccxt.Exchange) -> bool:
    """
    Load the account list once, and say whether per-currency transfer lookups
    can work at all.

    ccxt resolves `fetch_deposits_withdrawals(code=...)` by scanning
    exchange.accounts for that currency's wallet, via load_accounts(). That
    cache only engages when the list is NON-EMPTY:

        if self.accounts:  return self.accounts
        else:              self.accounts = self.fetch_accounts(params)

    An INTX (perp) key sees no v3 brokerage accounts, so the list comes back
    empty, nothing is cached, and EVERY currency re-downloads the whole list
    before failing to find its wallet. Measured on one live perp credential:
    33 identical /v3/brokerage/accounts requests and 11.8s - a quarter of the
    entire run - to produce nothing.

    Priming once here turns that into a single request and an early return.
    """
    try:
        exchange.load_accounts()
    except Exception as e:
        logger.warning("Could not list Coinbase accounts, skipping per-currency "
                       "transfer lookups: %s", e)
        return False

    if not exchange.accounts:
        logger.debug("%s: no v3 brokerage accounts visible to this key - "
                     "per-currency transfer lookups cannot resolve a wallet",
                     exchange.id)
        return False
    return True


def _paginate_transfers(exchange: ccxt.Exchange, code: str, since: int,
                        limit: int) -> list[dict]:
    """
    Walk a full transfer history for one currency.

    Coinbase caps a page at 100, so without pagination a participant's first
    backfill stops at the cap and silently drops the rest - wrong funding
    totals then feed straight into everyone's returns.
    """
    return _paginate(
        lambda cursor, page_size: exchange.fetch_deposits_withdrawals(
            code=code, since=cursor, limit=page_size),
        since, limit, f"transfers ({code or 'account'})",
    )


def get_cash_flows(exchange: ccxt.Exchange, since=None, limit: int = 100,
                   portfolio_uuid=None) -> list[dict]:
    """
    External deposits and withdrawals, valued in USDC at the time they moved.

    `portfolio_uuid` is accepted for interface parity with the other venue
    adapters and deliberately unused: Coinbase's v2 transactions endpoint
    reports for the whole ACCOUNT, not per portfolio, so scoping it would
    silently drop transfers rather than narrow them.

    This is what makes returns mean anything. Without it a participant who
    deposits $1,000 mid-competition shows the transfer as profit, and every
    return-based metric ranks funding rather than trading.

    Only settled transfers count - a pending or cancelled one hasn't changed
    the account's value.

    `since` accepts milliseconds or an ISO8601 string, matching closed_orders.
    """
    since = resolve_since(exchange, since)

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
        #
        # Every one of those lookups needs a wallet UUID out of the account
        # list. If that list is empty the loop cannot succeed for ANY
        # currency, and running it anyway re-downloads the list once per
        # currency - see _account_index_is_usable().
        if not _account_index_is_usable(exchange):
            return []

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


if __name__ == "__main__":
    # Ad hoc smoke test against YOUR OWN .env credentials. Never runs as part
    # of the scheduled sync, which imports this module rather than executing
    # it.
    #
    # One block, at the bottom. There used to be three, sitting between
    # function definitions - each worked only because the functions it called
    # happened to be defined above it, so adding a function in the wrong place
    # would have broken them.
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    for venue in ('spot', 'perp'):
        try:
            exchange = exchange_from_env(venue)
            totals = get_account_totals_usdc(exchange, account_type=venue)
            orders = closed_orders(exchange)
            print(f"{venue}: {totals['total_usdc']:.2f} USDC, {len(orders)} closed order(s)")
        except Exception as e:
            # Expected when only one venue's keys are in .env
            print(f"{venue}: {type(e).__name__}: {e}")
