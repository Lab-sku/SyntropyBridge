"""Tests for weighted random channel selection.

Verifies that ``ChannelService.select_channel`` distributes traffic
proportionally to the ``weight`` column rather than using uniform
round-robin.
"""

from __future__ import annotations

from collections import Counter

import pytest

from backend.security import Security
from backend.services.channel_service import ChannelService


def _insert_channel(conn, *, provider: str, name: str, weight: int) -> int:
    """Insert an active channel directly and return its ID."""
    encrypted = Security.encrypt("test-key")
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO channels
            (provider, name, base_url, api_key_encrypted, weight, is_active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (provider, name, "https://example.com", encrypted, weight),
    )
    conn.commit()
    return int(cur.lastrowid)


class TestWeightedDistribution:
    """1 000 selections with weights 100/50/50 should yield
    roughly 50 %/25 %/25 % (±10 % tolerance)."""

    N = 1000

    def test_weighted_distribution(self, temp_db):
        import sqlite3

        conn = sqlite3.connect(temp_db)
        conn.row_factory = sqlite3.Row

        id_a = _insert_channel(conn, provider="openai", name="ch-a", weight=100)
        id_b = _insert_channel(conn, provider="openai", name="ch-b", weight=50)
        id_c = _insert_channel(conn, provider="openai", name="ch-c", weight=50)
        conn.close()

        counts: Counter[int] = Counter()
        for _ in range(self.N):
            ch = ChannelService.select_channel(provider="openai")
            assert ch is not None
            counts[ch.id] += 1

        # Expected shares: 100/(100+50+50)=50%, 50/200=25%, 50/200=25%
        # Allow ±10% absolute tolerance.
        assert counts[id_a] / self.N == pytest.approx(0.50, abs=0.10)
        assert counts[id_b] / self.N == pytest.approx(0.25, abs=0.10)
        assert counts[id_c] / self.N == pytest.approx(0.25, abs=0.10)

        # All three channels must have been selected at least once.
        assert set(counts.keys()) == {id_a, id_b, id_c}


class TestEdgeCases:
    """Boundary conditions for weighted selection."""

    def test_single_candidate_always_returned(self, temp_db):
        import sqlite3

        conn = sqlite3.connect(temp_db)
        conn.row_factory = sqlite3.Row
        cid = _insert_channel(conn, provider="anthropic", name="solo", weight=100)
        conn.close()

        for _ in range(50):
            ch = ChannelService.select_channel(provider="anthropic")
            assert ch is not None
            assert ch.id == cid

    def test_no_candidates_returns_none(self, temp_db):
        assert ChannelService.select_channel(provider="nonexistent") is None

    def test_zero_weight_clamped_to_one(self, temp_db):
        """A channel with weight=0 should still be selectable (clamped to 1),
        but a channel with weight=1000 should dominate."""
        import sqlite3

        conn = sqlite3.connect(temp_db)
        conn.row_factory = sqlite3.Row
        id_low = _insert_channel(conn, provider="google", name="zero-w", weight=0)
        id_high = _insert_channel(conn, provider="google", name="high-w", weight=1000)
        conn.close()

        counts: Counter[int] = Counter()
        for _ in range(500):
            ch = ChannelService.select_channel(provider="google")
            assert ch is not None
            counts[ch.id] += 1

        # weight=0 clamped to 1, weight=1000 → expected ratio ~1/1001 vs 1000/1001
        # The high-weight channel should get the vast majority.
        assert counts[id_high] > counts[id_low]
        assert counts[id_low] > 0  # still gets SOME traffic

    def test_exclude_ids_respected(self, temp_db):
        import sqlite3

        conn = sqlite3.connect(temp_db)
        conn.row_factory = sqlite3.Row
        _insert_channel(conn, provider="openai", name="excluded", weight=100)
        id_kept = _insert_channel(conn, provider="openai", name="kept", weight=100)

        # Fetch the excluded channel's ID
        cur = conn.cursor()
        cur.execute("SELECT id FROM channels WHERE name = 'excluded'")
        excluded_id = int(cur.fetchone()["id"])
        conn.close()

        for _ in range(20):
            ch = ChannelService.select_channel(provider="openai", exclude_ids={excluded_id})
            assert ch is not None
            assert ch.id == id_kept
