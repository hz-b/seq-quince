"""Shared standard-library fixtures, independent of concrete device suites."""

import asyncio
import logging
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


class CoreTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp_dir = Path(temporary.name)
        level = logging.getLogger().level
        self.addCleanup(logging.getLogger().setLevel, level)
        self._previous_loop = None
        if sys.version_info < (3, 10):
            try:
                self._previous_loop = asyncio.get_event_loop()
            except RuntimeError:
                pass
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.addCleanup(self._close_loop)

    def _close_loop(self):
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
        self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        if hasattr(self.loop, "shutdown_default_executor"):
            self.loop.run_until_complete(self.loop.shutdown_default_executor())
        self.loop.close()
        asyncio.set_event_loop(self._previous_loop)

    def run_async(self, coroutine):
        return self.loop.run_until_complete(coroutine)

    def patch(self, target, attribute, *args, **kwargs):
        patcher = patch.object(target, attribute, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result
