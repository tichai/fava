"""This module provides the data required by Fava's reports."""

import copy
import datetime
import os

from beancount import loader
from beancount.core import getters, realization
from beancount.core.flags import FLAG_UNREALIZED
from beancount.core.account_types import get_account_sign
from beancount.core.compare import hash_entry
from beancount.core.data import (get_entry, iter_entry_dates, Open, Close,
                                 Document, Balance, TxnPosting, Transaction,
                                 Event, Custom)
from beancount.ops import prices, summarize
from beancount.parser.options import get_account_types
from beancount.reports.context import render_entry_context
from beancount.utils.encryption import is_encrypted_file
from beancount.utils.misc_utils import filter_type

from fava.util import date
from fava.core.budgets import BudgetModule
from fava.core.charts import ChartModule
from fava.core.fava_options import parse_options
from fava.core.file import FileModule
from fava.core.filters import (AccountFilter, FromFilter, PayeeFilter,
                               TagFilter, TimeFilter)
from fava.core.helpers import FavaAPIException, FavaModule
from fava.core.holdings import get_final_holdings, aggregate_holdings_by
from fava.core.misc import FavaMisc
from fava.core.query_shell import QueryShell
from fava.core.watcher import Watcher
from fava.ext import find_extensions


def _list_accounts(root_account, active_only=False):
    """List of all sub-accounts of the given root."""
    accounts = [child_account.account
                for child_account in
                realization.iter_children(root_account)
                if not active_only or child_account.txn_postings]

    return accounts if active_only else accounts[1:]


# pylint: disable=too-few-public-methods
class AttributesModule(FavaModule):
    """Some attributes of the ledger (mostly for auto-completion)."""

    def __init__(self, ledger):
        super().__init__(ledger)
        self.accounts = None
        self.payees = None
        self.tags = None
        self.years = None

    def load_file(self):
        all_entries = self.ledger.all_entries
        self.payees = getters.get_all_payees(all_entries)
        self.tags = getters.get_all_tags(all_entries)
        self.years = list(getters.get_active_years(all_entries))

        self.accounts = _list_accounts(self.ledger.all_root_account,
                                       active_only=True)


# pylint: disable=too-few-public-methods
class ExtensionModule(FavaModule):
    """Some attributes of the ledger (mostly for auto-completion)."""

    def __init__(self, ledger):
        super().__init__(ledger)
        self._extensions = None
        self._instances = {}

    def load_file(self):
        self._extensions = []
        for extension in self.ledger.fava_options['extensions']:
            extensions, errors = find_extensions(
                os.path.dirname(self.ledger.beancount_file_path), extension)
            self._extensions.extend(extensions)
            self.ledger.errors.extend(errors)

        for cls in self._extensions:
            if cls not in self._instances:
                self._instances[cls] = cls(self.ledger)

    def run_hook(self, event, *args):
        for ext in self._instances.values():
            ext.run_hook(event, *args)


MODULES = ['attributes', 'budgets', 'charts', 'extensions', 'file', 'misc',
           'query_shell']


# pylint: disable=too-many-instance-attributes
class FavaLedger():
    """Create an interface for a Beancount ledger.

    Arguments:
        path: Path to the main Beancount file.

    """

    __slots__ = [
        '_default_format_string', '_format_string', 'account_types',
        'all_entries', 'all_root_account', 'beancount_file_path',
        '_date_first', '_date_last', 'entries', 'errors',
        'fava_options', '_filters', '_is_encrypted', 'options', 'price_map',
        'root_account', '_watcher'] + MODULES

    def __init__(self, path):
        #: The path to the main Beancount file.
        self.beancount_file_path = path
        self._is_encrypted = is_encrypted_file(path)
        self._filters = {
            'account': AccountFilter(),
            'from': FromFilter(),
            'payee': PayeeFilter(),
            'tag': TagFilter(),
            'time': TimeFilter(),
        }

        #: An :class:`AttributesModule` instance.
        self.attributes = AttributesModule(self)

        #: A :class:`.BudgetModule` instance.
        self.budgets = BudgetModule(self)

        #: A :class:`.ChartModule` instance.
        self.charts = ChartModule(self)

        #: A :class:`.ExtensionModule` instance.
        self.extensions = ExtensionModule(self)

        #: A :class:`.FileModule` instance.
        self.file = FileModule(self)

        #: A :class:`.FavaMisc` instance.
        self.misc = FavaMisc(self)

        #: A :class:`.QueryShell` instance.
        self.query_shell = QueryShell(self)

        self._watcher = Watcher()

        #: List of all (unfiltered) entries.
        self.all_entries = None

        #: A list of all errors reported by Beancount.
        self.errors = None

        #: A Beancount options map.
        self.options = None

        #: A Namedtuple containing the names of the five base accounts.
        self.account_types = None

        #: A dict with all of Fava's option values.
        self.fava_options = None

        self.load_file()

    def load_file(self):
        """Load the main file and all included files and set attributes."""
        # use the internal function to disable cache
        if not self._is_encrypted:
            # pylint: disable=protected-access
            self.all_entries, self.errors, self.options = \
                loader._load([(self.beancount_file_path, True)],
                             None, None, None)
            include_path = os.path.dirname(self.beancount_file_path)
            self._watcher.update(self.options['include'], [
                os.path.join(include_path, path)
                for path in self.options['documents']])
        else:
            self.all_entries, self.errors, self.options = \
                loader.load_file(self.beancount_file_path)
        self.price_map = prices.build_price_map(self.all_entries)
        self.account_types = get_account_types(self.options)
        self.all_root_account = realization.realize(self.all_entries,
                                                    self.account_types)
        if self.options['render_commas']:
            self._format_string = '{:,f}'
            self._default_format_string = '{:,.2f}'
        else:
            self._format_string = '{:f}'
            self._default_format_string = '{:.2f}'

        self.fava_options, errors = parse_options(
            filter_type(self.all_entries, Custom))
        self.errors.extend(errors)

        for mod in MODULES:
            getattr(self, mod).load_file()

        self.filter(True)

    # pylint: disable=attribute-defined-outside-init
    def filter(self, force=False, **kwargs):
        """Set and apply (if necessary) filters."""
        changed = False
        for filter_name, value in kwargs.items():
            if self._filters[filter_name].set(value):
                changed = True

        if not (changed or force):
            return

        self.entries = self.all_entries

        for filter_class in self._filters.values():
            self.entries = filter_class.apply(self.entries, self.options)

        self.root_account = realization.realize(self.entries,
                                                self.account_types)

        self._date_first, self._date_last = \
            getters.get_min_max_dates(self.entries, (Transaction))
        if self._date_last:
            self._date_last = self._date_last + datetime.timedelta(1)

        if self._filters['time']:
            self._date_first = self._filters['time'].begin_date
            self._date_last = self._filters['time'].end_date

    def changed(self):
        """Check if the file needs to be reloaded.

        Returns:
            True if a change in one of the included files or a change in a
            document folder was detected and the file has been reloaded.

        """
        # We can't reload an encrypted file, so act like it never changes.
        if self._is_encrypted:
            return False
        changed = self._watcher.check()
        if changed:
            self.load_file()
        return changed

    def quantize(self, value, currency):
        """Quantize the value to the right number of decimal digits.

        Uses the DisplayContext generated by beancount."""
        if not currency:
            return self._default_format_string.format(value)
        return self._format_string.format(
            self.options['dcontext'].quantize(value, currency))

    def _interval_tuples(self, interval):
        """Calculates tuples of (begin_date, end_date) of length interval for the
        period in which entries contains transactions.  """
        return date.interval_tuples(self._date_first, self._date_last,
                                    interval)

    def get_account_sign(self, account_name):
        """Get account sign."""
        return get_account_sign(account_name, self.account_types)

    @property
    def root_account_closed(self):
        """A root account where closing entries have been generated."""
        closing_entries = summarize.cap_opt(self.entries, self.options)
        return realization.realize(closing_entries)

    def interval_balances(self, interval, account_name, accumulate=False):
        """Balances by interval.

        Arguments:
            interval: An interval.
            account_name: An account name.
            accumulate: A boolean, ``True`` if the balances for an interval
                should include all entries up to the end of the interval.

        Returns:
            A list of RealAccount instances for all the intervals.

        """
        min_accounts = [account
                        for account in _list_accounts(self.all_root_account)
                        if account.startswith(account_name)]

        interval_tuples = list(reversed(self._interval_tuples(interval)))

        interval_balances = [
            realization.realize(list(iter_entry_dates(
                self.entries,
                self._date_first if accumulate else begin_date,
                end_date)), min_accounts)
            for begin_date, end_date in interval_tuples]

        return interval_balances, interval_tuples

    def account_journal(self, account_name, with_journal_children=False):
        """Journal for an account.

        Args:
            account_name: An account name.
            with_journal_children: Whether to include postings of subaccounts
                of the given account.

        Returns:
            A list of tuples ``(entry, postings, change, balance)``.

        """
        real_account = realization.get_or_create(self.root_account,
                                                 account_name)

        if with_journal_children:
            postings = realization.get_postings(real_account)
        else:
            postings = real_account.txn_postings

        return [(entry, postings, change, copy.copy(balance)) for
                (entry, postings, change, balance) in
                realization.iterate_with_balance(postings)]

    def events(self, event_type=None):
        """List events (possibly filtered by type)."""
        events = list(filter_type(self.entries, Event))

        if event_type:
            return filter(lambda e: e.type == event_type, events)

        return events

    def holdings(self, aggregation_key=None):
        """List all holdings (possibly aggregated)."""

        # Get latest price unless there's an active time filter.
        price_date = self._filters['time'].end_date \
            if self._filters['time'] else None

        holdings_list = get_final_holdings(
            self.entries,
            (self.account_types.assets, self.account_types.liabilities),
            self.price_map,
            price_date
        )

        if aggregation_key:
            holdings_list = aggregate_holdings_by(holdings_list,
                                                  aggregation_key)
        return holdings_list

    def get_entry(self, entry_hash):
        """Find an entry.

        Arguments:
            entry_hash: Hash of the entry.

        Returns:
            The entry with the given hash.
        Raises:
            FavaAPIException: If there is no entry for the given hash.

        """
        try:
            return next(entry for entry in self.all_entries
                        if entry_hash == hash_entry(entry))
        except StopIteration:
            raise FavaAPIException('No entry found for hash "{}"'
                                   .format(entry_hash))

    def context(self, entry_hash):
        """Context for an entry.

        Arguments:
            entry_hash: Hash of entry.

        Returns:
            A tuple ``(entry, context)`` of the (unique) entry with the given
            ``entry_hash`` and its context.

        """
        entry = self.get_entry(entry_hash)
        ctx = render_entry_context(self.all_entries, self.options, entry)
        return entry, ctx.split("\n", 2)[2]

    def commodity_pairs(self):
        """List pairs of commodities.

        Returns:
            A list of pairs of commodities. Pairs of operating currencies will
            be given in both directions not just in the one found in file.

        """
        fw_pairs = self.price_map.forward_pairs
        bw_pairs = []
        for currency_a, currency_b in fw_pairs:
            if (currency_a in self.options['operating_currency'] and
                    currency_b in self.options['operating_currency']):
                bw_pairs.append((currency_b, currency_a))
        return sorted(fw_pairs + bw_pairs)

    def prices(self, base, quote):
        """List all prices."""
        all_prices = prices.get_all_prices(self.price_map,
                                           "{}/{}".format(base, quote))

        if self._filters['time']:
            return [(date, price) for date, price in all_prices
                    if (date >= self._filters['time'].begin_date and
                        date < self._filters['time'].end_date)]
        else:
            return all_prices

    def last_entry(self, account_name):
        """Get last entry of an account.

        Args:
            account_name: An account name.

        Returns:
            The last entry of the account if it is not a Close entry.
        """
        account = realization.get_or_create(self.all_root_account,
                                            account_name)

        last = realization.find_last_active_posting(account.txn_postings)

        if last is None or isinstance(last, Close):
            return

        return get_entry(last)

    @property
    def postings(self):
        """All postings contained in some transaction."""
        return [posting for entry in filter_type(self.entries, Transaction)
                for posting in entry.postings]

    def statement_path(self, entry_hash, metadata_key):
        """Returns the path for a statement found in the specified entry."""
        entry = self.get_entry(entry_hash)
        value = entry.meta[metadata_key]

        beancount_dir = os.path.dirname(self.beancount_file_path)
        paths = [os.path.join(beancount_dir,
                              value)]
        paths.extend([os.path.join(beancount_dir, document_root,
                                   posting.account.replace(':', '/'), value)
                      for posting in entry.postings
                      for document_root in self.options['documents']])

        for path in paths:
            if os.path.isfile(path):
                return path

        raise FavaAPIException('Statement not found.')

    def document_path(self, path):
        """Get absolute path of a document.

        Returns:
            The absolute path of ``path`` if it points to a document.

        Raises:
            FavaAPIException: If ``path`` is not the path of one of the
                documents.

        """
        for entry in filter_type(self.entries, Document):
            if entry.filename == path:
                return path

        raise FavaAPIException(
            'File "{}" not found in document entries.'.format(path))

    def account_uptodate_status(self, account_name):
        """Status of the last balance or transaction.

        Args:
            account_name: An account name.

        Returns:
            A status string for the last balance or transaction of the account.

            - 'green':  A balance check that passed.
            - 'red':    A balance check that failed.
            - 'yellow': Not a balance check.
        """

        real_account = realization.get_or_create(self.all_root_account,
                                                 account_name)

        for txn_posting in reversed(real_account.txn_postings):
            if isinstance(txn_posting, Balance):
                if txn_posting.diff_amount:
                    return 'red'
                else:
                    return 'green'
            if isinstance(txn_posting, TxnPosting) and \
                    txn_posting.txn.flag != FLAG_UNREALIZED:
                return 'yellow'

    def account_metadata(self, account_name):
        """Metadata of the account.

        Args:
            account_name: An account name.

        Returns:
            Metadata of the Open entry of the account.
        """
        real_account = realization.get_or_create(self.root_account,
                                                 account_name)
        for posting in real_account.txn_postings:
            if isinstance(posting, Open):
                return posting.meta
        return {}
