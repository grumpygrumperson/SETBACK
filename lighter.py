"""
Lighter venue adapter - the same surface as coinbase.py, for the Lighter DEX.

Mirrors coinbase.py so post_to_supabase can treat the two interchangeably:
build_exchange, get_account_totals_usdc, closed_orders, closed_trades and
get_cash_flows all take and return the same shapes.

WHERE LIGHTER GENUINELY DIFFERS, AND WHY THE CODE BELOW LOOKS ODD IN PLACES
--------------------------------------------------------------------------
Everything here was read off ccxt's lighter.py implementation, not guessed.
Four differences shape this whole module:

1. PERPS ONLY. ccxt reports has['spot'] = False, has['swap'] = True. There is
   no Lighter spot account, so account_type is always 'perp'. Asking for
   'spot' raises rather than quietly returning an empty balance that would
   read as a participant with no money.

2. TWO WAYS TO AUTHENTICATE, and the right one for a competition is the
   less obvious one.

   ccxt's requiredCredentials is {'privateKey': True} - it expects the L1
   WALLET private key. That works, but a wallet private key is unscoped and
   irrevocable: it controls every asset in the wallet on every chain, cannot
   be made read-only, and "rotating" it means moving all funds elsewhere.
   Collecting one per participant would make this service custodian of every
   competitor's wallet.

   Lighter also issues a READ-ONLY TOKEN, and ccxt will use one even though
   it doesn't advertise it. Private endpoints are signed by create_auth(),
   which returns a cached token whenever one is present and unexpired - and
   load_account() returns None rather than raising when no private key is
   set, so nothing on that path ever consults a key. Injecting a token into
   options['auths'] therefore authenticates fully. Verified against the live
   API: fetch_balance and fetch_my_trades both succeed with privateKey=None.

   Read-only tokens cannot move funds and expire on their own, so this
   module prefers them and treats the private key as the fallback.

3. `since` IS NOT SENT TO THE SERVER. Both fetch_closed_orders and
   fetch_my_trades accept `since` but only use it to filter client-side after
   the response comes back. Resuming a sync by timestamp therefore cannot
   work the way it does on Coinbase, and coinbase._paginate - which walks
   history by advancing `since` - would re-request page one forever. Lighter
   paginates by CURSOR instead, which ccxt implements itself behind
   params={'paginate': True}. That is what this module uses.

4. fetch_closed_orders REQUIRES A SYMBOL and caps at 100 per market, so
   fetching a participant's history means one call per market and still only
   returns their most recent 100 orders in each. fetch_my_trades takes no
   symbol and paginates properly, so closed_orders() here is built by
   aggregating fills into orders. See its docstring.

VERIFIED AGAINST A LIVE ACCOUNT
-------------------------------
Read-only token auth, balance, fills, order aggregation, cash flows and the
fee tier have all run against a funded Lighter account holding an open
position. The equity figure was confirmed there: with a position open,
total_asset_value (14.991) = collateral (15.000) + unrealized_pnl (-0.009),
so it is net liquidation value and collateral alone would have understated
the loss.

Still unconfirmed: fee_cost. Lighter's trade payload carries no fee field,
and this account is Standard tier, which pays nothing. A Premium account
pays fees that appear in neither the fill nor the market data - see
get_account_fee_tier().
"""

import logging
import os
import time
import ccxt
from cryptography.fernet import InvalidToken
from dotenv import load_dotenv
from venue_common import (USD_EQUIVALENTS, get_fernet, load_shared_markets,
                          money_amount, resolve_since)

load_dotenv()

logger = logging.getLogger(__name__)

# Your own Lighter key, for local testing. Participants' keys live encrypted
# in participant_api_keys, exactly as with Coinbase.
ENV_CREDENTIALS = {
    'perp': 'LIGHTER_READONLY_TOKEN',
}

# ccxt's id for this venue. Stored in participant_api_keys.exchange.
EXCHANGE_ID = 'lighter'

# Lighter settles everything in USDC and has no spot product, so a
# participant's account_type is always this.
ACCOUNT_TYPE = 'perp'

# Whether one credential's transfer history covers the whole account or only
# that credential's slice of it. False here: Lighter's deposit and withdrawal
# endpoints are scoped to an account index, so every credential has to be
# asked separately or its transfers are simply never seen.
#
# Coinbase sets this True - its v2 transactions endpoint answers for the
# entire account whichever portfolio's key you ask with, so asking twice
# returns the same deposits twice. See the note on
# coinbase.CASH_FLOWS_ARE_ACCOUNT_WIDE.
CASH_FLOWS_ARE_ACCOUNT_WIDE = False

# ccxt raises NotSupported above this, and the message is unhelpful about
# why. Checked here so a mis-pasted key fails at registration with something
# a participant can act on.
_MAX_PRIVATE_KEY_LENGTH = 66

# 0x + 40 hex. Used only to tell an address apart from a key - see
# build_exchange.
_ADDRESS_LENGTH = 42

# Lighter's read-only tokens are 'ro:<account_index>:<scope>:<deadline>:<sig>'
# - the account index and expiry come free, so a token needs no extra lookup
# to be usable.
_TOKEN_PREFIX = 'ro:'
_TOKEN_SEGMENTS = 5

# Warn once a token has under a fortnight left. A participant whose token
# lapses mid-competition simply stops reporting, and by the time that shows
# up as missing snapshots the equity curve already has a hole in it.
_TOKEN_EXPIRY_WARNING_SECONDS = 14 * 86400

# ccxt validates apiKeyIndex into 4..254 and rejects 0. On the token path it
# is never sent to Lighter - it only keys ccxt's internal auth cache - so any
# valid value works and this is simply the lowest one ccxt accepts.
_DEFAULT_API_KEY_INDEX = 4


def parse_readonly_token(token: str) -> dict:
    """
    Pull the account index and expiry out of a Lighter read-only token.

    Format: ro:<account_index>:<scope>:<deadline>:<signature>

    The account index is what Coinbase's portfolio_uuid is for - the thing
    that says WHICH account to read - and it travels inside the token, so a
    token needs no signup-time lookup at all. The deadline is a Unix
    timestamp in seconds; ccxt compares against it before reusing a cached
    token, so it has to be an int, not the string it arrives as.
    """
    parts = (token or '').split(':')
    if len(parts) != _TOKEN_SEGMENTS or parts[0] != 'ro':
        raise ValueError(
            "Not a Lighter read-only token - expected five colon-separated "
            f"parts beginning 'ro:', got {len(parts)}"
        )

    _, account_index, scope, deadline, signature = parts

    if not account_index.isdigit() or not deadline.isdigit():
        raise ValueError(
            "Malformed Lighter read-only token - the account index and "
            "deadline should both be numeric"
        )

    return {
        'account_index': int(account_index),
        'scope': scope,
        'deadline': int(deadline),
        'signature': signature,
    }


def build_exchange_from_token(token: str, encrypted: bool = True,
                              api_key_index: int = _DEFAULT_API_KEY_INDEX,
                              **options) -> ccxt.Exchange:
    """
    Build a Lighter instance authenticated by a READ-ONLY TOKEN.

    Preferred over the private-key path: a read-only token cannot move funds
    and expires by itself, so collecting one per participant carries none of
    the custody risk that collecting wallet keys does.

    ccxt has no public API for this - it always expects to mint its own
    tokens from a signer - so the token is written straight into the internal
    options['auths'] cache that create_auth() reads. That is a private
    structure and a ccxt upgrade could reshape it; the smoke test at the
    bottom of this file is what would notice.

    Raises if the token has already expired, rather than letting the sync
    fail later with an opaque 'invalid auth string' from Lighter.
    """
    if not token:
        raise ValueError("A token is required")

    if encrypted:
        try:
            token = get_fernet().decrypt(token.encode()).decode()
        except InvalidToken as e:
            raise ValueError(
                "Could not decrypt the Lighter token - wrong FERNET_KEY, or "
                "the stored value isn't encrypted (try encrypted=False)"
            ) from e

    token = token.strip()
    details = parse_readonly_token(token)

    remaining = details['deadline'] - int(time.time())
    if remaining <= 0:
        raise ValueError(
            f"This Lighter read-only token expired "
            f"{abs(remaining) // 86400} day(s) ago. Generate a new one."
        )
    if remaining < _TOKEN_EXPIRY_WARNING_SECONDS:
        logger.warning(
            "Lighter read-only token for account %s expires in %.1f day(s) - "
            "the sync will start failing for this participant after that",
            details['account_index'], remaining / 86400,
        )

    account_index = details['account_index']

    exchange = ccxt.lighter({'enableRateLimit': True, **options})

    # Both are read back out of options by create_auth() when the caller
    # doesn't pass them, which is the case on every fetch_* path here.
    exchange.options['accountIndex'] = account_index
    exchange.options['apiKeyIndex'] = int(api_key_index)

    # The cache create_auth() consults. With a valid deadline it returns
    # `token` verbatim and never reaches the signing code, which is what lets
    # this work with no private key at all.
    exchange.options.setdefault('auths', {}).setdefault(str(account_index), {})[
        str(api_key_index)
    ] = {
        'signer': None,
        'lighterPrivateKey': None,
        'deadline': details['deadline'],
        'token': token,
    }

    return exchange


def _check_token_scope(details: dict) -> None:
    """
    Look at the `scope` segment of a read-only token.

    parse_readonly_token has always extracted this field and nothing has ever
    read it. That is worth fixing, but carefully: Lighter's scope vocabulary
    is NOT documented anywhere I can verify, and the only value observed so
    far is 'all'. Enforcing an allowlist built on one observation would reject
    valid tokens.

    So this warns by default and enforces only when asked. Run a signup round,
    read the warnings to learn what Lighter actually issues, put the real
    values in LIGHTER_ALLOWED_SCOPES, then set LIGHTER_ENFORCE_SCOPE=1.

    This is defence in depth, not the load-bearing control. The 'ro:' prefix
    is what actually guarantees the credential cannot trade - Lighter mints
    these tokens and a read-only one has no power to sign an order, whatever
    its scope says.
    """
    scope = (details.get('scope') or '').strip()
    allowed = {s.strip() for s in
               os.getenv("LIGHTER_ALLOWED_SCOPES", "all").split(",") if s.strip()}

    if scope in allowed:
        return

    enforce = os.getenv("LIGHTER_ENFORCE_SCOPE", "").strip().lower() in ("1", "true", "yes")
    if enforce:
        raise ValueError(
            f"Lighter token scope '{scope}' is not in the allowed set "
            f"{sorted(allowed)}. Set LIGHTER_ALLOWED_SCOPES if this scope is "
            f"legitimate."
        )

    logger.warning(
        "Lighter token for account %s has unrecognised scope %r (allowed: %s). "
        "Accepted, because the scope vocabulary is unconfirmed - add it to "
        "LIGHTER_ALLOWED_SCOPES once you know it is read-only.",
        details.get('account_index'), scope, sorted(allowed),
    )


def _refuse_wallet_key() -> None:
    """
    Refuse to authenticate Lighter with an L1 wallet private key.

    A read-only token is scoped and expiring; a wallet private key is neither.
    It controls every asset in that wallet on every chain, it cannot be made
    read-only, and it cannot be revoked - "rotating" it means moving all the
    funds somewhere else. Accepting one makes this service the custodian of a
    competitor's entire wallet, permanently, for a competition that only ever
    needed to READ their balance.

    That is the one mistake here that cannot be undone afterwards, which is
    why the check is a hard refusal rather than a warning.

    It lives in build_exchange rather than only in verify_credential on
    purpose: signup-time validation protects new registrations, but a wallet
    key already sitting in participant_api_keys would otherwise keep working
    forever. Refusing on the path every credential actually goes through
    covers both.

    ALLOW_WALLET_KEYS exists for exchange_from_env(), where the operator is
    testing against their OWN wallet from their own machine. It must never be
    set on the deployed service.
    """
    if os.getenv("ALLOW_WALLET_KEYS", "").strip().lower() in ("1", "true", "yes"):
        logger.warning(
            "ALLOW_WALLET_KEYS is set - authenticating Lighter with a wallet "
            "private key. This must never be set on the deployed service."
        )
        return

    raise ValueError(
        "Refusing to use a Lighter WALLET PRIVATE KEY. This competition only "
        "reads balances, so it accepts read-only tokens only - a token starts "
        "'ro:', cannot move funds, and expires on its own. A wallet private "
        "key controls every asset in the wallet on every chain and cannot be "
        "revoked. Generate a read-only token in the Lighter UI and register "
        "that instead."
    )


def build_exchange(private_key: str, encrypted: bool = True,
                   account_index=None, api_key_index=None,
                   **options) -> ccxt.Exchange:
    """
    Build a ccxt Lighter instance for one participant.

    Deliberately NOT compatible with coinbase.build_exchange's signature:
    Lighter authenticates with an L1 private key and has no api key or
    secret, so pretending the two take the same arguments would only hide
    that. post_to_supabase dispatches on the credential's `exchange` column.

    `private_key` is Fernet-encrypted in participant_api_keys, so `encrypted`
    defaults to True. Pass encrypted=False for a plaintext key from .env.

    `account_index` scopes the instance to one Lighter sub-account. ccxt can
    derive it from the private key, but that costs an extra public call on
    every use - find_account_index() resolves it once at signup so the sync
    doesn't pay for it every run. Same pattern as Coinbase's portfolio_uuid.

    A READ-ONLY TOKEN passed here is routed to build_exchange_from_token()
    instead. Both credential types live in the same column, so the sync
    doesn't need to know which a participant registered - and a token is the
    better answer for everyone, since it can't move funds.

    THE WALLET-KEY PATH IS REFUSED unless ALLOW_WALLET_KEYS is set. See
    _refuse_wallet_key() for why, and why the check lives here rather than
    only at signup.
    """
    if not private_key:
        raise ValueError("A private_key is required")

    # Detected before decryption too - a stored token arrives Fernet-encrypted
    # and won't show its prefix until build_exchange_from_token decrypts it.
    if not encrypted and private_key.strip().startswith(_TOKEN_PREFIX):
        return build_exchange_from_token(
            private_key, encrypted=False,
            api_key_index=api_key_index or _DEFAULT_API_KEY_INDEX, **options)

    if encrypted:
        try:
            probe = get_fernet().decrypt(private_key.encode()).decode()
        except InvalidToken:
            probe = ''
        if probe.strip().startswith(_TOKEN_PREFIX):
            return build_exchange_from_token(
                probe, encrypted=False,
                api_key_index=api_key_index or _DEFAULT_API_KEY_INDEX, **options)

    # Everything below this line is the WALLET PRIVATE KEY path. Anything that
    # reaches it is not a read-only token, so it is refused by default.
    _refuse_wallet_key()

    if encrypted:
        try:
            private_key = get_fernet().decrypt(private_key.encode()).decode()
        except InvalidToken as e:
            raise ValueError(
                "Could not decrypt the Lighter private key - wrong FERNET_KEY, "
                "or the stored value isn't encrypted (try encrypted=False)"
            ) from e

    private_key = private_key.strip()

    # ccxt raises NotSupported for anything longer, pointing at its FAQ. Say
    # the actionable thing instead: it wants the L1 key, not an API key blob.
    if len(private_key) > _MAX_PRIVATE_KEY_LENGTH:
        raise ValueError(
            f"Lighter private key is {len(private_key)} characters; ccxt "
            f"expects the L1 private key (at most "
            f"{_MAX_PRIVATE_KEY_LENGTH}). A Lighter API-key blob is not the "
            f"same thing and will not authenticate."
        )

    # An address is 0x + 40 hex; a private key is 0x + 64 hex. Pasting the
    # address is an easy mistake and a nasty one to diagnose: it's short
    # enough to pass the check above, ccxt then derives a DIFFERENT address
    # from it, and Lighter answers "account not found" - the exact error a
    # real-but-unfunded wallet gives. Caught here so the two stay
    # distinguishable.
    if len(private_key) == _ADDRESS_LENGTH and private_key.startswith('0x'):
        raise ValueError(
            "That looks like a wallet ADDRESS, not a private key. An address "
            "is public and cannot sign; ccxt derives it from the private key "
            "instead. Export the private key of the wallet that owns the "
            "Lighter account (0x + 64 hex characters)."
        )

    config = {
        'privateKey': private_key,
        'enableRateLimit': True,
        **options,
    }

    exchange = ccxt.lighter(config)

    # ccxt reads both out of options when the caller doesn't pass them.
    if account_index is not None:
        exchange.options['accountIndex'] = account_index
    if api_key_index is not None:
        exchange.options['apiKeyIndex'] = api_key_index

    return exchange


def verify_credential(row: dict) -> dict:
    """
    Prove a Lighter credential works, at signup, and resolve its account
    handle. Part of the venue contract - see venues.REQUIRED_FUNCTIONS.

    `row` is a signup record with at least api_key and account_type; returns
    {'account_type', 'portfolio_uuid', 'passphrase'} for storage.

    Four Lighter-specific checks live here rather than in the importer,
    because they are facts about this venue and nothing else:

      - READ-ONLY TOKENS ONLY. Rejected here, at the point where a human is
        still watching and the participant can still fix it, rather than
        leaving build_exchange to refuse it later in an unattended cron.
      - PERPS ONLY, so a row registered as 'spot' is a mistake worth catching
        while the participant can still fix it
      - the whole credential is one value in api_key; there is no secret
      - the account index is inside the read-only token, so resolving it
        costs no API call

    The live fetch_balance() is what separates "the token parses" from "the
    token authenticates".
    """
    account_type = (str(row.get('account_type') or ACCOUNT_TYPE)).strip().lower()

    if account_type != ACCOUNT_TYPE:
        raise ValueError(
            f"Lighter has no '{account_type}' market - register this "
            f"credential as '{ACCOUNT_TYPE}'"
        )

    credential = str(row['api_key']).strip()

    # Refuse a wallet private key BEFORE building anything with it. Note this
    # rejects on the shape of the value, not on whether it works: a wallet key
    # authenticates perfectly well, which is exactly the problem.
    if not credential.startswith(_TOKEN_PREFIX):
        raise ValueError(
            f"This is not a Lighter read-only token. A token starts "
            f"'{_TOKEN_PREFIX}' and cannot move funds; anything else is "
            f"treated as a wallet private key, which controls every asset in "
            f"the wallet on every chain and will not be accepted. Generate a "
            f"read-only token in the Lighter UI and submit that."
        )

    # Parses the token and proves it is well-formed and unexpired. The account
    # index travels INSIDE the token, so this replaces the find_account_index()
    # call that used to be here - one fewer API request per registration, and
    # one fewer way for signup to fail on a network blip.
    details = parse_readonly_token(credential)
    _check_token_scope(details)

    # Plaintext here; encrypted by the caller once proven to work.
    exchange = build_exchange_from_token(credential, encrypted=False)
    account_index = details['account_index']

    exchange.fetch_balance()

    # Standard accounts trade free; Premium accounts pay maker/taker fees that
    # appear NOWHERE in the fill or the market data. Their gross performance
    # would be overstated against Coinbase traders, whose fees ARE recorded -
    # so flag it while someone is watching.
    try:
        fees = get_account_fee_tier(exchange, account_index)
        if not fees.get('fees_are_zero'):
            logger.warning(
                "Lighter account %s is tier '%s' (taker tick %s, maker tick "
                "%s) - it pays fees Lighter does not report per trade, so this "
                "participant's costs will be missing from their returns",
                account_index, fees.get('user_tier'),
                fees.get('taker_fee_tick'), fees.get('maker_fee_tick'),
            )
    except Exception as e:
        # Informational only - never block a registration over it.
        logger.warning("Could not read Lighter fee tier for %s: %s",
                       account_index, e)

    return {
        'account_type': ACCOUNT_TYPE,
        # The venue's account handle, same column as Coinbase's portfolio
        # UUID. The column is text; the index is an int.
        'portfolio_uuid': str(account_index),
        'passphrase': None,
    }


def build_from_credential(credential: dict) -> ccxt.Exchange:
    """
    Build an exchange from one participant_api_keys row.

    The uniform entry point every venue adapter provides. Lighter's credential
    shape differs from Coinbase's in two ways this absorbs:

      api_key         holds the WHOLE credential - a read-only token, or an
                      L1 private key. api_secret is null; there is no pair.
      portfolio_uuid  holds the account index, an integer rather than a UUID.
                      Ignored on the token path, where the index travels
                      inside the token itself.
    """
    account_index = credential.get("portfolio_uuid")

    exchange = build_exchange(
        credential["api_key"],
        encrypted=True,
        account_index=account_index,
    )

    # Before any other call - ccxt loads markets itself at the top of
    # fetch_balance and fetch_my_trades, so priming later is too late to
    # save anything. Lighter lists ~241 markets; sharing them across
    # participants makes every credential after the first free.
    load_shared_markets(exchange)

    return exchange


def exchange_from_env() -> ccxt.Exchange:
    """
    Build a Lighter exchange from your own .env credential, for local
    testing. Mirrors coinbase.exchange_from_env.

    Prefers LIGHTER_READONLY_TOKEN and falls back to LIGHTER_PRIVATEKEY, so
    a machine holding both uses the one that can't move money.
    """
    token = os.getenv(ENV_CREDENTIALS[ACCOUNT_TYPE])
    if token:
        return build_exchange_from_token(token, encrypted=False)

    private_key = os.getenv("LIGHTER_PRIVATEKEY")
    if not private_key:
        raise RuntimeError(
            f"Set {ENV_CREDENTIALS[ACCOUNT_TYPE]} (preferred - read-only, "
            f"cannot move funds) or LIGHTER_PRIVATEKEY in .env to use Lighter"
        )

    return build_exchange(
        private_key,
        encrypted=False,                 # .env keys are plaintext
        account_index=os.getenv("LIGHTER_ACCOUNT_INDEX"),
        api_key_index=os.getenv("LIGHTER_APIKEY_INDEX"),
    )


def find_account_index(exchange: ccxt.Exchange) -> str:
    """
    Discover the Lighter sub-account index for a set of credentials.

    The analogue of coinbase.find_perp_portfolio_uuid: call it ONCE at signup
    and store the result in participant_api_keys.portfolio_uuid, so the sync
    never pays for the lookup.

    Returns None if the key has no account, which is the normal answer for a
    wallet that has never traded on Lighter.

    Lighter reports that case as an ERROR (code 21100, "account not found")
    rather than an empty list, so it's caught here and turned into None -
    otherwise registering a wallet that hasn't been funded yet fails with an
    opaque ExchangeError instead of something the participant can act on.

    On the token path this costs no API call at all: the account index is a
    field inside the token, and build_exchange_from_token has already put it
    in options.
    """
    from_token = exchange.options.get('accountIndex')
    if from_token is not None:
        return from_token

    try:
        balance = exchange.fetch_balance()
    except ccxt.ExchangeError as e:
        if 'account not found' in str(e).lower() or '21100' in str(e):
            logger.warning(
                "No Lighter account for this wallet - it may not have "
                "deposited yet. Address: %s",
                exchange.eth_get_address_from_private_key(exchange.privateKey)
            )
            return None
        raise

    accounts = ((balance.get('info') or {}).get('accounts')) or []

    if not accounts:
        logger.warning("No Lighter account visible for these credentials")
        return None

    # A wallet can hold several sub-accounts. Take the first and say so
    # rather than picking silently - the Coinbase equivalent of this once
    # matched the wrong portfolio by name and valued the wrong account.
    if len(accounts) > 1:
        logger.warning(
            "Lighter key has %d sub-accounts %s - using the first. Set "
            "account_index explicitly if that's the wrong one.",
            len(accounts),
            [a.get('account_index') or a.get('index') for a in accounts],
        )

    first = accounts[0]
    return first.get('account_index') or first.get('index')


def get_account_totals_usdc(exchange: ccxt.Exchange, account_type: str = ACCOUNT_TYPE,
                            portfolio_uuid=None) -> dict:
    """
    Value a Lighter account in USDC.

    Returns the same shape as coinbase.get_account_totals_usdc so
    balance_snapshots stores both identically:

        {'timestamp', 'datetime', 'account_type': 'perp', 'total_usdc',
         'collateral_usdc', 'available_usdc', 'cross_asset_usdc'}

    `portfolio_uuid` is named for the Coinbase column it comes from; on
    Lighter it carries the account index. Keeping the parameter name means
    the sync can pass credential['portfolio_uuid'] to either venue.

    WHICH FIGURE IS THE EQUITY - worth verifying against a live account.
    ccxt's PARSED balance reports only `collateral` for a swap account,
    which excludes open position value. The raw response also carries
    `total_asset_value`, which the ccxt docs sample shows as larger than
    collateral (9536.79 against 9000.00 with an open position). That reads
    as net account value, the same role Coinbase INTX's `total_balance`
    plays - and using collateral instead would understate anyone holding a
    position. total_asset_value is therefore preferred, with collateral as
    the fallback.
    """
    if account_type != ACCOUNT_TYPE:
        raise ValueError(
            f"Lighter has no '{account_type}' account - ccxt reports "
            f"has['spot'] = False for this venue. Only '{ACCOUNT_TYPE}' is "
            f"valid. A Lighter credential registered as '{account_type}' is "
            f"a registration error, not an empty account."
        )

    timestamp = exchange.milliseconds()

    params = {}
    if portfolio_uuid:
        params['accountIndex'] = portfolio_uuid

    balance = exchange.fetch_balance(params)
    accounts = ((balance.get('info') or {}).get('accounts')) or []

    if not accounts:
        raise ValueError(
            "Lighter returned no account for these credentials - the key may "
            "have no sub-account, or account_index may point at one that "
            "doesn't exist"
        )

    account = accounts[0]

    collateral = money_amount(account.get('collateral'))
    total = money_amount(account.get('total_asset_value'), default=collateral)

    if not total and collateral:
        # Never let a missing field zero out a real balance: a snapshot of 0
        # for a funded account reads as a total loss on the equity curve and
        # cannot be corrected after the fact.
        logger.warning(
            "Lighter total_asset_value missing or zero; falling back to "
            "collateral (%s)", collateral
        )
        total = collateral

    return {
        'timestamp': timestamp,
        'datetime': exchange.iso8601(timestamp),
        'account_type': ACCOUNT_TYPE,
        'total_usdc': total,
        'collateral_usdc': collateral,
        'available_usdc': money_amount(account.get('available_balance')),
        'cross_asset_usdc': money_amount(account.get('cross_asset_value')),
    }


def _l1_address(exchange: ccxt.Exchange) -> str:
    """
    The Ethereum address that owns this Lighter account.

    fetch_deposits and fetch_withdrawals both require it. It's read off the
    account rather than derived from the private key so that it works on the
    read-only token path, where no private key exists - and it's the same
    value either way, since ccxt derives the address from the key precisely
    to look this account up.
    """
    accounts = ((exchange.fetch_balance().get('info') or {}).get('accounts')) or []
    if accounts:
        address = accounts[0].get('l1_address')
        if address:
            return address

    # Private-key path with no account yet: fall back to deriving it.
    if getattr(exchange, 'privateKey', None):
        return exchange.eth_get_address_from_private_key(exchange.privateKey)

    raise ValueError("No L1 address available for this Lighter account")


def get_account_fee_tier(exchange: ccxt.Exchange, portfolio_uuid=None) -> dict:
    """
    What THIS account pays in fees, as opposed to what the market charges.

    Lighter's docs say Standard accounts trade free while Premium accounts
    pay maker/taker fees, discountable by staking LIT. That distinction is
    invisible in the market data: ccxt parses taker_fee/maker_fee from the
    public /orderBooks endpoint into market['taker'] and market['maker'], and
    every one of the 241 markets reads 0.0 there - which is the STANDARD
    rate, not this account's rate.

    Deriving a fee from the market rate would therefore report zero for a
    Premium participant who actually paid, flattering them on a leaderboard
    scored net of costs. The per-account figures live here instead:

        user_tier               'standard' | (premium tiers)
        current_taker_fee_tick  0 for standard
        current_maker_fee_tick  0 for standard
        effective_lit_stakes    LIT staked, which discounts the above

    The fee "ticks" are not a rate - the unit isn't documented, and a
    standard account reports 0 for both, so there's nothing to calibrate
    against. They're returned raw rather than converted into a percentage
    that would be a guess.
    """
    account_index = portfolio_uuid or exchange.options.get('accountIndex')
    if account_index is None:
        raise ValueError("An account index is required to read the fee tier")

    response = exchange.privateGetAccountLimits({'account_index': int(account_index)})

    tier = response.get('user_tier') or response.get('user_tier_name')

    return {
        'user_tier': tier,
        'taker_fee_tick': response.get('current_taker_fee_tick'),
        'maker_fee_tick': response.get('current_maker_fee_tick'),
        'lit_staked': response.get('effective_lit_stakes'),
        'fees_are_zero': (tier == 'standard'
                          and not response.get('current_taker_fee_tick')
                          and not response.get('current_maker_fee_tick')),
    }


def account_type_from_order(order: dict) -> str:
    """
    Always 'perp'. Present so callers can treat the two venue modules alike.

    Unlike Coinbase - where an order's product_type genuinely has to be
    interpreted, and got it wrong for INTX perpetuals - Lighter has exactly
    one product type, so there is nothing to infer.
    """
    return ACCOUNT_TYPE


def _paginated(exchange: ccxt.Exchange, method: str, *args, **kwargs) -> list:
    """
    Call a ccxt history method with cursor pagination enabled.

    NOT coinbase._paginate. That helper walks history by advancing `since`,
    which Lighter ignores - it would re-request the first page until the
    duplicate-id guard tripped, silently returning one page of history.
    Lighter paginates by cursor, and ccxt implements that itself when
    params={'paginate': True}.
    """
    params = dict(kwargs.pop('params', {}))
    params['paginate'] = True
    return getattr(exchange, method)(*args, params=params, **kwargs)


def closed_trades(exchange: ccxt.Exchange, symbol: str = None, since=None,
                  limit: int = 100, portfolio_uuid=None) -> list[dict]:
    """
    A participant's fills, in the same shape as coinbase.closed_trades.

    This is the RELIABLE history source on Lighter: fetch_my_trades takes no
    symbol, sorts by timestamp and paginates by cursor, where
    fetch_closed_orders requires a symbol and returns only the most recent
    100 per market.
    """
    since = resolve_since(exchange, since)

    params = {}
    if portfolio_uuid:
        params['accountIndex'] = portfolio_uuid

    raw = _paginated(exchange, 'fetch_my_trades', symbol, since, limit,
                     params=params)

    clean = []
    for trade in raw:
        try:
            fee_info = trade.get('fee') or {}
            clean.append({
                'account_type': ACCOUNT_TYPE,
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
            logger.warning("Skipping malformed Lighter trade %s: %s",
                           trade.get('id'), e)
            continue

    return clean


def closed_orders(exchange: ccxt.Exchange, symbol: str = None, since=None,
                  limit: int = 100, portfolio_uuid=None) -> list[dict]:
    """
    A participant's closed orders, in the same shape as
    coinbase.closed_orders - ready to upsert into trade_metrics.

    BUILT FROM FILLS, NOT FROM fetch_closed_orders. That endpoint requires a
    symbol and caps at 100 per market, so covering a participant would mean
    one call per market and would still miss anyone with more than 100
    closed orders in any of them. fetch_my_trades has neither limit, so fills
    are fetched and aggregated back into orders here.

    Aggregation is by order id: amount sums, price is the size-weighted
    average of the fills, fees sum, and the timestamp is that of the last
    fill - which matches what `average` and `filled` mean on a Coinbase
    order, so both venues store comparable rows.

    An order still open when this runs will appear with only its fills so
    far, and will be upserted again with the full amount once it completes.
    The (participant_id, exchange, account_type, order_id) key makes that a
    correction rather than a duplicate.
    """
    fills = closed_trades(exchange, symbol=symbol, since=since, limit=limit,
                          portfolio_uuid=portfolio_uuid)

    orders: dict[str, dict] = {}

    for fill in fills:
        order_id = fill.get('order_id') or fill.get('trade_id')
        if not order_id:
            logger.warning("Lighter fill with no order id, skipping: %s", fill)
            continue

        amount = fill.get('amount') or 0.0
        price = fill.get('price') or 0.0

        order = orders.get(order_id)
        if order is None:
            orders[order_id] = {
                'participant_id': None,      # populated by the sync
                'account_type': ACCOUNT_TYPE,
                'timestamp': fill.get('timestamp'),
                'datetime': fill.get('datetime'),
                'symbol': fill.get('symbol'),
                'type': fill.get('type'),
                'side': fill.get('side'),
                'price': price,
                'amount': amount,
                # Kept as None when Lighter reports no fee. Lighter's trade
                # payload carries no fee field at all, and storing 0.0 would
                # assert "this trade was free" when the truth is "unknown" -
                # a claim the leaderboard would then score people on.
                'fee_cost': fill.get('fee_cost'),
                'fee_currency': fill.get('fee_currency'),
                'order_id': order_id,
                '_notional': amount * price,
            }
            continue

        # Size-weighted average price across the fills
        order['_notional'] += amount * price
        order['amount'] += amount
        # Sum only real numbers; None + None stays None rather than becoming
        # a fabricated 0.0. A partially-reported fee still sums what's known.
        fill_fee = fill.get('fee_cost')
        if fill_fee is not None:
            order['fee_cost'] = (order['fee_cost'] or 0.0) + fill_fee
        order['fee_currency'] = order['fee_currency'] or fill.get('fee_currency')

        # Last fill wins for timing - an order is closed when it last filled
        if (fill.get('timestamp') or 0) >= (order['timestamp'] or 0):
            order['timestamp'] = fill.get('timestamp')
            order['datetime'] = fill.get('datetime')

    clean = []
    for order in orders.values():
        notional = order.pop('_notional')
        if order['amount']:
            order['price'] = notional / order['amount']
        clean.append(order)

    clean.sort(key=lambda o: o.get('timestamp') or 0)
    return clean


def get_cash_flows(exchange: ccxt.Exchange, since=None, limit: int = 100,
                   portfolio_uuid=None) -> list[dict]:
    """
    External deposits and withdrawals, in the same shape as
    coinbase.get_cash_flows - signed usdc_value, deposits positive.

    Two structural differences from Coinbase:

    1. There is no fetch_deposits_withdrawals on Lighter. Deposits and
       withdrawals are separate endpoints, fetched and merged here.

    2. There is no allowlist of transfer types. Coinbase needs one because
       its transactions endpoint reports trades as 'deposits', and counting
       those as funding would subtract every trade from a participant's
       return. Lighter's deposit and withdrawal endpoints report only
       genuine bridge movements, so there is nothing to filter - and an
       allowlist copied across would reject every real transfer, leaving
       returns silently unadjusted for funding.

    fetch_deposits additionally requires the L1 address, which is derived
    from the private key.
    """
    since = resolve_since(exchange, since)

    params = {}
    if portfolio_uuid:
        params['accountIndex'] = portfolio_uuid

    # fetch_deposits raises ArgumentsRequired without an L1 address. Read it
    # off the account rather than deriving it from the private key: on the
    # read-only token path there IS no private key, and deriving would fail
    # with 'NoneType is not subscriptable' - leaving returns unadjusted for
    # funding, which is exactly the error cash flows exist to prevent.
    try:
        params['address'] = _l1_address(exchange)
    except Exception as e:
        logger.warning("Could not determine the L1 address for Lighter "
                       "transfers - returns will not be adjusted for "
                       "funding: %s", e)
        return []

    raw = []
    for method, direction in (('fetch_deposits', 'in'),
                              ('fetch_withdrawals', 'out')):
        try:
            for entry in _paginated(exchange, method, None, since, limit,
                                    params=params):
                raw.append((entry, direction))
        except ccxt.NotSupported:
            logger.warning("Lighter does not support %s - transfers of that "
                           "direction will be missing", method)
        except Exception as e:
            logger.warning("Could not fetch Lighter %s: %s", method, e)

    flows = []
    for entry, direction in raw:
        try:
            if entry.get('status') not in (None, 'ok'):
                continue                   # pending or failed moved nothing

            amount = entry.get('amount')
            currency = entry.get('currency')
            timestamp = entry.get('timestamp')

            if not amount or timestamp is None:
                logger.warning("Skipping Lighter transfer %s: incomplete",
                               entry.get('id'))
                continue

            # Lighter settles in USDC. Anything else is priced at zero rather
            # than guessed, and says so - a wrong price here lands directly
            # in a participant's return.
            if currency and currency not in USD_EQUIVALENTS:
                logger.warning(
                    "Lighter transfer %s is in %s, not a USD equivalent - "
                    "valued at 0. Price it before trusting this participant's "
                    "returns.", entry.get('id'), currency
                )
                price = 0.0
            else:
                price = 1.0

            flows.append({
                'participant_id': None,     # populated by the sync
                'account_type': ACCOUNT_TYPE,
                'timestamp': timestamp,
                'datetime': entry.get('datetime'),
                'direction': direction,
                'currency': currency or 'USDC',
                'amount': float(amount),
                'usdc_value': float(amount) * price * (1 if direction == 'in' else -1),
                'transfer_id': entry.get('id'),
                'raw_type': entry.get('type') or direction,
            })

        except Exception as e:
            logger.warning("Skipping malformed Lighter transfer %s: %s",
                           entry.get('id'), e)
            continue

    flows.sort(key=lambda f: f.get('timestamp') or 0)
    return flows


if __name__ == "__main__":
    # Ad hoc smoke test against your own .env key. Mirrors coinbase.py's.
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        ex = exchange_from_env()
        index = find_account_index(ex)
        totals = get_account_totals_usdc(ex, portfolio_uuid=index)
        orders = closed_orders(ex, portfolio_uuid=index)
        flows = get_cash_flows(ex, portfolio_uuid=index)
        print(f"account_index={index}")
        print(f"perp: {totals['total_usdc']:.2f} USDC "
              f"(collateral {totals['collateral_usdc']:.2f}), "
              f"{len(orders)} order(s), {len(flows)} cash flow(s)")
    except Exception as e:
        print(f"lighter: {type(e).__name__}: {e}")
