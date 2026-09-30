"""Tests for pool.py's active/idle connection tracking, the plumbing behind the admin UI's real-time
connection-pool panel (queryapigate_pool_active_connections / queryapigate_pool_idle_connections)."""
import threading
import unittest

from queryapigate.pool import ConnectionPool


class FakeConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeDriver:
    """A minimal driver: connect() hands out a fresh FakeConn every time, close()/reset() do nothing real."""

    def __init__(self):
        self.connects = 0

    def connect(self, details, read_only):
        self.connects += 1
        return FakeConn()

    def is_alive(self, session):
        return True

    def reset(self, session):
        pass

    def close(self, session):
        session.conn.close()


class ActiveCountTests(unittest.TestCase):
    def setUp(self):
        self.pool = ConnectionPool()
        self.driver = FakeDriver()

    def test_zero_when_nothing_is_checked_out(self):
        self.assertEqual(self.pool.active_count(), 0)

    def test_one_while_a_single_checkout_is_in_flight(self):
        with self.pool.checkout(self.driver, {}, False) as session:
            self.assertIsNotNone(session)
            self.assertEqual(self.pool.active_count(), 1)
        self.assertEqual(self.pool.active_count(), 0)

    def test_drops_back_to_zero_after_a_normal_release(self):
        with self.pool.checkout(self.driver, {}, False):
            pass
        self.assertEqual(self.pool.active_count(), 0)
        self.assertEqual(self.pool.idle_count(), 1)  # released, not lost - it's sitting idle now

    def test_drops_back_to_zero_even_when_the_body_raises(self):
        with self.assertRaises(ValueError):
            with self.pool.checkout(self.driver, {}, False):
                raise ValueError('boom')
        self.assertEqual(self.pool.active_count(), 0)
        self.assertEqual(self.pool.idle_count(), 0)  # a session that saw an error is closed, never pooled

    def test_counts_several_concurrent_checkouts_on_the_same_key(self):
        entered = threading.Barrier(4)  # 3 worker threads + this one, all synchronized before the read below
        release = threading.Event()
        seen_during = []

        def hold():
            with self.pool.checkout(self.driver, {}, False):
                entered.wait(timeout=5)
                release.wait(timeout=5)

        threads = [threading.Thread(target=hold) for _ in range(3)]
        for t in threads:
            t.start()
        entered.wait(timeout=5)
        seen_during.append(self.pool.active_count())
        release.set()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(seen_during, [3])
        self.assertEqual(self.pool.active_count(), 0)

    def test_idle_and_active_are_independent_figures(self):
        """A connection already idle in the pool, checked out again, is active - not idle - for as long as
        it's held; idle_count() and active_count() never double-count the same physical connection."""
        with self.pool.checkout(self.driver, {}, False):
            pass  # one connection now idle
        self.assertEqual(self.pool.idle_count(), 1)
        self.assertEqual(self.pool.active_count(), 0)
        with self.pool.checkout(self.driver, {}, False):
            self.assertEqual(self.pool.idle_count(), 0)  # reused, not idle anymore
            self.assertEqual(self.pool.active_count(), 1)
        self.assertEqual(self.driver.connects, 1)  # the second checkout reused it - never opened a new one


if __name__ == '__main__':
    unittest.main()
