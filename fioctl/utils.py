from datetime import datetime
import itertools
import json
import time
import click
import concurrent.futures
import os
import sys
import math
from tabulate import tabulate
from token_bucket import Limiter
from token_bucket import MemoryStorage

from .config import nested_get, nested_set

class ListType(click.ParamType):
    name = "list"

    def convert(self, value, _param, _ctx):
        if isinstance(value, list):
            return value
        return [val.strip() for val in value.split(",")]

class UpdateType(click.ParamType):
    name = "update"

    def convert(self, value, _param, _ctx):
        update = [tuple(val.strip().split("=")) for val in value.split(",")]
        nested_update = [(column.split("."), update) for (column, update) in update]

        update = {}
        for (nested_key, val) in nested_update:
            nested_set(update, nested_key, val)
        
        return update

class FormatType(click.ParamType):
    name = "format"

    def convert(self, value, _param, _ctx):
        return self.formatters()[value]

    def formatters(self):
        return {
            "json": self.format_json,
            "table": self.format_table
        }
    
    def format_json(self, value, **kwargs):
        return json.dumps(value, indent=2, sort_keys=True)

    def format_table(self, value, cols=None):
        if isinstance(value, dict):
            cols = cols or value.keys()
            fetch_map = {col: col.split(".") for col in cols}
            return tabulate(
                [(col, self._convert(nested_get(value, fetch_map[col]))) for col in cols], headers=["attribute", "value"], tablefmt='psql')
        
        value = list(value)
        if not value:
            return "No results"
        
        cols = cols or list(value[0].keys())
        fetch_map = {col: col.split(".") for col in cols}
        return tabulate(self._list_table_format(value, cols, fetch_map), headers=cols, tablefmt="psql")
    
    def _convert(self, value):
        if isinstance(value, dict):
            return self.format_json(value)
        return value
        
    def _list_table_format(self, l, cols, fetch_map):
        def tableize_row(row):
            return [self._convert(nested_get(row, fetch_map[col])) for col in cols]
        
        return [tableize_row(row) for row in l]


def merge_streams(stream, other_stream, comparison=lambda x, y: x["id"] <= y["id"]):
    fetch = lambda stream: next(stream, None)
    head1, head2 = fetch(stream), fetch(other_stream)

    while head1 and head2:
        if comparison(head1, head2):
            yield head1
            head1 = fetch(stream)
        else:
            yield head2
            head2 = fetch(other_stream)
    
    if head1:
        yield head1
        for head1 in stream:
            yield head1
    
    if head2:
        yield head2
        for head2 in other_stream:
            yield head2


def datetime_compare(first, second):
    return from_iso(first) <= from_iso(second)

def from_iso(date_string):
    return datetime.strptime(date_string, "%Y-%m-%dT%H:%M:%S.%fZ")

def exec_stream(callable, iterable, sync=lambda _: False, capacity=10, rate=10):
    """
    Executes a stream according to a defined rate limit.
    """
    limiter = Limiter(capacity, rate, MemoryStorage())
    futures = set()

    def execute(operation):
        return (operation, callable(operation))

    with concurrent.futures.ThreadPoolExecutor(max_workers=capacity) as executor:
        while True:
            if not limiter.consume("stream", 1):
                start = int(time.time())
                done, pending = concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    yield future.result()

                futures = pending
                if (int(time.time()) - start) < 1:
                    time.sleep(1.0 / rate) # guarantee there's capacity in the rate limit at end of the loop

            operation = next(iterable, None)

            if not operation:
                done, _ = concurrent.futures.wait(futures)
                for future in done:
                    yield future.result()
                break

            if sync(operation):
                yield execute(operation)
                continue

            futures.add(executor.submit(execute, operation))

def parallelize(callable, iterable, capacity=10):
    with concurrent.futures.ThreadPoolExecutor(max_workers=capacity) as executor:
        return executor.map(callable, iterable)

def chunker(iterable, n):
    it = iter(iterable)
    while True:
       chunk = tuple(itertools.islice(it, n))
       if not chunk:
           return
       yield chunk

def stream_fs(root):
    for directory, subdirs, files in os.walk(root):
        for folder in subdirs:
            yield ('d', os.path.join(directory, folder))
        for f in files:
            yield ('f', os.path.join(directory, f))

def retry(callable, *args, **kwargs):
    attempt = kwargs.pop('attempt', 0)
    try:
        callable(*args, **kwargs)
    except:
        click.echo(f"Retrying {sys.exc_info()[0]}")
        time.sleep(min(.5 * math.pow(2, attempt), 4))
        kwargs['attempt'] = attempt + 1
        retry(callable, *args, **kwargs)