"""Async-test support contract."""

import asyncio

import pytest


@pytest.mark.asyncio
async def test_async_tests_are_supported() -> None:
    """Future async adapters can be tested without custom event-loop plumbing."""
    await asyncio.sleep(0)
