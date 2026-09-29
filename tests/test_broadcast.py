"""Tests for broadcast.py - the in-process pub/sub Broadcaster (BACKLOG #43)."""
import queue
import unittest

from queryapigate import broadcast


class BroadcasterTests(unittest.TestCase):
    def setUp(self):
        self.b = broadcast.Broadcaster()

    def test_a_fresh_broadcaster_has_no_subscribers(self):
        self.assertEqual(self.b.subscriber_count(), 0)

    def test_subscribe_registers_one_subscriber(self):
        self.b.subscribe()
        self.assertEqual(self.b.subscriber_count(), 1)

    def test_a_published_event_reaches_a_subscriber(self):
        q = self.b.subscribe()
        self.b.publish({'type': 'execution', 'filename': 'q1'})
        self.assertEqual(q.get_nowait(), {'type': 'execution', 'filename': 'q1'})

    def test_every_subscriber_gets_every_event(self):
        q1, q2 = self.b.subscribe(), self.b.subscribe()
        self.b.publish({'a': 1})
        self.assertEqual(q1.get_nowait(), {'a': 1})
        self.assertEqual(q2.get_nowait(), {'a': 1})

    def test_publishing_with_no_subscribers_does_nothing(self):
        self.b.publish({'a': 1})  # must not raise

    def test_unsubscribe_removes_it(self):
        q = self.b.subscribe()
        self.b.unsubscribe(q)
        self.assertEqual(self.b.subscriber_count(), 0)

    def test_unsubscribing_an_unknown_queue_is_a_no_op(self):
        self.b.unsubscribe(queue.Queue())  # never subscribed - must not raise

    def test_an_unsubscribed_queue_gets_no_further_events(self):
        q = self.b.subscribe()
        self.b.unsubscribe(q)
        self.b.publish({'a': 1})
        self.assertTrue(q.empty())

    def test_a_full_queue_drops_the_event_rather_than_blocking(self):
        q = self.b.subscribe()
        for i in range(broadcast._QUEUE_SIZE):
            self.b.publish({'i': i})
        self.assertEqual(q.qsize(), broadcast._QUEUE_SIZE)
        self.b.publish({'i': 'overflow'})  # must not raise or block
        self.assertEqual(q.qsize(), broadcast._QUEUE_SIZE)  # the overflow event was dropped, not queued
        self.assertEqual(q.get_nowait(), {'i': 0})  # the oldest event is still there - nothing was evicted


if __name__ == '__main__':
    unittest.main()
